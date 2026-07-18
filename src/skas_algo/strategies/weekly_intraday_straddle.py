"""weekly_intraday_straddle — a weekly-cycle, intraday SHORT straddle gated by VWAP and the
prior-day combined-premium low (NIFTY). The classic "short straddle with a VWAP stop".

Reference (owner-supplied): https://www.youtube.com/watch?v=kYahbSjbubQ

A cycle spans one weekly NIFTY expiry period. On the FIRST trading day after the previous
weekly expiry ("expiry+1"), at 09:20, the ATM strike (nearest 100 — the platform-wide NIFTY
rule, already applied by the live chain's coarsening) of the nearest listed weekly is LOCKED;
that fixed strike + expiry is the cycle's straddle, traded intraday EVERY trading day of the
week. A mid-cycle deploy auto force-starts at the current ATM (it does not wait a week); a new
cycle re-anchors at each subsequent expiry+1 09:20 (detected off the chain's nearest weekly).

Daily rules, on the FIXED strike's combined premium (from Kite 5-min OPTION bars with volume):
  * x    = CE.close + PE.close of the last CLOSED 5-min bar.
  * VWAP  = sum of per-leg session VWAPs (VWAP(CE)+VWAP(PE)), volume-weighted, resets daily.
  * y     = the prior trading day's intraday LOW of the combined premium (min of CE.close+PE.close).
  * ENTRY (SELL) on a 5-min close, when flat: x < y AND x < VWAP. Up to max_entries_per_day.
  * EXIT: x closes back ABOVE VWAP (cross-up), OR 15:25 hard square-off (never waits on margin);
    optional stop_loss_pct (% of frozen broker margin), default 0 = OFF (short = uncapped tails).
  * RE-ENTRY same day: flat again and x < VWAP AND x < y (up to the cap). Intraday only.

Design notes:
- **DEPLOY-ONLY, broker (zerodha) source required, no backtest** (GFF intraday-option data will
  seed a backtest later). Strike selection needs ``ctx.market.live_chain``; x/VWAP/y need the
  option bars pushed by the manager via ``set_option_bars_fn`` (None on a cache source → entries
  gate off, safe cold-start).
- **``on_slice`` orders protective exits (15:25 + optional stop) BEFORE the Kite bar fetch**, and
  wraps the fetch in try/except, so a broker hiccup can never swallow the square-off (the engine
  runs ``on_slice`` bare).
- **``margin_base`` is the BROKER basket margin ONLY** (owner rule — the model reads ~2×); the
  manager pushes it via ``set_broker_margin``; the optional stop WAITS for it, the 15:25 exit
  never does.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from skas_algo.engine.options.contract_specs import expiry_weekday_for, lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction
from skas_algo.live.holidays import previous_trading_day

from ._options_common import ExitCadenceMixin, bad_close, legs_mtm_pnl

logger = logging.getLogger(__name__)


def _hhmm(s: str, fallback: time) -> time:
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return fallback


# Data-health alerts (surfaced on the Live page via the snapshot's ``strategy_alert``). The
# strategy NEVER opens a position — not even a forced one — while option bars are unfetchable:
# without them x/VWAP/y don't exist, so the VWAP exit could never fire on the open book.
_ALERT_NO_SOURCE = (
    "option-bars source unavailable (no broker adapter — cache fallback / logged out?) — "
    "x/VWAP/y can't be computed; ALL entries disabled"
)
_ALERT_NO_BARS = (
    "Kite returned no 5-min option bars for today's straddle legs — check the historical-data "
    "subscription on this account; entries disabled until bars flow"
)
_ALERT_NO_PRIOR = (
    "prior-day option bars unavailable — can't compute y (yesterday's combined-premium low); "
    "check the Kite historical-data subscription; entries disabled"
)
_ALERT_FETCH_FAILED = (
    "option-bar fetch failed (broker error) — entries disabled; retrying every tick"
)


class WeeklyIntradayStraddle(ExitCadenceMixin):
    strategy_id = "weekly_intraday_straddle"
    intraday = True  # ticks every refresh_seconds; the cycle/entry/exit windows self-gate

    _MARKET_OPEN = time(9, 15)
    _EOD_HARD = time(15, 30)

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,
        entry_start: str = "09:20",       # cycle lock time on expiry+1 AND daily entry-window open
        entry_cutoff: str = "15:20",      # no fresh entries after this (just before the square-off)
        eod_exit: str = "15:25",          # hard intraday square-off — never carried
        candle_minutes: int = 5,
        max_entries_per_day: int = 3,
        stop_loss_pct: float = 0.0,       # optional MTM stop, % of broker margin; 0 = OFF
        # Stop-comparison cadence (two-cadence model 2026-07-18). "tick" = every call —
        # the pre-cadence behavior (§1). NO profit_check here: this strategy has no
        # profit-booking decision (the VWAP cross-up, bar-driven, is the exit).
        stop_check: str = "tick",
        eod_time: str = "15:20",
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.lots = max(1, int(lots))
        self.entry_start = _hhmm(entry_start, time(9, 20))
        self.entry_cutoff = _hhmm(entry_cutoff, time(15, 20))
        self.eod_exit = _hhmm(eod_exit, time(15, 25))
        self.candle_minutes = max(1, int(candle_minutes))
        self.max_entries_per_day = int(max_entries_per_day)
        self.stop_loss_pct = float(stop_loss_pct or 0.0)
        self.stop_check = str(stop_check)
        self.eod_time = str(eod_time)
        self.min_leg_oi = int(min_leg_oi)
        self.lot_overrides = lot_overrides
        self.initial_capital = initial_capital

        # ---- state (all persisted) ----
        # cycle = {expiry_iso, strike, ce_symbol, pe_symbol, start_day, lot_size} or None.
        # None ⇒ needs anchoring; the anchor flag IS the cycle. The re-anchor guard keys off
        # cycle["expiry_iso"] so a mid-week restart keeps the locked strike.
        self.cycle: dict | None = None
        self.legs: list[dict] = []
        self.entries_today: int = 0
        self.day_iso: str | None = None
        self.evaluated_bar: str | None = None  # start-iso of the last 5-min bar evaluated (1/bar)
        self.y_today: float | None = None       # prior-day combined-premium low for THIS day
        self.y_day: str | None = None           # the day y_today was computed for
        self.prev_close: float | None = None    # prior-day combined-premium CLOSE (display)
        self.prev_day: str | None = None        # the prior trading day y/prev_close came from
        self.margin_base: float = 0.0
        self.margin_source: str = ""
        self.peak_pct: float = 0.0
        self.force_pending: bool = False
        # Data-health error shown on the Live page (manager surfaces it as ``strategy_alert``).
        # Set whenever option bars can't be fetched; entries (incl. forced) stay disabled while
        # set; re-evaluated once per 5-min boundary. Persisted so a restart keeps the banner.
        self.strategy_alert: str | None = None
        # ---- transient (never persisted) ----
        self._broker_margin: float | None = None   # re-pushed by set_broker_margin after recovery
        self._option_bars_fn = None                 # re-wired by the manager on _wire_quote_source
        self._x: float | None = None                # last combined-premium close (monitor only)
        self._vwap_val: float | None = None         # last VWAP (monitor only)
        self._vwap_ce: float | None = None          # per-leg session VWAPs (monitor only)
        self._vwap_pe: float | None = None
        # Today's combined-premium 5-min series with the RUNNING VWAP per bar — the Live-page
        # signal chart. Refilled from the same per-boundary fetch the signal uses (no extra
        # broker calls); transient, so after a restart it repopulates on the next boundary.
        self._today_series: list[dict] = []

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return [self.underlying]

    def set_broker_margin(self, value: float) -> None:
        if value and value > 0:
            self._broker_margin = float(value)

    def strategy_pnl(self, closes: dict) -> float | None:
        """The MTM measure the stop check compares (decision-entry basis)."""
        return legs_mtm_pnl(self.legs, closes)

    def set_option_bars_fn(self, fn) -> None:
        """Manager wiring: fn(underlying, expiry_iso, strike, right, from_dt, to_dt, minutes)
        -> [{start, o, h, l, c, volume}]. None on a cache source → entries gate off."""
        self._option_bars_fn = fn

    def request_force_entry(self) -> str:
        """Live-page 'Force entry now': next tick anchors the cycle to the current ATM (if not
        already) and SELLS the straddle immediately, bypassing the daily x<y & x<VWAP gate and
        the entry window. Persisted so a restart keeps it."""
        self.force_pending = True
        return "next tick anchors the cycle at the current ATM and sells the straddle now"

    # ---------------------------------------------------------------- expiry
    def _nearest_expiry(self, ctx, today: date) -> date | None:
        """Nearest listed weekly ≥ today (live-chain expiries when available, else calendar)."""
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
        now: datetime = ctx.now()
        today: date = ctx.today()
        self._roll_day(today)
        live = self._live_legs(ctx)

        # (A) Protective exits FIRST — unconditional, never depend on the Kite bar fetch.
        if live:
            got = self._protective_exit(ctx, live, now)
            if got:
                return got

        # No option-bars source at all (cache fallback / logged out) → surface the error and
        # never open a position: x/VWAP/y can't exist, so the VWAP exit could never fire.
        if self._option_bars_fn is None:
            self.strategy_alert = _ALERT_NO_SOURCE
            return []

        # (B) Forced entry (Live-page button): anchor now + sell, bypassing the signal gates —
        # but NOT the data-health gate: bars must be fetchable (probe = computing y) or the
        # forced book would have no working VWAP exit. Flag stays armed; retries every tick.
        if not live and self.force_pending:
            if self._lock_cycle_if_needed(ctx, today):
                try:
                    self._ensure_y(now, today)
                except Exception:  # pragma: no cover - probe hiccup → treated as unfetchable
                    logger.exception("weekly_intraday_straddle force-probe failed (%s)",
                                     self.underlying)
                    self.strategy_alert = _ALERT_FETCH_FAILED
                    return []
                if self.y_today is None:
                    return []  # _ensure_y set the alert; force_pending stays armed
                self.strategy_alert = None
                got = self._enter(ctx)
                if got:
                    self.force_pending = False
                return got
            return []

        # (C) Ensure a cycle is anchored (auto-lock at 09:20 expiry+1 / mid-cycle force-start).
        if not self._ensure_cycle(ctx, now, today):
            return []

        # (D) Per-5min-boundary evaluation (guarded fetch): VWAP-cross exit / signal entry.
        try:
            return self._evaluate(ctx, live, now, today)
        except Exception:  # pragma: no cover - a broker hiccup never swallows protective exits
            logger.exception("weekly_intraday_straddle bar-eval failed (%s)", self.underlying)
            self.strategy_alert = _ALERT_FETCH_FAILED
            return []

    def _roll_day(self, today: date) -> None:
        if self.day_iso != today.isoformat():
            self.day_iso = today.isoformat()
            self.entries_today = 0
            self.evaluated_bar = None
            self.y_today = None
            self.y_day = None
            self.prev_close = None
            self.prev_day = None
            self._x = None
            self._vwap_val = None
            self._vwap_ce = None
            self._vwap_pe = None
            self._today_series = []

    def _live_legs(self, ctx) -> list[dict]:
        if not self.legs:
            return []
        if not any(ctx.lots(leg["symbol"]) for leg in self.legs):
            self.legs = []  # engine settled/closed everything
            return []
        return self.legs

    # ------------------------------------------------------------- cycle anchor
    def _ensure_cycle(self, ctx, now: datetime, today: date) -> bool:
        """True when a valid cycle is active this tick. Locks a new cycle at/after entry_start
        on a fresh deploy or a weekly roll (the nearest listed weekly changed)."""
        nearest = self._nearest_expiry(ctx, today)
        if nearest is None:  # chain briefly unavailable — keep an un-expired cycle if we have one
            return self.cycle is not None and self.cycle["expiry_iso"] >= today.isoformat()
        if self.cycle is not None and self.cycle["expiry_iso"] == nearest.isoformat():
            return True  # still inside the current cycle
        if now.time() >= self.entry_start:  # lock the new cycle (09:20 expiry+1, or mid-cycle now)
            return self._lock_cycle(ctx, today, nearest)
        return False  # pre-09:20 with no/expired cycle → dormant until the 09:20 lock

    def _lock_cycle_if_needed(self, ctx, today: date) -> bool:
        """Anchor a cycle at the current ATM regardless of the time (the force-entry path)."""
        nearest = self._nearest_expiry(ctx, today)
        if nearest is None:
            return self.cycle is not None
        if self.cycle is not None and self.cycle["expiry_iso"] == nearest.isoformat():
            return True
        return self._lock_cycle(ctx, today, nearest)

    def _lock_cycle(self, ctx, today: date, expiry: date) -> bool:
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(self.underlying, expiry.isoformat()) if chain_fn else None
        if not chain or not chain.get("rows") or not chain.get("atm_strike"):
            return False  # no live chain this tick — retry
        atm = float(chain["atm_strike"])  # already 100-coarsened for NIFTY by the live chain
        per_lot = int(chain.get("lot_size") or 0)
        if per_lot <= 0:
            try:
                per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
            except KeyError:
                return False
        ce = make(self.underlying, expiry, atm, "CE", lot_size=per_lot,
                  lot_overrides=self.lot_overrides).symbol
        pe = make(self.underlying, expiry, atm, "PE", lot_size=per_lot,
                  lot_overrides=self.lot_overrides).symbol
        self.cycle = {"expiry_iso": expiry.isoformat(), "strike": atm, "ce_symbol": ce,
                      "pe_symbol": pe, "start_day": today.isoformat(), "lot_size": per_lot}
        # New strike ⇒ recompute this day's y and re-open the per-bar latch.
        self.y_today = None
        self.y_day = None
        self.prev_close = None
        self.prev_day = None
        self._today_series = []
        self.evaluated_bar = None
        return True

    # ------------------------------------------------------------- evaluation
    def _evaluate(self, ctx, live: list[dict], now: datetime, today: date) -> list[Signal]:
        cur_boundary = self._bar_start(now).replace(tzinfo=None).isoformat()
        frm = datetime.combine(today, self._MARKET_OPEN)
        combined = self._fetch_combined(frm, now.replace(tzinfo=None))
        closed = [r for r in combined if r["start"] < cur_boundary]  # drop the in-progress bar
        if not closed:
            # Normal before the session's first candle closes; past that it means Kite served
            # nothing for the legs (no historical-data subscription / API outage) — surface it.
            grace = (datetime.combine(today, self.entry_start)
                     + timedelta(minutes=self.candle_minutes)).time()
            if now.time() >= grace:
                self.strategy_alert = _ALERT_NO_BARS
            return []  # nothing to evaluate yet — retry next tick (no latch)
        last = closed[-1]
        if last["start"] == self.evaluated_bar:
            return []  # this 5-min close already evaluated
        self.evaluated_bar = last["start"]
        self.strategy_alert = None  # bars flowed this boundary — data healthy (y may re-flag)
        if last["start"][:10] != today.isoformat():
            return []  # a stale prior-day bar (should not happen — from_dt is today)

        # y + prev-close refresh every boundary (once/day latch inside) — ALSO while holding,
        # so the Live signal panel always shows the prior-day levels and a post-exit re-entry
        # can judge immediately.
        self._ensure_y(now, today)

        x = last["cc"]
        self._today_series, self._vwap_ce, self._vwap_pe = self._vwap_series(closed)
        vwap = (self._vwap_ce + self._vwap_pe) \
            if self._vwap_ce is not None and self._vwap_pe is not None else None
        self._x, self._vwap_val = x, vwap

        # EXIT: combined premium closes back above VWAP (cross-up) — only when holding.
        if live:
            if vwap is not None and x > vwap:
                return self._exit_all(live, "vwap_cross")
            return []

        # ENTRY (SELL): flat, inside the window, cap not hit, x < y AND x < VWAP.
        if not (self.entry_start <= now.time() < self.entry_cutoff):
            return []
        if self.entries_today >= self.max_entries_per_day:
            return []
        if self.y_today is None or vwap is None:
            return []
        if x < self.y_today and x < vwap:
            return self._enter(ctx)
        return []

    def _ensure_y(self, now: datetime, today: date) -> None:
        """Once/day: y = the prior trading day's intraday LOW of the combined premium (min of
        CE.close+PE.close over that day's 5-min bars). Retried each boundary until available."""
        # prev_close check self-heals state persisted before it existed (refetch fills it).
        if self.y_day == today.isoformat() and self.prev_close is not None:
            return
        prev = previous_trading_day(today)
        frm = datetime.combine(prev, self._MARKET_OPEN)
        to = datetime.combine(prev, self._EOD_HARD)
        combined = self._fetch_combined(frm, to)
        if not combined:
            self.strategy_alert = _ALERT_NO_PRIOR
            return  # retry next boundary; entries stay gated (y is None)
        self.y_today = min(r["cc"] for r in combined)
        self.prev_close = combined[-1]["cc"]  # prior day's last combined close (display only)
        self.prev_day = prev.isoformat()
        self.y_day = today.isoformat()

    # ------------------------------------------------------------- bar helpers
    def _bar_start(self, now: datetime) -> datetime:
        m = (now.minute // self.candle_minutes) * self.candle_minutes
        return now.replace(minute=m, second=0, microsecond=0)

    def _fetch_combined(self, from_dt: datetime, to_dt: datetime) -> list[dict]:
        """Both legs' 5-min bars inner-joined on ``start`` → the combined-premium series.
        Each row: {start, cc (=CE.close+PE.close), ce, pe}. Empty on any missing/failed fetch."""
        cy = self.cycle
        if cy is None or self._option_bars_fn is None:
            return []
        ce = self._option_bars_fn(self.underlying, cy["expiry_iso"], cy["strike"], "CE",
                                  from_dt, to_dt, self.candle_minutes) or []
        pe = self._option_bars_fn(self.underlying, cy["expiry_iso"], cy["strike"], "PE",
                                  from_dt, to_dt, self.candle_minutes) or []
        pe_by = {b["start"]: b for b in pe}
        out = []
        for c in ce:
            p = pe_by.get(c["start"])
            if p is None:
                continue
            out.append({"start": c["start"], "cc": float(c["c"]) + float(p["c"]),
                        "ce": c, "pe": p})
        out.sort(key=lambda r: r["start"])
        return out

    @staticmethod
    def _vwap_series(closed: list[dict]) -> tuple[list[dict], float | None, float | None]:
        """One pass over today's closed bars → (series, vwap_ce, vwap_pe). Each series row is
        {start, cc, vwap} where ``vwap`` is the RUNNING combined VWAP as of that bar (the line
        the Live chart draws; None until BOTH legs have traded volume). The final per-leg
        VWAPs are the signal inputs: VWAP = vwap_ce + vwap_pe."""
        num = {"ce": 0.0, "pe": 0.0}
        den = {"ce": 0.0, "pe": 0.0}
        cum = {"ce": None, "pe": None}
        series: list[dict] = []
        for r in closed:
            for key in ("ce", "pe"):
                b = r[key]
                typ = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
                v = float(b.get("volume") or 0.0)
                num[key] += typ * v
                den[key] += v
                cum[key] = (num[key] / den[key]) if den[key] > 0 else None
            both = cum["ce"] is not None and cum["pe"] is not None
            series.append({"start": r["start"], "cc": r["cc"],
                           "vwap": (cum["ce"] + cum["pe"]) if both else None})
        return series, cum["ce"], cum["pe"]

    # ----------------------------------------------------------------- entry
    def _enter(self, ctx) -> list[Signal]:
        cy = self.cycle
        if cy is None:
            return []
        per_lot = int(cy.get("lot_size") or 0)
        if per_lot <= 0:
            try:
                per_lot = lot_size_for(self.underlying, date.fromisoformat(cy["expiry_iso"]),
                                       overrides=self.lot_overrides)
            except KeyError:
                return []
        ce_ltp = self._leg_ltp(ctx, cy["ce_symbol"])
        pe_ltp = self._leg_ltp(ctx, cy["pe_symbol"])
        if ce_ltp is None or pe_ltp is None:
            return []  # no live mark yet — the next boundary retries
        units = float(self.lots * per_lot)
        self.legs = [
            {"symbol": cy["ce_symbol"], "dir": -1, "units": units, "entry": ce_ltp},
            {"symbol": cy["pe_symbol"], "dir": -1, "units": units, "entry": pe_ltp},
        ]
        # margin_base: BROKER basket margin only (owner rule) — pending until the manager pushes it.
        self.margin_base = 0.0
        self.margin_source = "pending"
        self.peak_pct = 0.0
        self.entries_today += 1
        return [
            Signal(leg["symbol"], SignalAction.ENTER_SHORT, quantity=int(leg["units"]),
                   reason="wis_entry", meta={"multiplier": 1})
            for leg in self.legs
        ]

    def _leg_ltp(self, ctx, symbol: str) -> float | None:
        try:
            v = ctx.close(symbol)
        except KeyError:
            return None
        return None if bad_close(v) else float(v)

    # ---------------------------------------------------------------- manage
    def _protective_exit(self, ctx, legs: list[dict], now: datetime) -> list[Signal]:
        """15:25 hard square-off (never waits on margin) + optional MTM stop (% broker margin)."""
        if now.time() >= self.eod_exit:
            return self._exit_all(legs, "eod")
        if self.stop_loss_pct <= 0:
            return []  # VWAP-cross + EOD only
        if self.margin_source != "broker":
            if not self._broker_margin:
                return []  # the stop WAITS for the broker margin (never the model)
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
        if pnl_pct > self.peak_pct:
            self.peak_pct = pnl_pct
        # Sampled AFTER every readiness guard above — _due consumes its window (mixin
        # rule #1); default "tick" keeps this byte-identical to pre-cadence behavior.
        if self._due("stop", now) and pnl_pct <= -self.stop_loss_pct:
            return self._exit_all(legs, "stop")
        return []

    def _exit_all(self, legs: list[dict], reason: str) -> list[Signal]:
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in legs]
        self.legs = []
        return sigs

    # ------------------------------------------------------------ snapshot hooks
    def exit_amounts(self) -> tuple[float | None, float | None]:
        if self.stop_loss_pct > 0 and self.margin_source == "broker" and self.margin_base > 0:
            return None, self.margin_base * self.stop_loss_pct / 100.0
        return None, None  # no fixed target; the VWAP cross-up is the exit

    def exit_rules(self) -> list[str]:
        rules = ["Exit when the combined premium closes back above VWAP (checked per closed 5-min bar)"]
        if self.stop_loss_pct > 0:
            rules.append(f"Stop out at −{self.stop_loss_pct:g}% of broker margin "
                         f"({self._cadence_phrase('stop')})")
        rules.append(f"Hard square-off {self.eod_exit.strftime('%H:%M')} — never carried")
        return rules

    # --------------------------------------------------------------- monitor
    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        cy = self.cycle or {}
        return {"kind": "weekly_straddle", "names": [{
            "name": self.underlying,
            "spot": getattr(market, "index_spot", lambda _u: None)(self.underlying),
            "cycle_expiry": cy.get("expiry_iso"),
            "cycle_strike": cy.get("strike"),
            "cycle_start": cy.get("start_day"),
            "x": self._x, "vwap": self._vwap_val, "y": self.y_today,
            "vwap_ce": self._vwap_ce, "vwap_pe": self._vwap_pe,
            "prev_close": self.prev_close, "prev_day": self.prev_day,
            # Today's 5-min combined-premium closes + running VWAP — the signal chart.
            "series": [dict(r) for r in self._today_series],
            "alert": self.strategy_alert,
            "entries_today": self.entries_today, "max_entries": self.max_entries_per_day,
            "legs": [dict(leg) for leg in self.legs],
            "margin_base": self.margin_base, "margin_source": self.margin_source,
            "stop_amt": ((self.margin_base or 0) * self.stop_loss_pct / 100
                         if self.stop_loss_pct > 0 else None),
        }]}

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "cycle": (dict(self.cycle) if self.cycle else None),
            "legs": [dict(x) for x in self.legs],
            "entries_today": self.entries_today,
            "day_iso": self.day_iso,
            "evaluated_bar": self.evaluated_bar,
            "y_today": self.y_today,
            "y_day": self.y_day,
            "prev_close": self.prev_close,
            "prev_day": self.prev_day,
            "margin_base": self.margin_base,
            "margin_source": self.margin_source,
            "peak_pct": self.peak_pct,
            "force_pending": self.force_pending,
            "strategy_alert": self.strategy_alert,
        }

    def load_state(self, state: dict) -> None:
        self.cycle = dict(state["cycle"]) if state.get("cycle") else None
        self.legs = [dict(x) for x in (state.get("legs") or [])]
        self.entries_today = int(state.get("entries_today", 0))
        self.day_iso = state.get("day_iso")
        self.evaluated_bar = state.get("evaluated_bar")
        self.y_today = state.get("y_today")
        self.y_day = state.get("y_day")
        self.prev_close = state.get("prev_close")
        self.prev_day = state.get("prev_day")
        self.margin_base = float(state.get("margin_base", 0.0))
        self.margin_source = state.get("margin_source", "")
        self.peak_pct = float(state.get("peak_pct", 0.0))
        self.force_pending = bool(state.get("force_pending", False))
        self.strategy_alert = state.get("strategy_alert")
