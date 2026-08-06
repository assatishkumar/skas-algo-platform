"""straddle_btst — a BTST LONG straddle: the reverse of intraday_straddle.

BUY the ATM CE + PE near the close (``entry_time``, ~15:20), hold OVERNIGHT, sell on the
next trading session at ``exit_time`` (~09:20). The bet: the overnight gap + any IV
firming at the open is worth more than the overnight theta paid. Both times configurable.

Contract rule: the nearest listed expiry STRICTLY AFTER today — a BTST position must
survive the night, so on expiry day the entry rolls to the next weekly automatically
(that also makes Monday's entry the juiciest theta bill: 1-DTE into Tuesday's expiry).

Risk shape: LONG-only — the loss is capped at the premium paid, so there is no margin
dependence anywhere: the optional ``stop_loss_pct`` / ``profit_target_pct`` are % of the
PREMIUM PAID (0 = off, the ctor default; a pure time-window strategy needs neither).
"Friday's" entry simply exits on the next session that ticks (Monday), so weekends and
holidays need no calendar math.

Deploy-only live (ATM selection needs the live chain) + replayable on the 1-min option
store like the rest of the intraday family (mirrors intraday_straddle's ctx surface).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from skas_algo.engine.options.contract_specs import expiry_weekday_for, lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import ExitCadenceMixin, bad_close, legs_mtm_pnl


def _hhmm(s: str, fallback: time) -> time:
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return fallback


class StraddleBtstStrategy(ExitCadenceMixin):
    strategy_id = "straddle_btst"
    intraday = True  # ticks every refresh_seconds; entry window + exits self-gate

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,
        entry_time: str = "15:20",
        entry_window_end: str = "15:35",  # latest a (re)deploy can still enter for the day
        exit_time: str = "09:20",         # NEXT session's sell time
        stop_loss_pct: float = 0.0,       # % of premium paid; 0 = off (loss is capped anyway)
        profit_target_pct: float = 0.0,   # % of premium paid; 0 = off
        min_leg_oi: int = 1,
        r: float = 0.065,
        profit_check: str = "tick",
        stop_check: str = "tick",
        eod_time: str = "15:20",
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.lots = max(1, int(lots))
        self.entry_time = _hhmm(entry_time, time(15, 20))
        self.entry_window_end = _hhmm(entry_window_end, time(15, 35))
        self.exit_time = _hhmm(exit_time, time(9, 20))
        self.stop_loss_pct = float(stop_loss_pct or 0.0)
        self.profit_target_pct = float(profit_target_pct or 0.0)
        self.min_leg_oi = int(min_leg_oi)
        self.r = float(r)
        self.profit_check = str(profit_check)
        self.stop_check = str(stop_check)
        self.eod_time = str(eod_time)
        self.lot_overrides = lot_overrides
        self.initial_capital = initial_capital

        # ---- state (all persisted) ----
        self.legs: list[dict] = []
        self.entered_day: str | None = None   # latch: one entry per day
        self.premium_paid: float = 0.0        # ₹ at entry — the stop/target base
        self.force_pending: bool = False

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return [self.underlying]

    def strategy_pnl(self, closes: dict) -> float | None:
        return legs_mtm_pnl(self.legs, closes)

    def request_force_entry(self) -> str:
        self.force_pending = True
        return "next tick buys the straddle (bypasses the entry window + the once-a-day latch)"

    # ---------------------------------------------------------------- expiry
    def _btst_expiry(self, ctx, today: date) -> date | None:
        """Nearest listed expiry STRICTLY AFTER today — the position must live overnight."""
        chain = ctx.option_chain()
        if chain is not None:
            try:
                listed = [date.fromisoformat(str(e)[:10])
                          for e in chain.expiries(self.underlying, today)]
                nearest = min((e for e in listed if e > today), default=None)
                if nearest is not None:
                    return nearest
            except Exception:  # pragma: no cover - fall through to the calendar
                pass
        wd = expiry_weekday_for(self.underlying, today, "weekly")
        if wd is None:
            return None
        days = (wd - today.weekday()) % 7
        return today + timedelta(days=days or 7)  # today's own expiry rolls a week out

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now = ctx.now()
        today = ctx.today()
        live = self._live_legs(ctx)
        if live:
            return self._manage(ctx, live, now, today)
        if self.force_pending:
            got = self._try_enter(ctx, now, today)
            if got:
                self.force_pending = False
            return got
        if self.entered_day == today.isoformat():
            return []
        if self.entry_time <= now.time() <= self.entry_window_end:
            return self._try_enter(ctx, now, today)
        return []

    def _live_legs(self, ctx) -> list[dict]:
        if not self.legs:
            return []
        if not any(ctx.lots(leg["symbol"]) for leg in self.legs):
            self.legs = []  # engine settled/closed everything
            return []
        return self.legs

    # ------------------------------------------------------------ chain helpers
    def _ltp(self, row: dict | None) -> float | None:
        v = (row or {}).get("ltp")
        return None if v is None or bad_close(v) else float(v)

    def _oi_ok(self, row: dict | None) -> bool:
        return int((row or {}).get("oi") or 0) >= self.min_leg_oi

    def _atm_strike(self, rows: dict[float, dict], spot: float) -> float | None:
        best = None
        for k, r in rows.items():
            if not (self._oi_ok(r.get("ce")) and self._oi_ok(r.get("pe"))):
                continue
            if self._ltp(r.get("ce")) is None or self._ltp(r.get("pe")) is None:
                continue
            err = abs(k - spot)
            if best is None or err < best[0]:
                best = (err, k)
        return best[1] if best else None

    # ----------------------------------------------------------------- entry
    def _try_enter(self, ctx, now: datetime, today: date) -> list[Signal]:
        expiry = self._btst_expiry(ctx, today)
        if expiry is None:
            return []
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(self.underlying, expiry.isoformat()) if chain_fn else None
        if not chain or not chain.get("rows"):
            return []  # no chain this tick — retry within the window
        rows = {float(r["strike"]): r for r in chain["rows"]}
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = chain.get("spot") or (spot_fn(self.underlying) if spot_fn else None)
        if spot is None or bad_close(spot):
            return []
        per_lot = int(chain.get("lot_size") or 0)
        if per_lot <= 0:
            try:
                per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
            except KeyError:
                return []
        k = self._atm_strike(rows, float(spot))
        if k is None:
            return []
        ce_ltp = self._ltp(rows.get(float(k), {}).get("ce"))
        pe_ltp = self._ltp(rows.get(float(k), {}).get("pe"))
        if ce_ltp is None or pe_ltp is None:
            return []

        units = float(self.lots * per_lot)

        def sym(right: str) -> str:
            return make(self.underlying, expiry, float(k), right, lot_size=per_lot,
                        lot_overrides=self.lot_overrides).symbol

        self.legs = [
            {"symbol": sym("CE"), "dir": 1, "units": units, "entry": ce_ltp},
            {"symbol": sym("PE"), "dir": 1, "units": units, "entry": pe_ltp},
        ]
        self.premium_paid = (ce_ltp + pe_ltp) * units
        self.entered_day = today.isoformat()
        return [
            Signal(leg["symbol"], SignalAction.ENTER_LONG, quantity=int(leg["units"]),
                   reason="btst_entry", meta={"multiplier": 1})
            for leg in self.legs
        ]

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, legs: list[dict], now, today: date) -> list[Signal]:
        # Hard time exit: the first tick of a LATER session at/after exit_time. Never
        # gated on prints/cadence — the whole strategy is "be flat by 09:20".
        if self.entered_day and today.isoformat() > self.entered_day and \
                now.time() >= self.exit_time:
            return self._exit_all(legs, "btst_exit")
        if self.stop_loss_pct <= 0 and self.profit_target_pct <= 0:
            return []
        if self.premium_paid <= 0:
            return []
        has_print = getattr(ctx.market, "has_print", None)
        pnl = 0.0
        for leg in legs:
            if has_print is not None and not has_print(leg["symbol"]):
                return []  # a leg hasn't ticked — don't judge on a stale mark
            try:
                cur = ctx.close(leg["symbol"])
            except KeyError:
                return []
            pnl += (cur - leg["entry"]) * leg["units"] * leg["dir"]
        pnl_pct = 100.0 * pnl / self.premium_paid
        if self._due("profit", now) and self.profit_target_pct > 0 \
                and pnl_pct >= self.profit_target_pct:
            return self._exit_all(legs, "target")
        if self._due("stop", now) and self.stop_loss_pct > 0 \
                and pnl_pct <= -self.stop_loss_pct:
            return self._exit_all(legs, "stop")
        return []

    def _exit_all(self, legs: list[dict], reason: str) -> list[Signal]:
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in legs]
        self.legs = []
        return sigs

    # ------------------------------------------------------------ snapshot hooks
    def exit_amounts(self) -> tuple[float | None, float | None]:
        if self.premium_paid <= 0 or not self.legs:
            return None, None
        tgt = self.premium_paid * self.profit_target_pct / 100.0 \
            if self.profit_target_pct > 0 else None
        stp = self.premium_paid * self.stop_loss_pct / 100.0 \
            if self.stop_loss_pct > 0 else None
        return tgt, stp

    def exit_rules(self) -> list[str]:
        rules = [f"Sell next session at {self.exit_time.strftime('%H:%M')} (hard exit)"]
        if self.profit_target_pct > 0:
            rules.append(f"Target +{self.profit_target_pct:g}% of premium paid "
                         f"({self._cadence_phrase('profit')})")
        if self.stop_loss_pct > 0:
            rules.append(f"Stop −{self.stop_loss_pct:g}% of premium paid "
                         f"({self._cadence_phrase('stop')})")
        rules.append("Loss capped at the premium paid (long-only book)")
        return rules

    # --------------------------------------------------------------- monitor
    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        today = market.current_date
        return {"kind": "straddle", "names": [{
            "name": self.underlying,
            "spot": getattr(market, "index_spot", lambda _u: None)(self.underlying),
            "traded_today": self.entered_day == today.isoformat(),
            "legs": [dict(leg) for leg in self.legs],
            "premium_paid": self.premium_paid,
        }]}

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "legs": [dict(x) for x in self.legs],
            "entered_day": self.entered_day,
            "premium_paid": self.premium_paid,
            "force_pending": self.force_pending,
        }

    def load_state(self, state: dict) -> None:
        self.legs = [dict(x) for x in (state.get("legs") or [])]
        self.entered_day = state.get("entered_day")
        self.premium_paid = float(state.get("premium_paid", 0.0))
        self.force_pending = bool(state.get("force_pending", False))
