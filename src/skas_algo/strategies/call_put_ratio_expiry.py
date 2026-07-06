"""call_put_ratio_expiry — expiry-day 1:3 premium-ratio seller (NIFTY + SENSEX).

Trades ONLY on each index's own weekly expiry day (NIFTY Tuesday, SENSEX Thursday), once,
in the 09:20–09:27 window (video ref: https://www.youtube.com/watch?v=iorriHcOpdU):

  * BUY 1 lot ATM CE + 1 lot ATM PE (per set);
  * from the ATM PE premium x, SELL 3 lots of the put strike trading nearest x/3;
  * from the ATM CE premium y, SELL 3 lots of the call strike trading nearest y/3.

Net per side: +1 ATM / −3 far OTM → net SHORT 2 lots beyond the ⅓ strikes — losses run
open out there; the margin-based stop is the only guard. Exits: profit ≥ 1.1% of margin
deployed, loss ≥ 1% of margin, or 15:20 — never carries (0DTE settles the same evening
anyway; engine settlement is the backstop).

Design notes:
- **Strike selection needs the LIVE chain** (`ctx.market.live_chain`, getattr-guarded —
  the donchian 30Δ precedent): the ⅓-premium placement is smile-driven, which is also why
  there is deliberately NO backtest (flat-vol BS would systematically misplace the
  strikes; paper-first validation instead — CLAUDE.md).
- **margin_base tracks the BROKER basket margin ONLY** (owner rule — the model reads
  ~2× real and would double the rupee thresholds): the live manager pushes the throttled
  Zerodha basket margin via ``set_broker_margin`` within ~a tick of the fill; the base
  freezes on the first push and the target/stop checks WAIT until then (the 15:20 exit
  is time-based and never waits).
- One entry per underlying per expiry day (`traded_day` guard, persisted); missing the
  window (deploy at 09:30, chain hiccups through 09:27) skips the day — never a late chase.
"""

from __future__ import annotations

from datetime import date, time

from skas_algo.engine.options.contract_specs import expiry_weekday_for, lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close


def _hhmm(s: str, fallback: time) -> time:
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return fallback


