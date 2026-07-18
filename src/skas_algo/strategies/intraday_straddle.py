"""intraday_straddle — a configurable intraday short straddle (NIFTY / BANKNIFTY).

Sell an ATM CE + PE at ~09:18 on the nearest weekly (0DTE on expiry day), exit ~15:25.
Two stops, both configurable:
  * a FIXED stop at ``stop_loss_pct`` of the broker basket margin, and
  * a TRAILING stop that only ever tightens up as profit grows —
      - ``trail_mode="ratchet"``: each +``trail_trigger_pct`` of PEAK profit lifts the stop by
        +``trail_step_pct`` (peak +4% → breakeven, +6% → locks +1%);
      - ``trail_mode="below_peak"``: once peak ≥ ``trail_trigger_pct``, stop = peak − ``trail_step_pct``.
    Trailing is off when either trail pct is 0.

Structure is a true straddle by default (both legs at the ATM strike); set ``strike_delta``
(e.g. 0.6) to sell slightly-ITM legs picked by per-share BS |delta| instead.

Design notes (mirrors call_put_ratio_expiry — the closest template):
- Strike selection needs the LIVE chain (``ctx.market.live_chain``) → DEPLOY-ONLY, broker
  source required; there is NO backtest (the EOD-slice engine can't model an intraday
  straddle's stop/trailing — paper-first validation instead, CLAUDE.md §1).
- ``margin_base`` tracks the BROKER basket margin ONLY (owner rule — the model reads ~2×);
  the manager pushes it via ``set_broker_margin`` within ~a tick of the fill; the stop
  checks WAIT for it (the 15:25 time exit never waits).
- One entry per day (``entered_day`` latch, persisted); a stopped-out day does not re-enter.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import floor

from skas_algo.engine.options import black_scholes as bs
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


class IntradayStraddleStrategy(ExitCadenceMixin):
    strategy_id = "intraday_straddle"
    intraday = True  # ticks every refresh_seconds; entry window + exits self-gate

    _EXPIRY_CUTOFF = time(15, 30)

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,
        strike_delta: float = 0.0,        # 0 = ATM straddle; else target per-share |delta| (~0.6 = slight ITM)
        entry_time: str = "09:18",
        entry_window_end: str = "15:00",  # latest a (re)deploy can still enter for the day
        exit_time: str = "15:25",
        stop_loss_pct: float = 2.0,       # fixed SL, % of broker margin_base
        trail_trigger_pct: float = 1.0,   # every this much PEAK profit moves the stop
        trail_step_pct: float = 0.5,      # ...by this much (0 on either disables trailing)
        trail_mode: str = "ratchet",      # "ratchet" | "below_peak"
        min_leg_oi: int = 1,
        r: float = 0.065,
        # Two-cadence model (2026-07-18): how often the trail (profit protection) and the
        # stop COMPARISON are sampled. "tick" = every call — the exact pre-cadence
        # behavior, so a recovered deploy is byte-identical (§1); the deploy/backtest
        # forms default these to "1min".
        profit_check: str = "tick",
        stop_check: str = "tick",
        eod_time: str = "15:20",         # what "eod" means for the cadences (not the exit)
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.lots = max(1, int(lots))
        self.strike_delta = float(strike_delta or 0.0)
        self.entry_time = _hhmm(entry_time, time(9, 18))
        self.entry_window_end = _hhmm(entry_window_end, time(15, 0))
        self.exit_time = _hhmm(exit_time, time(15, 25))
        self.stop_loss_pct = float(stop_loss_pct)
        self.trail_trigger_pct = float(trail_trigger_pct)
        self.trail_step_pct = float(trail_step_pct)
        self.trail_mode = str(trail_mode or "ratchet")
        self.min_leg_oi = int(min_leg_oi)
        self.r = float(r)
        self.profit_check = str(profit_check)
        self.stop_check = str(stop_check)
        self.eod_time = str(eod_time)
        self.lot_overrides = lot_overrides
        self.initial_capital = initial_capital

        # ---- state (all persisted) ----
        self.legs: list[dict] = []
        self.entered_day: str | None = None
        self.margin_base: float = 0.0
        self.margin_source: str = ""
        self.peak_pct: float = 0.0        # high-water MTM P&L (% of margin_base) — drives the trail
        self.force_pending: bool = False
        self._broker_margin: float | None = None  # not persisted; re-pushed after recovery

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return [self.underlying]

    def set_broker_margin(self, value: float) -> None:
        if value and value > 0:
            self._broker_margin = float(value)

    def strategy_pnl(self, closes: dict) -> float | None:
        """The MTM measure the stop/trail compares (decision-entry basis)."""
        return legs_mtm_pnl(self.legs, closes)

    def request_force_entry(self) -> str:
        """Live-page 'Force entry now': the next tick sells the ATM straddle even outside the
        entry window and after today's one-a-day latch. Persisted so a restart keeps it."""
        self.force_pending = True
        return "next tick sells the straddle (bypasses the entry window + the once-a-day latch)"

    # ---------------------------------------------------------------- expiry
    def _nearest_expiry(self, ctx, today: date) -> date | None:
        """Nearest listed weekly ≥ today (live-chain expiries when available, else calendar)."""
        chain = ctx.option_chain()
        if chain is not None:
            try:
                listed = [date.fromisoformat(str(e)[:10]) for e in chain.expiries(self.underlying, today)]
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
        today = ctx.today()
        live = self._live_legs(ctx)
        if live:
            return self._manage(ctx, live, now)
        if self.force_pending:  # bypasses the daily latch + the window
            got = self._try_enter(ctx, now, today)
            if got:
                self.force_pending = False
            return got
        if self.entered_day == today.isoformat():
            return []  # one entry per day — a stopped-out day does not re-enter
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

    def _t_years(self, expiry: date, now: datetime) -> float:
        exp_dt = datetime.combine(expiry, self._EXPIRY_CUTOFF)
        if now.tzinfo is not None:
            exp_dt = exp_dt.replace(tzinfo=now.tzinfo)
        return max((exp_dt - now).total_seconds(), 0.0) / (365.0 * 24 * 3600)

    def _atm_strike(self, rows: dict[float, dict], spot: float) -> float | None:
        """Listed strike nearest ``spot`` with BOTH a CE and PE that print + have OI."""
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

    def _delta_strike(self, rows: dict[float, dict], side: str, spot: float, t: float) -> float | None:
        """Strike whose per-share BS |delta| (IV solved from its own LTP) is nearest
        ``strike_delta`` — ANY strike (no OTM filter), so ~0.6Δ (slightly ITM) is allowed."""
        if t <= 0:
            return None
        right = "CE" if side == "ce" else "PE"
        best = None
        for k, r in rows.items():
            prem = self._ltp(r.get(side))
            if prem is None or not self._oi_ok(r.get(side)):
                continue
            iv = bs.implied_vol(prem, spot, k, t, self.r, right)
            if iv is None or iv <= 0:
                continue
            d = abs(bs.delta(spot, k, t, self.r, iv, right))
            err = abs(d - self.strike_delta)
            if best is None or err < best[0]:
                best = (err, k)
        return best[1] if best else None

    # ----------------------------------------------------------------- entry
    def _try_enter(self, ctx, now: datetime, today: date) -> list[Signal]:
        expiry = self._nearest_expiry(ctx, today)
        if expiry is None:
            return []
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(self.underlying, expiry.isoformat()) if chain_fn else None
        if not chain or not chain.get("rows"):
            return []  # no live chain this tick — retry within the window
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

        if self.strike_delta > 0:
            t = self._t_years(expiry, now)
            ce_k = self._delta_strike(rows, "ce", float(spot), t)
            pe_k = self._delta_strike(rows, "pe", float(spot), t)
        else:
            ce_k = pe_k = self._atm_strike(rows, float(spot))
        if ce_k is None or pe_k is None:
            return []
        ce_ltp = self._ltp(rows.get(float(ce_k), {}).get("ce"))
        pe_ltp = self._ltp(rows.get(float(pe_k), {}).get("pe"))
        if ce_ltp is None or pe_ltp is None:
            return []

        units = float(self.lots * per_lot)
        def sym(k: float, right: str) -> str:
            return make(self.underlying, expiry, float(k), right, lot_size=per_lot,
                        lot_overrides=self.lot_overrides).symbol
        self.legs = [
            {"symbol": sym(ce_k, "CE"), "dir": -1, "units": units, "entry": ce_ltp},
            {"symbol": sym(pe_k, "PE"), "dir": -1, "units": units, "entry": pe_ltp},
        ]
        # margin_base: BROKER basket margin only (owner rule) — pending until the manager pushes it.
        self.margin_base = 0.0
        self.margin_source = "pending"
        self.peak_pct = 0.0
        self.entered_day = today.isoformat()
        return [
            Signal(leg["symbol"], SignalAction.ENTER_SHORT, quantity=int(leg["units"]),
                   reason="straddle_entry", meta={"multiplier": 1})
            for leg in self.legs
        ]

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, legs: list[dict], now) -> list[Signal]:
        if now.time() >= self.exit_time:  # hard time exit — never waits on margin
            return self._exit_all(legs, "eod")
        if self.margin_source != "broker":
            if not self._broker_margin:
                return []  # stops WAIT for the broker margin (never the model)
            self.margin_base = self._broker_margin
            self.margin_source = "broker"
        base = self.margin_base or 0.0
        if base <= 0:
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
        pnl_pct = 100.0 * pnl / base
        # Two-cadence sampling: the trail's high-water update rides profit_check, the
        # stop COMPARISON rides stop_check. Sampled HERE — after the time-exit, margin
        # and print guards above — because _due consumes its window (mixin rule #1);
        # sampling before an early return would eat a stop slot. Defaults "tick" keep
        # this byte-identical to the pre-cadence behavior.
        if self._due("profit", now) and pnl_pct > self.peak_pct:
            self.peak_pct = pnl_pct
        if self._due("stop", now):
            stop_pct = self._stop_level()
            if pnl_pct <= stop_pct:
                return self._exit_all(legs, "trail" if stop_pct > -self.stop_loss_pct else "stop")
        return []

    def _stop_level(self) -> float:
        """Current stop as a % of margin_base (negative = loss). Starts at −stop_loss_pct and
        only ratchets UP as peak_pct grows, per trail_mode. Trailing off if a trail pct is 0."""
        fixed = -self.stop_loss_pct
        if self.trail_trigger_pct <= 0 or self.trail_step_pct <= 0:
            return fixed
        if self.trail_mode == "below_peak":
            if self.peak_pct < self.trail_trigger_pct:
                return fixed
            return max(fixed, self.peak_pct - self.trail_step_pct)
        # ratchet (default): each trail_trigger_pct of peak profit lifts the stop by trail_step_pct
        steps = floor(self.peak_pct / self.trail_trigger_pct) if self.peak_pct > 0 else 0
        return max(fixed, fixed + self.trail_step_pct * steps)

    def _exit_all(self, legs: list[dict], reason: str) -> list[Signal]:
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in legs]
        self.legs = []
        return sigs

    # ------------------------------------------------------------ snapshot hooks
    def exit_amounts(self) -> tuple[float | None, float | None]:
        base = self.margin_base if self.margin_source == "broker" else 0.0
        if base <= 0:
            return None, None
        return None, base * self.stop_loss_pct / 100.0  # no fixed target; trailing is the upside

    def exit_rules(self) -> list[str]:
        rules = [f"Stop out at −{self.stop_loss_pct:g}% of broker margin "
                 f"({self._cadence_phrase('stop')})"]
        if self.trail_trigger_pct > 0 and self.trail_step_pct > 0:
            if self.trail_mode == "below_peak":
                rules.append(f"Trail: once +{self.trail_trigger_pct:g}% up, stop = peak − "
                             f"{self.trail_step_pct:g}% ({self._cadence_phrase('profit')})")
            else:
                rules.append(f"Trail: every +{self.trail_trigger_pct:g}% profit raises the stop "
                             f"+{self.trail_step_pct:g}% ({self._cadence_phrase('profit')})")
        rules.append(f"Hard exit {self.exit_time.strftime('%H:%M')} — never carried")
        return rules

    # --------------------------------------------------------------- monitor
    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        today = market.current_date
        return {"kind": "straddle", "names": [{
            "name": self.underlying,
            "spot": getattr(market, "index_spot", lambda _u: None)(self.underlying),
            "traded_today": self.entered_day == today.isoformat(),
            "legs": [dict(leg) for leg in self.legs],
            "margin_base": self.margin_base,
            "margin_source": self.margin_source,
            "peak_pct": self.peak_pct,
            "stop_pct": self._stop_level(),
            "stop_amt": (self.margin_base or 0) * self.stop_loss_pct / 100,
        }]}

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "legs": [dict(x) for x in self.legs],
            "entered_day": self.entered_day,
            "margin_base": self.margin_base,
            "margin_source": self.margin_source,
            "peak_pct": self.peak_pct,
            "force_pending": self.force_pending,
        }

    def load_state(self, state: dict) -> None:
        self.legs = [dict(x) for x in (state.get("legs") or [])]
        self.entered_day = state.get("entered_day")
        self.margin_base = float(state.get("margin_base", 0.0))
        self.margin_source = state.get("margin_source", "")
        self.peak_pct = float(state.get("peak_pct", 0.0))
        self.force_pending = bool(state.get("force_pending", False))
