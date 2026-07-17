"""broker_smoke_test — a deliberate, tiny, end-to-end REAL-order-path probe.

BUY the smallest possible position, hold ~a minute, SELL, then STOP the run itself.
Two legs, deployed as SEPARATE runs (one strategy class detects its mode):
  * ``leg="option"`` (DERIV deploy): 1 lot of a cheap far-OTM weekly option — the strike
    whose premium is nearest ``target_premium`` inside [premium_min, premium_max] (bounds
    the cash at risk regardless of vol) with an OI floor. The wide OTM spread makes the
    LIMIT-at-touch → 10s → MARKET escalation fire naturally — that's a feature: one cycle
    exercises place, poll, modify/escalate, fill, book-sync, reconcile, exit.
  * ``leg="stock"`` (STOCK deploy): 1 share of ``symbol`` (default ITC) — same LiveBroker,
    different exchange route (NSE equity vs NFO options).

Deliberately NOT configurable upward: always 1 lot / 1 share. This is a connectivity +
order-path test (built after the 2026-07-17 paper-flatten incident), not a trading
strategy — every existing rail (per-order notional cap, daily order cap, market hours,
the §1 four-key arming gate) applies unchanged, and per §1 the LIVE deploy and every
real cycle is the OWNER's hand; tests use fake adapters only.

Lifecycle (owner choice 2026-07-18): one cycle then the run STOPS ITSELF — the strategy
sets ``stop_requested`` once its book is flat again and the manager loop honors it
(``_maybe_self_stop``). A restart mid-hold recovers the held leg and still exits on
schedule (which quietly doubles as a recovery drill). Deploy-only, no backtest
(a paper smoke test proves nothing — paper fills always "work").
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from skas_algo.engine.options.contract_specs import expiry_weekday_for, lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close, legs_mtm_pnl


class BrokerSmokeTestStrategy:
    strategy_id = "broker_smoke_test"
    intraday = True  # tick-driven in BOTH deploy modes (a STOCK run must not wait for 15:20)

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 50_000,
        leg: str = "option",             # "option" (1 lot OTM weekly) | "stock" (1 share)
        underlying: str | None = None,   # option leg
        symbol: str = "ITC",             # stock leg
        right: str = "CE",               # option leg: CE or PE
        hold_seconds: int = 60,          # buy → hold this long → sell
        target_premium: float = 10.0,    # pick the strike trading nearest this…
        premium_min: float = 5.0,        # …within this band (bounds cash at risk)
        premium_max: float = 20.0,
        min_leg_oi: int = 100,           # skip dead strikes (a no-OI fill proves nothing)
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.leg = str(leg)
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.symbol = str(symbol).upper()
        self.right = "PE" if str(right).upper() == "PE" else "CE"
        self.hold_seconds = max(15, int(hold_seconds))
        self.target_premium = float(target_premium)
        self.premium_min = float(premium_min)
        self.premium_max = float(premium_max)
        self.min_leg_oi = int(min_leg_oi)
        self.lot_overrides = lot_overrides
        self.initial_capital = initial_capital

        # ---- state (persisted — a restart mid-hold must still exit) ----
        self.legs: list[dict] = []
        self.entry_at: datetime | None = None
        self.exited: bool = False          # exit signalled; waiting to see the book flat
        self.stop_requested: bool = False  # flat again → the manager loop stops the run
        self.strategy_alert: str | None = None

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return [self.underlying] if self.leg == "option" else []

    def strategy_pnl(self, closes: dict) -> float | None:
        return legs_mtm_pnl(self.legs, closes)

    def exit_rules(self) -> list[str]:
        return [
            f"Sell after a {self.hold_seconds}s hold (checked every tick)",
            "One cycle only — the run stops itself once flat",
        ]

    # ---------------------------------------------------------------- expiry
    def _nearest_expiry(self, ctx, today: date) -> date | None:
        chain = ctx.option_chain()
        if chain is not None:
            try:
                listed = [date.fromisoformat(str(e)[:10])
                          for e in chain.expiries(self.underlying, today)]
                nearest = min((e for e in listed if e >= today), default=None)
                if nearest is not None:
                    return nearest
            except Exception:  # pragma: no cover - fall through to the calendar
                pass
        wd = expiry_weekday_for(self.underlying, today, "weekly")
        if wd is None:
            return None
        return today + timedelta(days=(wd - today.weekday()) % 7)

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now = ctx.now()
        if self.stop_requested:
            return []  # done — waiting for the manager loop to stop the run
        if self.legs:
            if not any(ctx.lots(leg["symbol"]) for leg in self.legs):
                # Book flat again (our exit filled, or settlement) → the test is complete.
                self.legs = []
                self.stop_requested = True
                return []
            return self._maybe_exit(now)
        if self.exited:
            # Exit signalled but legs already cleared → flat; stop next pass.
            self.stop_requested = True
            return []
        return self._enter(ctx, now)

    def _maybe_exit(self, now: datetime) -> list[Signal]:
        if self.entry_at is None:
            self.entry_at = now  # defensive: recovered without a timestamp → start the clock
            return []
        entry_at = self.entry_at
        if now.tzinfo is not None and entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=now.tzinfo)
        elif now.tzinfo is None and entry_at.tzinfo is not None:
            entry_at = entry_at.replace(tzinfo=None)
        if (now - entry_at).total_seconds() < self.hold_seconds:
            return []
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason="smoke_exit")
                for leg in self.legs]
        self.legs = []
        self.exited = True
        return sigs

    # ----------------------------------------------------------------- entry
    def _enter(self, ctx, now: datetime) -> list[Signal]:
        if self.leg == "stock":
            return self._enter_stock(ctx, now)
        return self._enter_option(ctx, now)

    def _enter_stock(self, ctx, now: datetime) -> list[Signal]:
        try:
            px = ctx.close(self.symbol)
        except KeyError:
            self.strategy_alert = f"no quote for {self.symbol} yet — retrying"
            return []
        if bad_close(px):
            return []
        self.strategy_alert = None
        self.legs = [{"symbol": self.symbol, "dir": 1, "units": 1.0, "entry": float(px)}]
        self.entry_at = now
        return [Signal(self.symbol, SignalAction.ENTER_LONG, quantity=1, reason="smoke_entry")]

    def _enter_option(self, ctx, now: datetime) -> list[Signal]:
        today = ctx.today()
        expiry = self._nearest_expiry(ctx, today)
        if expiry is None:
            self.strategy_alert = "no weekly expiry resolvable — retrying"
            return []
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(self.underlying, expiry.isoformat()) if chain_fn else None
        if not chain or not chain.get("rows"):
            self.strategy_alert = "no LIVE chain — a broker quote source is required"
            return []
        side = self.right.lower()
        best = None  # (premium distance from target, strike, ltp)
        for r in chain["rows"]:
            leg_row = r.get(side)
            ltp = (leg_row or {}).get("ltp")
            if ltp is None or bad_close(ltp):
                continue
            if not (self.premium_min <= float(ltp) <= self.premium_max):
                continue
            if int((leg_row or {}).get("oi") or 0) < self.min_leg_oi:
                continue
            err = abs(float(ltp) - self.target_premium)
            if best is None or err < best[0]:
                best = (err, float(r["strike"]), float(ltp))
        if best is None:
            self.strategy_alert = (
                f"no {self.right} in the ₹{self.premium_min:g}–{self.premium_max:g} premium "
                f"band with OI ≥ {self.min_leg_oi} on {expiry} — retrying")
            return []
        _, strike, ltp = best
        per_lot = int(chain.get("lot_size") or 0)
        if per_lot <= 0:
            try:
                per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
            except KeyError:
                self.strategy_alert = f"unknown lot size for {self.underlying} — cannot size 1 lot"
                return []
        self.strategy_alert = None
        sym = make(self.underlying, expiry, strike, self.right, lot_size=per_lot,
                   lot_overrides=self.lot_overrides).symbol
        self.legs = [{"symbol": sym, "dir": 1, "units": float(per_lot), "entry": ltp}]
        self.entry_at = now
        return [Signal(sym, SignalAction.ENTER_LONG, quantity=per_lot,
                       reason="smoke_entry", meta={"multiplier": 1})]

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "leg": self.leg,
            "legs": list(self.legs),
            "entry_at": self.entry_at.isoformat() if self.entry_at else None,
            "exited": self.exited,
            "stop_requested": self.stop_requested,
        }

    def load_state(self, state: dict) -> None:
        self.leg = state.get("leg", self.leg)
        self.legs = list(state.get("legs", []))
        ea = state.get("entry_at")
        self.entry_at = datetime.fromisoformat(ea) if ea else None
        self.exited = bool(state.get("exited", False))
        self.stop_requested = bool(state.get("stop_requested", False))
