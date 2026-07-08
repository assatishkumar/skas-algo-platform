"""momentum_theta_gainer_intra — intraday 15-min SuperTrend + daily-pivot option seller.

Trades weekly-expiry index options (NIFTY, SENSEX) off self-built 15-minute candles:
on a CLOSED candle, close above SuperTrend(7,3) AND above the daily pivot R1 → sell the
ATM PUT of the nearest weekly (0DTE allowed); close below SuperTrend AND below S1 → sell
the ATM CALL. Exit when SuperTrend flips against the position or at 15:20 IST. Max
``max_trades_per_day`` ENTRIES per underlying; one open position per underlying; after a
flip exit re-entry needs a FRESH full signal on a LATER candle (no same-candle reversal).

Design notes (why it looks the way it does):
- **The strategy builds its own 15-min bars** from the live index spot ticks
  (``ctx.market.index_spot``) using ``ctx.now()`` — no intraday bars exist anywhere in the
  cache, and the live loop ticks every ``refresh_seconds`` (deploy default 15s). A candle
  closes on the first tick at/after its boundary, so closes are evaluated ≤ one tick late.
  Bars are carried in ``export_state`` so restarts resume mid-day.
- **Pivots come from the strategy's OWN prior-day bars** (aggregated H/L/C), not the daily
  cache — SENSEX has no cached daily series at all, and the warmup seed (below) provides
  the prior days for both names. No prior-day bars → no pivots → no entries (safe cold
  start).
- **Warmup**: ``seed_intraday_bars(fetch)`` is called by the live manager (getattr-guarded)
  with the broker adapter's ``intraday_bars`` when a Zerodha session exists — seeds ~7 days
  of real 15-min bars so SuperTrend and pivots are valid from the first tick. On a cache
  quote source it cold-starts instead (ST valid after ~2×period candles, entries from
  day 2 once a prior day of bars exists).
- SENSEX is LIVE-ONLY (no history exists to backtest); its options quote via BFO — the
  Zerodha adapter handles the exchange per contract. The dedicated backtest service
  (services/momentum_theta_bt) replays NIFTY only, with BS-priced premiums.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from skas_algo.engine.indicators.supertrend import _supertrend_bars
from skas_algo.engine.options.contract_specs import expiry_weekday_for, lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close

# ATM strike rounding step per index.
_STRIKE_STEP = {"NIFTY": 50, "SENSEX": 100, "BANKNIFTY": 100}
_MARKET_OPEN = time(9, 15)


def _parse_hhmm(s: str, fallback: time) -> time:
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return fallback


class MomentumThetaGainerIntra:
    strategy_id = "momentum_theta_gainer_intra"
    intraday = True  # tick every refresh_seconds

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 500_000,
        underlyings: list[str] | None = None,
        lots: dict | int | None = None,          # {"NIFTY": 1, "SENSEX": 1} or a single int
        st_period: int = 7,
        st_multiplier: float = 3.0,
        candle_minutes: int = 15,
        max_trades_per_day: int = 3,
        eod_exit: str = "15:20",
        entry_cutoff: str = "15:00",             # no fresh shorts minutes before the forced exit
        min_dte: int = 0,                        # 0 → 0DTE allowed on expiry day
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlyings = [u.upper() for u in (underlyings or ["NIFTY"])]
        if isinstance(lots, dict):
            self.lots = {u.upper(): max(1, int(v)) for u, v in lots.items()}
        else:
            self.lots = {u: max(1, int(lots or 1)) for u in self.underlyings}
        self.st_period = int(st_period)
        self.st_multiplier = float(st_multiplier)
        self.candle_minutes = max(1, int(candle_minutes))
        self.max_trades_per_day = int(max_trades_per_day)
        self.eod_exit = _parse_hhmm(eod_exit, time(15, 20))
        self.entry_cutoff = _parse_hhmm(entry_cutoff, time(15, 0))
        self.min_dte = int(min_dte)
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # ---- per-underlying state (ALL persisted for restart recovery) ----
        # bars: list of [start_iso, o, h, l, c] CLOSED candles, oldest first.
        self.bars: dict[str, list[list]] = {u: [] for u in self.underlyings}
        self.pending: dict[str, dict | None] = {u: None for u in self.underlyings}
        # open leg per underlying: {symbol, right, units, entry_close, entered_candle}
        self.open_leg: dict[str, dict | None] = {u: None for u in self.underlyings}
        self.entries_today: dict[str, int] = {u: 0 for u in self.underlyings}
        self.day_iso: str | None = None
        # pivots per underlying: {"day": iso, "r1": float, "s1": float, "p": float}
        self.pivots: dict[str, dict | None] = {u: None for u in self.underlyings}
        # start of the last candle whose close was evaluated for entries (skip-once guard)
        self.evaluated_candle: dict[str, str | None] = {u: None for u in self.underlyings}
        self._seeded = False
        # Optional official-daily-OHLC provider: fn(underlying, today) -> {high, low,
        # close} of the PRIOR trading day, or None. NSE's official close is the last-30-min
        # weighted average, so pivots from it match TradingView's "Auto" daily pivots;
        # bar-derived stays the fallback (SENSEX has no daily series anywhere).
        self._daily_ohlc_fn = None
        # Per-day cache of the provider row so an ungated day doesn't re-hit the broker every
        # tick: {underlying: (day_iso, row_or_None)}. Transient (not serialized).
        self._daily_ohlc_cache: dict[str, tuple[str, dict | None]] = {}
        # Optional live alert sink: fn(underlying, message) — used ONLY to warn that the
        # daily pivot source was stale (never wired in backtest). Dedup per day per name.
        self._notify_fn = None
        self._stale_alerted: dict[str, str] = {}

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        """The live loop feeds each of these names' index spot every tick."""
        return list(self.underlyings)

    def set_daily_ohlc_fn(self, fn) -> None:
        self._daily_ohlc_fn = fn

    def set_notify_fn(self, fn) -> None:
        self._notify_fn = fn

    def seed_intraday_bars(self, fetch) -> None:
        """Warm SuperTrend + pivots with real 15-min history at deploy/recovery.
        ``fetch(underlying, days, minutes)`` -> [{start, open, high, low, close}, ...].
        Idempotent: only fills bars we don't already have (restart keeps live-built bars)."""
        if self._seeded:
            return
        keep = self._keep_bars()
        for u in self.underlyings:
            try:
                hist = fetch(u, 14, self.candle_minutes) or []
            except Exception:  # pragma: no cover - warmup is best-effort, never fatal
                continue
            have = {b[0] for b in self.bars[u]}
            rows = [[h["start"], float(h["open"]), float(h["high"]), float(h["low"]),
                     float(h["close"])] for h in hist if h.get("start") not in have]
            self.bars[u] = sorted(self.bars[u] + rows, key=lambda b: b[0])[-keep:]
        self._seeded = True

    # -------------------------------------------------------------- candles
    def _keep_bars(self) -> int:
        """Rolling bar window. SuperTrend's band ratchet is path-dependent, so too small
        a window shifts flip timing vs a continuous series (TradingView) — ~10 sessions
        of 15-min bars keeps flips stable while staying trivial to recompute."""
        return max(24 * self.st_period, 260)

    def _candle_start(self, now: datetime) -> datetime:
        m = (now.minute // self.candle_minutes) * self.candle_minutes
        return now.replace(minute=m, second=0, microsecond=0)

    def _roll_candle(self, u: str, now: datetime, spot: float) -> bool:
        """Feed one tick; returns True when this tick CLOSED a candle."""
        start = self._candle_start(now)
        p = self.pending[u]
        closed = False
        if p is not None and p["start"] != start.isoformat():
            self.bars[u].append([p["start"], p["o"], p["h"], p["l"], p["c"]])
            self.bars[u] = self.bars[u][-self._keep_bars():]
            self.pending[u] = None
            closed = True
        cur = self.pending[u]
        if cur is None:
            self.pending[u] = {"start": start.isoformat(), "o": spot, "h": spot,
                               "l": spot, "c": spot}
        else:
            cur["h"] = max(cur["h"], spot)
            cur["l"] = min(cur["l"], spot)
            cur["c"] = spot
        return closed

    def _supertrend(self, u: str) -> tuple[float | None, float | None]:
        """(direction, line) of the LAST CLOSED candle, or (None, None) until warm."""
        rows = self.bars[u]
        if len(rows) <= self.st_period:
            return None, None
        df = pd.DataFrame(rows, columns=["start", "open", "high", "low", "close"])
        st = _supertrend_bars(df, self.st_period, self.st_multiplier)
        d, line = st.iloc[-1]["direction"], st.iloc[-1]["supertrend"]
        if pd.isna(d) or pd.isna(line):
            return None, None
        return float(d), float(line)

    # --------------------------------------------------------------- pivots
    def _refresh_pivots(self, u: str, today: date) -> None:
        """Floor pivots from the PRIOR TRADING DAY (P=(H+L+C)/3, R1=2P−L, S1=2P−H), once per
        day per underlying. Prefers the official daily provider (broker-fresh in live); if the
        provider returns a STALE prior day — one that isn't the actual adjacent trading day,
        e.g. an unrefreshed cache — it falls back to the strategy's own live-built 15-min bars
        when THOSE are current, else leaves pivots ungated and alerts once (never trades off a
        stale pivot). The staleness guard engages ONLY when the provider surfaces a ``date``
        (live wiring); the dateless backtest provider short-circuits below, so the backtest
        path is byte-identical."""
        piv = self.pivots[u]
        if piv is not None and piv.get("day") == today.isoformat():
            return
        # Call the daily provider at most ONCE per day per underlying (an ungated day must not
        # re-hit the broker every tick); cache the (day, row) result.
        row = None
        if self._daily_ohlc_fn is not None:
            cached = self._daily_ohlc_cache.get(u)
            if cached is not None and cached[0] == today.isoformat():
                row = cached[1]
            else:
                try:
                    row = self._daily_ohlc_fn(u, today)
                except Exception:  # pragma: no cover - provider hiccup → fallback below
                    row = None
                self._daily_ohlc_cache[u] = (today.isoformat(), row)
        # BACKTEST / dateless provider → trusted (its data is never stale). Short-circuit
        # BEFORE any holiday calc so the backtest is byte-identical.
        if row and row.get("date") is None:
            self._set_pivots(u, today, float(row["high"]), float(row["low"]), float(row["close"]))
            return

        from skas_algo.live.holidays import previous_trading_day
        expected = previous_trading_day(today).isoformat()
        stale_date = None
        if row:  # LIVE provider row carries a date → freshness-gate it
            rd = str(row["date"])[:10]
            if rd >= expected:
                self._set_pivots(u, today, float(row["high"]), float(row["low"]),
                                 float(row["close"]))
                return
            stale_date = rd  # a pre-adjacent (stale) prior day — don't use it

        by_day: dict[str, list[list]] = {}
        for b in self.bars[u]:
            by_day.setdefault(b[0][:10], []).append(b)
        prior = sorted(d for d in by_day if d < today.isoformat())

        if stale_date is not None:
            if prior and prior[-1] >= expected:  # our own live bars ARE current → use them
                rows = by_day[prior[-1]]
                self._set_pivots(u, today, max(r[2] for r in rows), min(r[3] for r in rows),
                                 rows[-1][4])
                self._notify_stale_once(u, today, stale_date, expected, "using bar-derived pivots")
            else:  # nothing current anywhere → gate entries rather than trade stale numbers
                self._notify_stale_once(u, today, stale_date, expected, "entries gated for the day")
            return

        # No provider row (cache-source live / SENSEX) → EXISTING bar fallback, unchanged.
        if not prior:
            return  # cold start — no pivots yet, entries stay gated
        rows = by_day[prior[-1]]
        self._set_pivots(u, today, max(r[2] for r in rows), min(r[3] for r in rows), rows[-1][4])

    def _set_pivots(self, u: str, today: date, hi: float, lo: float, close: float) -> None:
        """P=(H+L+C)/3; R1 = 2P − LOW, S1 = 2P − HIGH (breakout-gate direction, validated vs
        TradingView — swapping inverts the gate into a near-no-op)."""
        p = (hi + lo + close) / 3.0
        self.pivots[u] = {"day": today.isoformat(), "p": p, "r1": 2 * p - lo, "s1": 2 * p - hi}

    def _notify_stale_once(self, u: str, today: date, stale_date: str, expected: str,
                           action: str) -> None:
        if self._stale_alerted.get(u) == today.isoformat():
            return
        self._stale_alerted[u] = today.isoformat()
        if self._notify_fn is not None:
            self._notify_fn(u, f"daily pivot source stale for {u} (prior day {stale_date}, "
                               f"expected ≥ {expected}) — {action}")

    # --------------------------------------------------------------- expiry
    def _weekly_expiry(self, ctx, u: str, today: date) -> date | None:
        """Nearest listed weekly ≥ today+min_dte: cached chain expiries when available
        (NIFTY), else the calendar weekday (SENSEX — live-only, nothing cached)."""
        floor_d = today + timedelta(days=self.min_dte)
        chain = ctx.option_chain()
        if chain is not None:
            try:
                listed = [date.fromisoformat(str(e)[:10]) for e in chain.expiries(u, today)]
                listed = [e for e in listed if e >= floor_d]
                if listed:
                    return min(listed)
            except Exception:  # pragma: no cover - fall through to the calendar
                pass
        wd = expiry_weekday_for(u, today, "weekly")
        if wd is None:
            return None
        return floor_d + timedelta(days=(wd - floor_d.weekday()) % 7)

    # ---------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()
        spot_fn = getattr(ctx.market, "index_spot", None)
        if spot_fn is None:
            return []

        if self.day_iso != today.isoformat():
            self.day_iso = today.isoformat()
            self.entries_today = {u: 0 for u in self.underlyings}

        signals: list[Signal] = []
        for u in self.underlyings:
            spot = spot_fn(u)
            leg = self._live_leg(ctx, u)

            # 15:20 forced exit fires regardless of candle/tick state.
            if leg is not None and now.time() >= self.eod_exit:
                signals.append(Signal(leg["symbol"], SignalAction.EXIT_ALL, reason="eod_1520"))
                self.open_leg[u] = None
                continue

            if spot is None or bad_close(spot):
                continue  # no tick for this name (e.g. SENSEX on a cache source)
            if not (_MARKET_OPEN <= now.time() <= time(15, 30)):
                continue

            candle_closed = self._roll_candle(u, now, float(spot))
            self._refresh_pivots(u, today)

            last_start = self.bars[u][-1][0] if self.bars[u] else None
            fresh_close = candle_closed and last_start != self.evaluated_candle.get(u)
            if not fresh_close:
                continue
            self.evaluated_candle[u] = last_start
            # Yesterday's LAST candle closes on today's FIRST tick (it was pending
            # overnight). Its close is stale and gap-distorted relative to today's pivots
            # — never a valid entry signal (TradingView semantics: first evaluable close
            # is 09:30). Exits are unaffected: the book is always flat overnight (15:20).
            if last_start is not None and last_start[:10] != today.isoformat():
                continue

            st_dir, st_line = self._supertrend(u)
            if st_dir is None:
                continue
            close = self.bars[u][-1][4]

            # EXIT on flip against the open leg — and per the owner's rule, a flip candle
            # never re-enters: the fresh signal must come on a LATER closed candle.
            if leg is not None:
                against = (leg["right"] == "PE" and st_dir < 0) or \
                          (leg["right"] == "CE" and st_dir > 0)
                if against:
                    signals.append(Signal(leg["symbol"], SignalAction.EXIT_ALL,
                                          reason="st_flip"))
                    self.open_leg[u] = None
                continue  # holding (or just flipped) → no entry evaluation this candle

            # ENTRY gates: cap, cutoff, pivots present.
            piv = self.pivots[u]
            if (piv is None or self.entries_today[u] >= self.max_trades_per_day
                    or now.time() >= self.entry_cutoff):
                continue
            if st_dir > 0 and close > st_line and close > piv["r1"]:
                right = "PE"    # bullish momentum above R1 → sell the ATM put
            elif st_dir < 0 and close < st_line and close < piv["s1"]:
                right = "CE"    # bearish momentum below S1 → sell the ATM call
            else:
                continue
            sig = self._enter(ctx, u, right, float(spot), today)
            if sig is not None:
                signals.append(sig)
        return signals

    def _enter(self, ctx, u: str, right: str, spot: float, today: date) -> Signal | None:
        expiry = self._weekly_expiry(ctx, u, today)
        if expiry is None:
            return None
        try:
            per_lot = lot_size_for(u, expiry, overrides=self.lot_overrides)
        except KeyError:
            return None
        step = _STRIKE_STEP.get(u, 50)
        strike = float(round(spot / step) * step)
        symbol = make(u, expiry, strike, right, lot_size=per_lot,
                      lot_overrides=self.lot_overrides).symbol
        try:
            premium = ctx.close(symbol)
        except KeyError:
            return None  # no quote yet — the next candle can retry with fresh conditions
        if bad_close(premium):
            return None
        units = self.lots.get(u, 1) * per_lot
        self.open_leg[u] = {"symbol": symbol, "right": right, "units": float(units),
                            "entry_close": float(premium)}
        self.entries_today[u] += 1
        return Signal(symbol, SignalAction.ENTER_SHORT, quantity=int(units),
                      reason=f"mtg_{'bull' if right == 'PE' else 'bear'}",
                      meta={"multiplier": 1})

    def _live_leg(self, ctx, u: str) -> dict | None:
        """The open leg IF the engine still holds it (engine-side settles/stops clear it)."""
        leg = self.open_leg[u]
        if leg is None:
            return None
        if not ctx.lots(leg["symbol"]):
            self.open_leg[u] = None
            return None
        return leg

    # -------------------------------------------------------------- monitor
    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        """Per-underlying live monitor for the Live page."""
        names = []
        for u in self.underlyings:
            st_dir, st_line = self._supertrend(u)
            piv = self.pivots[u] or {}
            leg = self.open_leg[u]
            names.append({
                "name": u,
                "spot": getattr(market, "index_spot", lambda _u: None)(u),
                "st_dir": st_dir, "st_line": st_line,
                "r1": piv.get("r1"), "s1": piv.get("s1"),
                "bars": len(self.bars[u]),
                "entries_today": self.entries_today[u],
                "max_trades": self.max_trades_per_day,
                "leg": dict(leg) if leg else None,
            })
        return {"kind": "momentum_theta", "names": names}

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "bars": {u: [list(b) for b in v] for u, v in self.bars.items()},
            "pending": {u: (dict(p) if p else None) for u, p in self.pending.items()},
            "open_leg": {u: (dict(v) if v else None) for u, v in self.open_leg.items()},
            "entries_today": dict(self.entries_today),
            "day_iso": self.day_iso,
            "pivots": {u: (dict(p) if p else None) for u, p in self.pivots.items()},
            "evaluated_candle": dict(self.evaluated_candle),
            "seeded": self._seeded,
        }

    def load_state(self, state: dict) -> None:
        for u in self.underlyings:
            self.bars[u] = [list(b) for b in (state.get("bars", {}).get(u) or [])]
            self.pending[u] = state.get("pending", {}).get(u) or None
            self.open_leg[u] = state.get("open_leg", {}).get(u) or None
            self.pivots[u] = state.get("pivots", {}).get(u) or None
            self.evaluated_candle[u] = state.get("evaluated_candle", {}).get(u)
            self.entries_today[u] = int(state.get("entries_today", {}).get(u, 0))
        self.day_iso = state.get("day_iso")
        self._seeded = bool(state.get("seeded", False))
