"""BIDS — Buy In Dips, the AUTOMATIC half (owner design 2026-09-25, phase 2 on 2026-09-29).

The Portfolio → BIDS tab suggests; this deployment BUYS. It holds a list of broker-held ETFs
(``watchlist``) on one account and, once a day at 15:05, runs each through the SAME ladder
the tab uses (``services/bids_ladder.evaluate``): a new high moves the peak up and resets the
ladder, and every further X% below the peak fires the next level, k·y rupees at level k, up
to N levels. A fired level becomes one whole-unit BUY of the ETF.

FUNDING (owner answers 2026-09-29: "keep a cash float", Zerodha). The shared
``EntryFundingMixin`` in ``park`` mode: the capital sits in ``fund_source`` (LIQUIDCASE) and
``float_parts`` × the first-level amount is kept as SETTLED cash, so a level that fires buys
the same day; the ETF is sold after every spend to refill the float for tomorrow (an equity
CNC sale settles T+1), and the excess is parked back. A level the settled cash cannot cover
is QUEUED and retried at each decision, re-sized at that day's price, until it fills — or
until the price recovers to the peak, which cancels it (the dip is gone). Signal order is the
mixin's load-bearing one: fund sales, then buys, then the park-back.

ONE LADDER, TWO HALVES. Live, ``set_bids_rules_fn`` (wired by the manager, read-only over the
portfolio tables) supplies each symbol's rule from the tab — its X / y / N, whether it is
enabled at all (an excluded class, a switched-off holding and the fund itself are not), and
the ladder the tab had already built — so a holding that moves from SUGGEST to AUTO carries
its peak and its fired levels across instead of rejoining. A peak the owner types on the tab
("manual") is adopted at the next decision. After each decision the manager writes this
run's ladders and fills back to the portfolio (``services/bids.record_auto``) — for a run
whose orders reach the broker only; a PAPER run never touches the real portfolio. In a
backtest there is no hook: the ctor's X / y / N apply to every symbol and a symbol joins at
its first price, the tab's own joining rule.

The run ADOPTS the account's existing units of every watched ETF (``adoptable_symbols``),
exactly as value_investing adopts stray shares: reconciliation compares the symbols a run
holds against the broker's whole holding, so the first 10-unit buy of an ETF the account
already held 75,000 of would otherwise read as a mismatch and halt the run. It never sells a
watched ETF — the only sales are of the fund.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from skas_algo.engine.types import Signal
from skas_algo.services.bids_ladder import LadderRule, LadderState, evaluate

from ._funding import EntryFundingMixin


class BidsStrategy(EntryFundingMixin):
    strategy_id = "bids"
    # 15:20 lands in the closing auction for F&O-listed cash names since CAS; an unfilled
    # entry halts the run. Same call as value_investing / supertrend_momentum.
    default_decision_time = "15:05"
    report_holdings = True           # the backtest's accumulation panel (never sells)

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 100_000,
        dip_pct: float = 2.0,            # X — each level is X% further below the peak
        amount: float = 5000.0,          # y — level k invests k × y
        max_levels: int = 10,            # N — no level beyond N until a new high resets
        watchlist: str = "",             # comma-separated ETFs; blank = every symbol
        # ---- funding (EntryFundingMixin) — a new strategy, so the defaults ARE the design ----
        funding: str = "park",
        fund_source: str | None = "LIQUIDCASE",
        float_parts: float = 3.0,        # settled cash kept = this × the first-level amount
        settlement_days: int = 1,
        funding_buffer_pct: float = 5.0,
        fund_seed: str = "never",        # a live deploy must never place a surprise ETF buy
        fund_size_cap: bool = False,
        **_ignored,
    ):
        self.universe = [str(s).upper() for s in (universe or [])]
        self._init_funding(funding, fund_source, float_parts, settlement_days,
                           funding_buffer_pct, fund_seed, fund_size=initial_capital,
                           fund_size_cap=fund_size_cap)
        self.dip_pct = float(dip_pct)
        self.amount = float(amount)
        self.max_levels = int(max_levels)
        self.watchlist = watchlist if isinstance(watchlist, str) else ",".join(watchlist or [])
        # ---- persisted ----
        # sym -> {peak, levels_fired, peak_source, peak_asof}
        self.ladders: dict[str, dict[str, Any]] = {}
        # sym -> {amount, levels, since} — the rupees a queued buy is for (the mixin keeps
        # the units/price it last tried in pending_entries)
        self.queued: dict[str, dict[str, Any]] = {}
        self.last_shop_day: str | None = None
        self.last_fired: list[dict[str, Any]] = []      # the last decision's levels, for the tile
        # ---- transient ----
        self._rules_fn = None
        self._rules_error: str | None = None

    # ------------------------------------------------------------------ hooks
    def set_bids_rules_fn(self, fn) -> None:
        """Manager wiring (live): fn(symbols) -> {sym: rule dict} from the portfolio tables."""
        self._rules_fn = fn

    def adoptable_symbols(self) -> list[str]:
        """Every watched ETF plus the fund — see the module docstring on reconciliation."""
        return self._symbols() + ([self.fund_source] if self.fund_source else [])

    def bids_state(self) -> dict[str, dict[str, Any]]:
        """This run's ladders, for the manager to mirror onto the Portfolio → BIDS rows."""
        return {s: dict(v) for s, v in self.ladders.items()}

    def on_fund_adopted(self, symbol: str, units: float, price: float) -> None:
        """Deliberately NOT the mixin's bookkeeping. There the deploy capital is ETF + float
        and adopted fund units are subtracted from the run's cash; here the deploy CAPITAL IS
        THE FLOAT — the settled cash this run may spend — so the owner never has to know the
        fund's value at deploy. With the mixin's rule a capital below the fund's value read
        as zero settled cash and every level queued forever. Sale proceeds and park-back
        buys still move the run's cash through the engine as usual."""
        return None

    def _float_target_hint(self) -> float:
        return self.float_parts * self.amount if self.funding == "park" else 0.0

    # ------------------------------------------------------------------ helpers
    def _symbols(self) -> list[str]:
        wl = [s.strip().upper() for s in str(self.watchlist or "").split(",") if s.strip()]
        syms = wl or list(self.universe)
        return [s for s in dict.fromkeys(syms) if s != self.fund_source]

    def _rules(self, symbols: list[str]) -> dict[str, dict] | None:
        """{sym: rule} from the tab, or None when no hook is wired (backtest). A hook that
        FAILS returns {} and sets an alert: no rule means no buy — a failed read must never
        turn into buying a holding the owner switched off."""
        self._rules_error = None
        if self._rules_fn is None:
            return None
        try:
            return dict(self._rules_fn(symbols) or {})
        except Exception as exc:  # pragma: no cover - exercised via a raising fake
            self._rules_error = f"could not read the BIDS rules ({type(exc).__name__}) — no buys today"
            return {}

    def _rule_for(self, rule: dict | None) -> LadderRule:
        r = rule or {}
        return LadderRule(
            dip_pct=float(r.get("dip_pct") or self.dip_pct),
            amount=float(r.get("amount") or self.amount),
            max_levels=int(r.get("max_levels") or self.max_levels),
        )

    def _ladder(self, sym: str, rule: dict | None, close: float, today: date) -> dict | None:
        """This symbol's ladder, joining or carrying the tab's across. None = it joined
        today (the joining price is the reference, never a dip)."""
        st = self.ladders.get(sym)
        r = rule or {}
        if st is not None:
            # a reference high typed on the tab after this run took over restarts the ladder
            if (r.get("peak_source") == "manual" and r.get("peak")
                    and str(r.get("peak_asof") or "") > str(st.get("peak_asof") or "")):
                st.update(peak=float(r["peak"]), levels_fired=0, peak_source="manual",
                          peak_asof=str(r.get("peak_asof")))
            return st
        if r.get("peak"):
            # SUGGEST → AUTO: the tab's ladder, fired levels included, carries across
            st = {"peak": float(r["peak"]), "levels_fired": int(r.get("levels_fired") or 0),
                  "peak_source": r.get("peak_source") or "joined",
                  "peak_asof": str(r.get("peak_asof") or today.isoformat())}
            self.ladders[sym] = st
            return st
        self.ladders[sym] = {"peak": float(close), "levels_fired": 0, "peak_source": "joined",
                             "peak_asof": today.isoformat()}
        return None

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx) -> list[Signal]:
        today = ctx.today() if hasattr(ctx, "today") else date.today()
        if self.last_shop_day == today.isoformat():
            return []                                  # one decision a day
        float_target = self._float_target_hint()
        self._settle(ctx, today)
        seed = self._maybe_seed(ctx, float_target)
        if seed:
            return seed
        present = set(ctx.present_symbols())
        symbols = self._symbols()
        rules = self._rules(symbols)
        if self._rules_error:
            self._alert(self._rules_error)
        unpriced = [s for s in symbols if s not in present]
        if unpriced:
            self._alert(f"no price for {', '.join(unpriced[:6])} — deploy them in the run's "
                        f"symbols (a watchlist edit cannot add a price)")

        def enabled(sym: str) -> bool:
            if rules is None:
                return True                          # backtest: every symbol, ctor knobs
            r = rules.get(sym)
            return bool(r and r.get("enabled", True))

        buys: list[Signal] = []
        self.last_fired = []

        def buy(sym: str, rupees: float, close: float) -> bool:
            units = max(1, int(rupees // close))      # whole units; one at least
            sig = self._want(sym, units, close, today)
            if sig is not None:
                sig.reason = "bids_level"
                buys.append(sig)
                self.queued.pop(sym, None)
                return True
            return False

        # 1. queued levels first — the oldest claims on today's settled cash
        for sym in list(self.queued):
            if sym not in present:
                continue
            if not enabled(sym):
                self.queued.pop(sym, None)
                self._cancel_pending(sym, today, "BIDS is switched off for it")
                continue
            close = float(ctx.close(sym))
            st = self.ladders.get(sym) or {}
            if st.get("peak") and close >= float(st["peak"]):
                self.queued.pop(sym, None)
                self._cancel_pending(sym, today, "the price recovered to its high before it was funded")
                continue
            buy(sym, float(self.queued[sym]["amount"]), close)

        # 2. today's ladders
        for sym in symbols:
            if sym not in present or sym in self.queued or not enabled(sym):
                continue
            try:
                close = float(ctx.close(sym))
            except KeyError:
                continue
            if close <= 0:
                continue
            rule_row = rules.get(sym) if rules else None
            st = self._ladder(sym, rule_row, close, today)
            if st is None:
                continue                              # joined today
            out = evaluate(LadderState(float(st["peak"]), int(st["levels_fired"])), close,
                           self._rule_for(rule_row))
            if out.state.peak != st["peak"] or out.reset:
                if out.state.peak != st["peak"]:
                    st.update(peak=float(out.state.peak), peak_source="high",
                              peak_asof=today.isoformat())
            st["levels_fired"] = int(out.state.levels_fired)
            if not out.triggers:
                continue
            rupees = sum(t.amount for t in out.triggers)
            levels = [t.level for t in out.triggers]
            self.last_fired.append({"symbol": sym, "levels": levels, "amount": rupees,
                                    "price": close, "day": today.isoformat()})
            if not buy(sym, rupees, close):
                self.queued[sym] = {"amount": rupees, "levels": levels,
                                    "since": today.isoformat()}

        # 3. the fund: sell what tomorrow needs, park the excess — then order the legs
        self._fund_signals(ctx, today, float_target)
        self.last_shop_day = today.isoformat()
        return self._fund_exits + buys + self._late_buys + self._park_buy

    # ------------------------------------------------------------------ (de)serialize
    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.export_state()

    def export_state(self) -> dict[str, Any]:
        return {
            "ladders": {s: dict(v) for s, v in self.ladders.items()},
            "queued": {s: dict(v) for s, v in self.queued.items()},
            "last_shop_day": self.last_shop_day,
            "last_fired": [dict(x) for x in self.last_fired],
            **self.funding_state(),
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.ladders = {s: dict(v) for s, v in (state.get("ladders") or {}).items()}
        self.queued = {s: dict(v) for s, v in (state.get("queued") or {}).items()}
        self.last_shop_day = state.get("last_shop_day")
        self.last_fired = [dict(x) for x in (state.get("last_fired") or [])]
        self.load_funding_state(state)

    def exit_rules(self) -> list[str]:
        return [
            "Never sells a watched ETF — the only sales are of the fund",
            f"Buys each further {self.dip_pct:g}% below the peak: level k invests k × "
            f"₹{self.amount:,.0f}, up to {self.max_levels} levels; a new high resets "
            "(per-holding rules from Portfolio → BIDS outrank these when live)",
        ] + self.funding_rules()
