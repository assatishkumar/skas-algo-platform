"""BIDS — Buy In Dips, the AUTOMATIC half (owner design 2026-09-25, phase 2 on 2026-09-29).

The Portfolio → BIDS tab suggests; this deployment BUYS. It holds a list of broker-held ETFs
(``watchlist``) on one account and, once a day at 15:05, runs each through the SAME ladder
the tab uses (``services/bids_ladder.evaluate``): a new high moves the peak up and resets the
ladder, and every further X% below the peak fires the next level, k·y rupees at level k, up
to N levels. A fired level becomes one whole-unit BUY of the ETF.

FUNDING — the owner's model, in their words (2026-09-29): "LIQUIDCASE is for funding. No
buying here. Whenever we need to buy any ETF or Stock configured for BIDS, the corresponding
LIQUIDCASE is sold." So every fired level SELLS whole units of ``fund_source`` worth what the
buy costs, in the same decision and ahead of the buy, and the fund is NEVER bought — no float
top-up, no park-back (the first version used the mixin's ``park`` mode, which parks any cash
above a float back into the fund; with ₹30k of capital that would have bought LIQUIDCASE).
An equity sale settles T+1, so today's buy is paid from the run's own cash — the deploy
CAPITAL is that buffer — and the sale replaces it tomorrow. A level the settled cash cannot
cover is QUEUED (its sale already placed, ``funded``) and retried at each decision, re-sized
at that day's price, WITHOUT selling again; it is cancelled if the price recovers to the peak
and the proceeds simply stay as cash. No fund left to sell → alert, and the buy still goes
ahead from cash if it can (a dip is never dropped). The mixin supplies only the settlement
LEDGER here (``_settle`` / ``_credit`` / ``_want`` / ``funding_state``), never its float.

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

The run ADOPTS the account's existing units of every watched ETF and of the fund
(``adoptable_symbols``), exactly as value_investing adopts stray shares: reconciliation
compares the symbols a run holds against the broker's whole holding, so the first 10-unit buy
of an ETF the account already held 75,000 of would otherwise read as a mismatch and halt the
run. PLEDGED units count as held (``ZerodhaAdapter.holdings`` adds ``collateral_quantity``),
so pledging is invisible to this strategy. It never sells a watched ETF — the only sales are
of the fund.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from skas_algo.engine.types import Signal, SignalAction
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
        # ---- funding: the fund is SOLD per buy and never bought (module docstring) ----
        fund_source: str | None = "LIQUIDCASE",
        settlement_days: int = 1,
        fund_seed: str = "never",        # "if_empty" = BACKTEST bootstrap only (buys the fund)
        **_ignored,                      # float_parts / funding_buffer_pct of old snapshots
    ):
        self.universe = [str(s).upper() for s in (universe or [])]
        # the mixin's LEDGER only: park mode keeps its T+1 settlement bookkeeping; float 0
        # and no call to _fund_signals, so nothing here ever parks or tops up a float
        self._init_funding("park", fund_source or "LIQUIDCASE", 0.0, settlement_days,
                           0.0, fund_seed, fund_size=initial_capital, fund_size_cap=False)
        self._lot_taken: dict[int, int] = {}      # fund lot id -> units already sold this decision
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
        # tab suggestions this run took over: {suggestion id: "bought" | "queued" | "expired"}
        # — the manager marks them on the tab; kept so one is never bought twice
        self.handed_suggestions: dict[int, str] = {}
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
        THE CASH BUFFER the run spends, so the owner never has to know the fund's value at
        deploy. With the mixin's rule a capital below the fund's value read as zero settled
        cash and every level queued forever. Fund sale proceeds still move the run's cash
        through the engine as usual."""
        return None

    def _float_target_hint(self) -> float:
        return 0.0                                  # no float: the fund is never bought

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

    def _sell_fund(self, ctx, cost: float, today: date, sales: list[Signal]) -> float:
        """Sell whole fund units worth ``cost`` (rounded UP) — one EXIT per lot, FIFO, across
        the lots not already spoken for this decision. Returns the rupees raised (0 = dry)."""
        fund = self.fund_source
        try:
            px = float(ctx.close(fund))
        except KeyError:
            self._alert(f"{fund} has no price today — nothing sold to fund the buy")
            return 0.0
        if px <= 0 or cost <= 0:
            return 0.0
        want = -(-cost // px)                        # ceil, whole units
        left = int(want)
        for lot in list(ctx.lots(fund) or []):
            if left <= 0:
                break
            free = int(lot.units) - int(self._lot_taken.get(lot.id, 0))
            take = min(left, free)
            if take <= 0:
                continue
            self._lot_taken[lot.id] = self._lot_taken.get(lot.id, 0) + take
            sales.append(Signal(symbol=fund, action=SignalAction.EXIT, lot_id=lot.id,
                                quantity=take, reason="fund_source", meta={"tag": "FUND"}))
            left -= take
        sold = int(want) - left
        if sold <= 0:
            self._alert(f"FUND DRY — no {fund} left to sell; buying from cash while it lasts. "
                        f"Top {fund} up in the broker.")
            self._notify_once("fund_dry", today, f"{fund} has nothing left to sell for BIDS — "
                              f"top it up. Buys continue from cash while it lasts.")
            return 0.0
        if left > 0:
            self._alert(f"{fund} covered {sold} of the {int(want)} units a buy needed")
        self._credit(today, sold * px)               # T+1: spendable tomorrow
        return sold * px

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx) -> list[Signal]:
        today = ctx.today() if hasattr(ctx, "today") else date.today()
        if self.last_shop_day == today.isoformat():
            return []                                  # one decision a day
        self._settle(ctx, today)
        # backtest bootstrap only (fund_seed="if_empty"): park the capital in the fund but
        # leave 3 first-level buys of cash, the buffer a live deploy's capital is
        seed = self._maybe_seed(ctx, 3.0 * self.amount)
        if seed:
            return seed
        self._lot_taken: dict[int, int] = {}
        # PRICED = has a live quote today, nothing more. NOT ctx.present_symbols(): live, that
        # also demands `lookback` days of CACHED history, and the VPS cache had no daily bars
        # for these ETFs — run 38's first 15:05 decision (2026-09-29) read all six as "no
        # price" with their live prices on the tile, and bought nothing. BIDS needs today's
        # price only (the ladder's history is its own peak).
        present = set()
        for sym in dict.fromkeys(self._symbols() + list(self.queued)):
            try:
                if float(ctx.close(sym)) > 0:
                    present.add(sym)
            except (KeyError, TypeError, ValueError):
                continue
        symbols = self._symbols()
        rules = self._rules(symbols)
        if self._rules_error:
            self._alert(self._rules_error)
        unpriced = [s for s in symbols if s not in present]
        if unpriced:
            self._alert(f"no live price for {', '.join(unpriced[:6])} — a name must be in the "
                        f"run's symbols to be quoted (a watchlist edit cannot add one)")

        def enabled(sym: str) -> bool:
            if rules is None:
                return True                          # backtest: every symbol, ctor knobs
            r = rules.get(sym)
            return bool(r and r.get("enabled", True))

        buys: list[Signal] = []
        sales: list[Signal] = []
        self.last_fired = []

        def buy(sym: str, rupees: float, close: float, *, funded: bool) -> bool:
            units = max(1, int(rupees // close))      # whole units; one at least
            if not funded:
                self._sell_fund(ctx, units * close, today, sales)   # the fund pays for it
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
            buy(sym, float(self.queued[sym]["amount"]), close, funded=True)   # sold already

        # 1b. HANDOVER: suggestions the tab raised before this run took the holding over. The
        # run carried their fired levels across, so its ladder will not buy them again —
        # left alone they sat on the tab with an Accept button forever (2026-10-01). Bought
        # now, the normal way (fund sold first); expired if the price is back at its high.
        for sym in symbols:
            r = (rules or {}).get(sym) or {}
            todo = [p for p in (r.get("pending") or [])
                    if int(p["id"]) not in self.handed_suggestions]
            if not todo or sym not in present or not enabled(sym) or sym in self.queued:
                continue
            close = float(ctx.close(sym))
            peak = float((self.ladders.get(sym) or {}).get("peak") or r.get("peak") or 0.0)
            if peak and close >= peak:
                for p in todo:
                    self.handed_suggestions[int(p["id"])] = "expired"
                continue
            rupees = sum(float(p["amount"]) for p in todo)
            levels = [int(p["level"]) for p in todo]
            self.last_fired.append({"symbol": sym, "levels": levels, "amount": rupees,
                                    "price": close, "day": today.isoformat(),
                                    "handover": True})
            outcome = "bought" if buy(sym, rupees, close, funded=False) else "queued"
            if outcome == "queued":
                self.queued[sym] = {"amount": rupees, "levels": levels, "funded": True,
                                    "since": today.isoformat()}
            for p in todo:
                self.handed_suggestions[int(p["id"])] = outcome

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
            if not buy(sym, rupees, close, funded=False):
                self.queued[sym] = {"amount": rupees, "levels": levels, "funded": True,
                                    "since": today.isoformat()}

        # (a queued buy is NOT written into the alert here — `strategy_alert` derives that line
        # from the queue and today's date every time it is read; see _queue_note)
        self.last_shop_day = today.isoformat()
        # ORDER: fund sales first, then buys — a rejected BUY halts the run and abandons the
        # rest of the decision, and the sale must never sit behind it
        return sales + buys

    # ------------------------------------------------------------------ the tile's status
    # The banner used to be text written AT the decision and repeated verbatim until the next
    # one — so at 09:56 the day after, it still read "WAITING FOR CASH … retried then" while
    # the sale had settled and the buy was due at 15:05 (owner, 2026-10-01). The decision's
    # own notes stay stored text; the line about a QUEUED buy is computed each time it is
    # read, from the queue, the settlement date and whether today's decision has run.
    @property
    def strategy_alert(self) -> str | None:
        parts = [p for p in (self.__dict__.get("_decision_alert"), self._queue_note()) if p]
        return " · ".join(parts) or None

    @strategy_alert.setter
    def strategy_alert(self, value: str | None) -> None:
        self.__dict__["_decision_alert"] = value

    def _alert(self, message: str) -> None:            # the mixin's, on the stored part only
        base = self.__dict__.get("_decision_alert")
        self.__dict__["_decision_alert"] = message if not base else f"{base} · {message}"

    def _queue_note(self, today: date | None = None) -> str | None:
        if not self.queued:
            return None
        if today is None:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        from skas_algo.live.holidays import next_trading_day

        def day(d: date) -> str:
            return f"{d.day} {d.strftime('%b')}"

        fund = self.fund_source
        decided_today = self.last_shop_day == today.isoformat()
        notes = []
        for sym, q in sorted(self.queued.items()):
            since = date.fromisoformat(str(q.get("since") or today.isoformat()))
            lands = next_trading_day(since, max(1, self.settlement_days))
            what = f"{sym} ₹{float(q['amount']):,.0f}"
            if today < lands:
                notes.append(f"{what}: paid for by the {day(since)} {fund} sale, which settles "
                             f"{day(lands)} — bought at that day's 15:05 decision")
            elif decided_today:
                notes.append(f"{what}: the {fund} sale has settled, but the broker's available "
                             f"cash did not cover it at today's 15:05 decision — retried at "
                             f"the next one")
            else:
                notes.append(f"{what}: the {day(since)} {fund} sale has settled — bought at "
                             f"today's 15:05 decision")
        # the heading says the state: SHORT only after a decision found too little cash
        short = any("did not cover it" in n for n in notes)
        return ("WAITING FOR CASH — " if short else "QUEUED BUY — ") + "; ".join(notes)

    # ------------------------------------------------------------------ (de)serialize
    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.export_state()

    def export_state(self) -> dict[str, Any]:
        return {
            "ladders": {s: dict(v) for s, v in self.ladders.items()},
            "queued": {s: dict(v) for s, v in self.queued.items()},
            "last_shop_day": self.last_shop_day,
            "last_fired": [dict(x) for x in self.last_fired],
            "handed_suggestions": {str(k): v for k, v in self.handed_suggestions.items()},
            **self.funding_state(),
            # the STORED part only — the queue line is derived on read, never persisted
            "strategy_alert": self.__dict__.get("_decision_alert"),
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.ladders = {s: dict(v) for s, v in (state.get("ladders") or {}).items()}
        self.queued = {s: dict(v) for s, v in (state.get("queued") or {}).items()}
        self.last_shop_day = state.get("last_shop_day")
        self.last_fired = [dict(x) for x in (state.get("last_fired") or [])]
        self.handed_suggestions = {int(k): str(v) for k, v in
                                   (state.get("handed_suggestions") or {}).items()}
        self.load_funding_state(state)
        # snapshots written before the queue line was derived stored it as text — drop it, or
        # the banner would carry yesterday's wording beside today's
        base = self.__dict__.get("_decision_alert")
        if base and ("WAITING FOR CASH" in base or "QUEUED BUY" in base):
            kept = [p for p in base.split(" · ") if "WAITING FOR CASH" not in p
                    and "QUEUED BUY" not in p
                    and "sale settles T+1" not in p]
            self.__dict__["_decision_alert"] = " · ".join(kept) or None

    def exit_rules(self) -> list[str]:
        return [
            "Never sells a watched ETF — the only sales are of the fund",
            f"Buys each further {self.dip_pct:g}% below the peak: level k invests k × "
            f"₹{self.amount:,.0f}, up to {self.max_levels} levels; a new high resets "
            "(per-holding rules from Portfolio → BIDS outrank these when live)",
            f"Each buy sells {self.fund_source} worth what it costs, in the same decision; "
            f"{self.fund_source} is never bought. Today's buy is paid from the run's cash and "
            f"the sale replaces it T+1",
        ]