class CallPutRatioExpiryStrategy:
    strategy_id = "call_put_ratio_expiry"
    intraday = True  # ticks every refresh_seconds; entry window + exits self-gate

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 500_000,
        underlyings: list[str] | None = None,
        sets: dict | int | None = None,          # 1 set = buy 1 + sell 3 per side
        entry_start: str = "09:20",
        entry_end: str = "09:27",
        eod_exit: str = "15:20",
        profit_target_pct: float = 1.1,          # % of margin_base
        stop_loss_pct: float = 1.0,              # % of margin_base
        ratio_divisor: float = 3.0,              # sell strike trades at ATM premium / this
        ratio_tolerance_pct: float = 30.0,       # best candidate further off → skip the day
        sell_lots_per_set: int = 3,
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlyings = [u.upper() for u in (underlyings or ["NIFTY"])]
        if isinstance(sets, dict):
            self.sets = {u.upper(): max(1, int(v)) for u, v in sets.items()}
        else:
            self.sets = {u: max(1, int(sets or 1)) for u in self.underlyings}
        self.entry_start = _hhmm(entry_start, time(9, 20))
        self.entry_end = _hhmm(entry_end, time(9, 27))
        self.eod_exit = _hhmm(eod_exit, time(15, 20))
        # Whole percents of margin_base; instance names avoid the generic snapshot's
        # fraction convention (see delta_neutral_monthly for the full note).
        self.target_pct = float(profit_target_pct)
        self.stop_pct = float(stop_loss_pct)
        self.ratio_divisor = float(ratio_divisor)
        self.ratio_tolerance_pct = float(ratio_tolerance_pct)
        self.sell_lots_per_set = int(sell_lots_per_set)
        self.min_leg_oi = int(min_leg_oi)
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # ---- per-underlying state (all persisted) ----
        self.legs: dict[str, list[dict]] = {u: [] for u in self.underlyings}
        self.margin_base: dict[str, float] = {u: 0.0 for u in self.underlyings}
        self.margin_source: dict[str, str] = {u: "" for u in self.underlyings}
        self.traded_day: dict[str, str | None] = {u: None for u in self.underlyings}
        # Latest broker basket margin pushed by the live manager (not persisted; NOTE it
        # covers the WHOLE book — with both underlyings entered the same day the base is
        # shared, which only happens if NIFTY's Tuesday coincides with a SENSEX Thursday
        # holiday-shift: acceptable).
        self._broker_margin: float | None = None

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return list(self.underlyings)

    def set_broker_margin(self, value: float) -> None:
        """Manager push: the real broker basket margin for our current legs."""
        if value and value > 0:
            self._broker_margin = float(value)

    # ---------------------------------------------------------------- expiry
    def _is_expiry_day(self, ctx, u: str, today: date) -> bool:
        """Today is this underlying's weekly expiry: nearest listed expiry == today
        (live chain expiries when available), else the calendar weekday."""
        chain = ctx.option_chain()
        if chain is not None:
            try:
                listed = [date.fromisoformat(str(e)[:10])
                          for e in chain.expiries(u, today)]
                nearest = min((e for e in listed if e >= today), default=None)
                if nearest is not None:
                    return nearest == today
            except Exception:  # pragma: no cover - fall through to the calendar
                pass
        wd = expiry_weekday_for(u, today, "weekly")
        return wd is not None and today.weekday() == wd

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now = ctx.now()
        today = ctx.today()
        signals: list[Signal] = []
        for u in self.underlyings:
            live = self._live_legs(ctx, u)
            if live:
                signals += self._manage(ctx, u, live, now)
            elif (self.traded_day.get(u) != today.isoformat()
                    and self.entry_start <= now.time() <= self.entry_end
                    and self._is_expiry_day(ctx, u, today)):
                signals += self._try_enter(ctx, u, today)
        return signals

    def _live_legs(self, ctx, u: str) -> list[dict]:
        """Legs the engine still holds; a settled/closed book clears our state."""
        legs = self.legs[u]
        if not legs:
            return []
        if not any(ctx.lots(leg["symbol"]) for leg in legs):
            self.legs[u] = []
            return []
        return legs

    # ----------------------------------------------------------------- entry
    def _try_enter(self, ctx, u: str, today: date) -> list[Signal]:
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(u, today.isoformat()) if chain_fn else None
        if not chain or not chain.get("rows") or not chain.get("atm_strike"):
            return []  # no live chain this tick — retry within the window
        atm = float(chain["atm_strike"])
        per_lot = int(chain.get("lot_size") or 0)
        if per_lot <= 0:
            try:
                per_lot = lot_size_for(u, today, overrides=self.lot_overrides)
            except KeyError:
                return []

        def ltp(row: dict | None) -> float | None:
            v = (row or {}).get("ltp")
            return None if v is None or bad_close(v) else float(v)

        def oi_ok(row: dict | None) -> bool:
            return int((row or {}).get("oi") or 0) >= self.min_leg_oi

        rows = {float(r["strike"]): r for r in chain["rows"]}
        atm_row = rows.get(atm)
        if atm_row is None:
            return []
        y = ltp(atm_row.get("ce"))
        x = ltp(atm_row.get("pe"))
        if x is None or y is None or not oi_ok(atm_row.get("ce")) or not oi_ok(atm_row.get("pe")):
            return []

        # ⅓-premium strikes: nearest-by-LTP on the SAME side, strictly OTM of ATM.
        ce_pick = self._ratio_strike(rows, atm, y / self.ratio_divisor, "ce", ltp, oi_ok,
                                     otm=lambda k: k > atm)
        pe_pick = self._ratio_strike(rows, atm, x / self.ratio_divisor, "pe", ltp, oi_ok,
                                     otm=lambda k: k < atm)
        if ce_pick is None or pe_pick is None:
            # Tolerance miss (thin chain / gap open) → done for the day, per the plan.
            self.traded_day[u] = today.isoformat()
            return []
        (ce_k, ce_prem), (pe_k, pe_prem) = ce_pick, pe_pick

        n = self.sets.get(u, 1)
        buy_units = float(n * per_lot)
        sell_units = float(n * self.sell_lots_per_set * per_lot)
        mk = lambda k, right: make(u, today, float(k), right, lot_size=per_lot,  # noqa: E731
                                   lot_overrides=self.lot_overrides).symbol
        legs = [
            {"symbol": mk(atm, "CE"), "dir": 1, "units": buy_units, "entry": y},
            {"symbol": mk(atm, "PE"), "dir": 1, "units": buy_units, "entry": x},
            {"symbol": mk(ce_k, "CE"), "dir": -1, "units": sell_units, "entry": ce_prem},
            {"symbol": mk(pe_k, "PE"), "dir": -1, "units": sell_units, "entry": pe_prem},
        ]

        # margin_base: BROKER basket margin only (owner rule). It isn't computable
        # until the legs exist at the broker — the manager pushes it within ~a tick;
        # target/stop wait for it (EOD exit is time-based and doesn't).
        self.margin_base[u] = 0.0
        self.margin_source[u] = "pending"

        self.legs[u] = legs
        self.traded_day[u] = today.isoformat()
        reason = "cpre_entry"
        return [
            Signal(leg["symbol"],
                   SignalAction.ENTER_LONG if leg["dir"] > 0 else SignalAction.ENTER_SHORT,
                   quantity=int(leg["units"]), reason=reason, meta={"multiplier": 1})
            for leg in legs
        ]

    def _ratio_strike(self, rows, atm, target, side, ltp, oi_ok, otm):
        """(strike, ltp) whose premium is nearest ``target`` among OTM rows; None when the
        best is off by more than ratio_tolerance_pct."""
        best = None
        for k, r in rows.items():
            if not otm(k):
                continue
            leg = r.get(side)
            prem = ltp(leg)
            if prem is None or not oi_ok(leg):
                continue
            err = abs(prem - target)
            if best is None or err < best[0]:
                best = (err, k, prem)
        if best is None or target <= 0:
            return None
        if best[0] / target * 100.0 > self.ratio_tolerance_pct:
            return None
        return best[1], best[2]

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, u: str, legs: list[dict], now) -> list[Signal]:
        if now.time() >= self.eod_exit:
            return self._exit_all(u, legs, "eod_1520")
        if self.margin_source.get(u) != "broker":
            if not self._broker_margin:
                return []  # thresholds wait for the broker number (never the model)
            self.margin_base[u] = self._broker_margin
            self.margin_source[u] = "broker"
        base = self.margin_base.get(u) or 0.0
        if base <= 0:
            return []
        has_print = getattr(ctx.market, "has_print", None)
        pnl = 0.0
        for leg in legs:
            if has_print is not None and not has_print(leg["symbol"]):
                return []  # a leg hasn't ticked yet — don't judge on a stale mark
            try:
                cur = ctx.close(leg["symbol"])
            except KeyError:
                return []
            pnl += (cur - leg["entry"]) * leg["units"] * leg["dir"]
        if pnl >= base * self.target_pct / 100.0:
            return self._exit_all(u, legs, "target")
        if pnl <= -base * self.stop_pct / 100.0:
            return self._exit_all(u, legs, "stop")
        return []

    def _exit_all(self, u: str, legs: list[dict], reason: str) -> list[Signal]:
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in legs]
        self.legs[u] = []
        return sigs

    # ------------------------------------------------------------ snapshot hooks
    def exit_amounts(self) -> tuple[float | None, float | None]:
        bases = [b for u, b in self.margin_base.items()
                 if b > 0 and self.margin_source.get(u) == "broker"]
        if not bases:
            return None, None
        base = sum(bases)
        return base * self.target_pct / 100.0, base * self.stop_pct / 100.0

    def exit_rules(self) -> list[str]:
        return [
            f"Book profit at +{self.target_pct:g}% of broker margin",
            f"Stop out at −{self.stop_pct:g}% of broker margin",
            f"Hard exit {self.eod_exit.strftime('%H:%M')} — never carried",
        ]

    # --------------------------------------------------------------- monitor
    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        names = []
        for u in self.underlyings:
            today = market.current_date
            names.append({
                "name": u,
                "spot": getattr(market, "index_spot", lambda _u: None)(u),
                "traded_today": self.traded_day.get(u) == today.isoformat(),
                "legs": [dict(leg) for leg in self.legs[u]],
                "margin_base": self.margin_base.get(u),
                "margin_source": self.margin_source.get(u),
                "target_amt": (self.margin_base.get(u) or 0) * self.target_pct / 100,
                "stop_amt": (self.margin_base.get(u) or 0) * self.stop_pct / 100,
            })
        return {"kind": "cp_ratio_expiry", "names": names}

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "legs": {u: [dict(x) for x in v] for u, v in self.legs.items()},
            "margin_base": dict(self.margin_base),
            "margin_source": dict(self.margin_source),
            "traded_day": dict(self.traded_day),
        }

    def load_state(self, state: dict) -> None:
        for u in self.underlyings:
            self.legs[u] = [dict(x) for x in (state.get("legs", {}).get(u) or [])]
            self.margin_base[u] = float(state.get("margin_base", {}).get(u, 0.0))
            self.margin_source[u] = state.get("margin_source", {}).get(u, "")
            self.traded_day[u] = state.get("traded_day", {}).get(u)

