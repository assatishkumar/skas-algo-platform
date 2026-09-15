"""supertrend_spread — hourly SuperTrend flip on NIFTY spot traded through monthly credit
spreads (positional, defined risk). Owner spec 2026-09-15; the 21_ema_momentum shape with
an intraday signal.

The one-line intuition: **the SuperTrend line is already the invalidation level, so the
short strike goes behind it** — sell the premium on the side the trend just left.

- SuperTrend(ATR ``atr_period``, ``multiplier``) on ``timeframe``-minute NIFTY bars
  anchored to 09:15, built by the strategy itself from the index spot it is fed every
  slice (the momentum_theta pattern). 60m: 09:15 · 10:15 · 11:15 · 12:15 · 13:15 · 14:15,
  six bars a day — the 14:15 bar is EVALUATED at 15:15 (its 60-minute close) and the
  15:15–15:30 stub is then MERGED into it for the indicator's history, no seventh bar, no
  second decision. 120m: three bars; 240m: two (09:15–13:15, 13:15–close, evaluated at
  15:15); ≥375m: one daily bar evaluated at 15:15.
- A flip = the direction on the just-closed bar ≠ the prior bar's; ``confirm_bars`` (1)
  further closed bars in the new direction make it actionable (0 = trade the flip bar).
- Bullish → BULL PUT SPREAD, short strike = the highest ``strike_step`` multiple AT OR
  BELOW the SuperTrend line; bearish → BEAR CALL SPREAD, short strike = the lowest multiple
  AT OR ABOVE it. Long leg 300–500 pts further out; net credit ₹80–140/share (₹90–130
  ideal preferred). If the line-anchored strike cannot fit the window, the short strike
  steps one strike TOWARD spot, at most ``max_strike_steps`` times; still nothing → skip
  and retry at every later bar close while the direction holds.
- Monthly expiry: before the 15th → current month, on/after → next month.
- Exits: the opposite confirmed flip closes AND reverses in the same decision — unless it
  comes within ``min_hold_bars`` closed bars of entry, in which case the book goes FLAT
  and waits for the next fresh signal (the whipsaw brake). Optional ``take_profit_pct``
  (whole percent of the entry credit, 0 = off) checked at every bar close; with
  ``tp_rollover`` a banked target re-enters at once, same direction, on the NEXT month's
  expiry (owner 2026-09-15: "once we take 75% profit, roll over"). Never into
  expiry week: exit ``roll_days_before`` calendar days before expiry and, if the direction
  still holds, re-enter next month in the same decision.
- No premium stop: the reverse signal IS the stop, and the long leg caps the tail.

Mode notes: the candles come from ``ctx.market.index_spot`` — the replay's de-carried
parity spot and the live view's index LTP — so the backtest and a deployment build the
same bars from the same feed (§3). Live warm-up: ``seed_intraday_bars`` aggregates Kite's
15-min candles into these buckets at deploy/recovery. A restart carries the bars in
``export_state``. The margin model charges shorts only, so reported margin reads ≈2× the
broker's for this defined-risk spread (the ema21 caveat).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd

from skas_algo.engine.indicators.supertrend import _supertrend_bars
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import SkipReasonMixin

SESSION_OPEN_MIN = 9 * 60 + 15          # 09:15
SESSION_MINUTES = 375                   # 09:15 → 15:30


def _bad(x) -> bool:
    return x is None or x != x or x <= 0  # None / NaN / non-positive


class SuperTrendSpreadStrategy(SkipReasonMixin):
    strategy_id = "supertrend_spread"
    intraday = True  # ticks every refresh live; the bar boundaries pace the decisions

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 500_000,
        underlying: str | None = None,
        lots: int = 1,
        timeframe: int = 60,             # minutes per bar; anchor stays 09:15
        atr_period: int = 10,
        multiplier: float = 3.0,
        confirm_bars: int = 1,           # 0 = trade the flip bar itself
        min_hold_bars: int = 3,          # a reversal inside this → flat, not reverse
        strike_step: int = 100,          # 100-point strikes ONLY
        width_min: int = 300,
        width_max: int = 500,
        credit_min: float = 80.0,        # acceptable window, per share
        credit_max: float = 140.0,
        credit_ideal_lo: float = 90.0,   # preferred window
        credit_ideal_hi: float = 130.0,
        max_strike_steps: int = 2,       # toward spot when the line strike does not fit
        take_profit_pct: float = 0.0,    # whole % of the entry credit; 0 = off
        tp_rollover: bool = False,       # after a take-profit, re-enter at once on NEXT month
        expiry_switch_day: int = 15,
        roll_days_before: int = 5,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.lots = max(1, int(lots))
        self.timeframe = max(5, int(timeframe))
        self.atr_period = max(2, int(atr_period))
        self.multiplier = float(multiplier)
        self.confirm_bars = max(0, int(confirm_bars))
        self.min_hold_bars = max(0, int(min_hold_bars))
        self.strike_step = int(strike_step)
        self.width_min = int(width_min)
        self.width_max = int(width_max)
        self.credit_min = float(credit_min)
        self.credit_max = float(credit_max)
        self.credit_ideal_lo = float(credit_ideal_lo)
        self.credit_ideal_hi = float(credit_ideal_hi)
        self.max_strike_steps = max(0, int(max_strike_steps))
        self.take_profit_pct = float(take_profit_pct)
        self.tp_rollover = bool(tp_rollover)
        self.expiry_switch_day = int(expiry_switch_day)
        self.roll_days_before = int(roll_days_before)
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # ---- state (all persisted for live recovery) ----
        self.bars: list[list] = []             # closed bars: [start_iso, o, h, l, c]
        self.pending: dict | None = None       # the forming bar
        self.evaluated_start: str | None = None  # the bar last evaluated (the stub merge)
        self.bars_closed: int = 0              # monotonic count of evaluated bars
        self.last_dir: float | None = None     # SuperTrend direction on the last bar
        self.pending_signal: dict | None = None  # {"dir": "bull"|"bear", "seen": n}
        self.direction: str | None = None      # the ARMED side (None = wait for a flip)
        self.armed: bool = False               # may (re)enter while flat
        self.legs: list[dict] = []             # [{symbol, dir, units, entry}]
        self.entry_credit: float = 0.0
        self.entry_expiry: date | None = None
        self.entry_bar: int | None = None      # bars_closed at entry (min_hold_bars)
        self.min_expiry: date | None = None    # after a TP rollover: the next entry's expiry
                                               # must be LATER than the one just banked
        self.last_line: float | None = None
        self._seeded = False

    # ---------------------------------------------------------------- warm-up
    def _bars_per_day(self) -> int:
        """Bars a session holds: 60m → 6 (the 15-min stub merged into the 14:15 bar),
        120m → 3, 240m → 2 (09:15–13:15 and 13:15–close, the exchange's own 4h split),
        ≥375m → 1 (a daily bar). Rounded, so a bar shorter than half a step is merged and
        one longer stands on its own."""
        return max(1, round(SESSION_MINUTES / self.timeframe))

    def _tail_eval_minute(self) -> int:
        """Minutes after 09:15 at which the session's LAST bar is evaluated: its own
        boundary, capped at 15:15 (360) so a bar that would run past the close is read at
        15:15 with the stub merged afterwards."""
        return min(self._bars_per_day() * self.timeframe, SESSION_MINUTES - 15)

    def _keep_bars(self) -> int:
        """SuperTrend's band ratchet is path-dependent, so the window must be deep enough
        for the carry to converge — ~40 sessions of bars (the momentum_theta rule scaled
        to the bars a day), never fewer than 24 ATR periods."""
        return max(40 * self._bars_per_day(), 24 * self.atr_period)

    def spot_symbols(self) -> list[str]:
        """The live loop feeds this name's index spot every tick."""
        return [self.underlying]

    def seed_intraday_bars(self, fetch) -> None:
        """Warm the SuperTrend with real history at deploy/recovery. ``fetch(underlying,
        days, minutes)`` -> [{start, open, high, low, close}, ...]; Kite's 15-min candles
        are aggregated into THESE buckets (Kite's own 60-min series has a separate 15:15
        stub bar, which is not this strategy's convention). Idempotent."""
        if self._seeded:
            return
        try:
            hist = fetch(self.underlying, 60, 15) or []
        except Exception:  # pragma: no cover - warm-up is best-effort, never fatal
            hist = []
        buckets: dict[str, list] = {}
        for h in hist:
            try:
                start = h["start"]
                ts = start if isinstance(start, datetime) else datetime.fromisoformat(str(start))
                key = self._bucket_start(ts.replace(tzinfo=None)).isoformat()
                o, hi, lo, c = (float(h["open"]), float(h["high"]), float(h["low"]),
                                float(h["close"]))
            except (KeyError, TypeError, ValueError):
                continue
            b = buckets.get(key)
            if b is None:
                buckets[key] = [key, o, hi, lo, c]
            else:
                b[2] = max(b[2], hi)
                b[3] = min(b[3], lo)
                b[4] = c
        have = {b[0] for b in self.bars}
        rows = [b for k, b in sorted(buckets.items()) if k not in have]
        self.bars = sorted(self.bars + rows, key=lambda b: b[0])[-self._keep_bars():]
        self._seeded = True

    # ---------------------------------------------------------------- candles
    def _bucket_start(self, now: datetime) -> datetime:
        """The 09:15-anchored bar this minute belongs to; the last bucket absorbs the
        session's tail (60m: 14:15 → 15:30)."""
        mins = now.hour * 60 + now.minute - SESSION_OPEN_MIN
        mins = max(0, mins)
        idx = min(mins // self.timeframe, self._bars_per_day() - 1)
        return now.replace(hour=9, minute=15, second=0, microsecond=0) + timedelta(
            minutes=idx * self.timeframe)

    def _feed(self, now: datetime, spot: float) -> bool:
        """Feed one tick. Returns True when a bar should be EVALUATED on this tick: the
        first tick at/after a boundary (the bar that just ended is closed into history),
        or the first tick of a new day (the tail bar closes with the stub merged)."""
        start = self._bucket_start(now).isoformat()
        p = self.pending
        evaluate = False
        if p is not None and p["start"] != start:
            # the pending bar is over: into history (merging the stub if it was already
            # evaluated at its boundary — then it is not evaluated again)
            self.bars.append([p["start"], p["o"], p["h"], p["l"], p["c"]])
            self.bars = self.bars[-self._keep_bars():]
            evaluate = self.evaluated_start != p["start"]
            self.pending = None
        cur = self.pending
        if cur is None:
            self.pending = {"start": start, "o": spot, "h": spot, "l": spot, "c": spot}
        else:
            cur["h"] = max(cur["h"], spot)
            cur["l"] = min(cur["l"], spot)
            cur["c"] = spot
        # the session's LAST bar is evaluated at its own boundary (or 15:15 when it would
        # run past the close) while the tail keeps feeding it — the first tick at/after
        # that minute, once (`evaluated_start`); a live tick a few seconds late still
        # counts, an exact-minute test would have missed it
        mins = now.hour * 60 + now.minute - SESSION_OPEN_MIN
        if (not evaluate and self.pending is not None and self.pending["start"] == start
                and self.evaluated_start != start
                and mins // self.timeframe >= self._bars_per_day() - 1
                and mins >= self._tail_eval_minute()):
            evaluate = True
            self._provisional = True
            return evaluate
        self._provisional = False
        return evaluate

    def _history(self) -> list[list]:
        """Bars for the indicator: the closed ones, plus the pending bar when it is being
        evaluated at its boundary before the stub lands."""
        if getattr(self, "_provisional", False) and self.pending is not None:
            p = self.pending
            return [*self.bars, [p["start"], p["o"], p["h"], p["l"], p["c"]]]
        return self.bars

    def _supertrend(self, rows: list[list]) -> tuple[float | None, float | None]:
        """(direction, line) on the LAST bar, (None, None) until warm."""
        if len(rows) <= self.atr_period + 1:
            return None, None
        df = pd.DataFrame(rows, columns=["start", "open", "high", "low", "close"])
        st = _supertrend_bars(df, self.atr_period, self.multiplier)
        d, line = st.iloc[-1]["direction"], st.iloc[-1]["supertrend"]
        if pd.isna(d) or pd.isna(line):
            return None, None
        return float(d), float(line)

    # ------------------------------------------------------------------ slice
    def on_slice(self, ctx) -> list[Signal]:
        now = ctx.now()
        if now is None:
            return []
        market = getattr(ctx, "market", None)
        spot_fn = getattr(market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn is not None else None
        if spot is None or _bad(spot):
            return []
        if not self._feed(now, float(spot)):
            return []
        # ---- a bar just closed: one decision ----
        today = ctx.today()
        rows = self._history()
        evaluated = rows[-1][0] if rows else None
        self.evaluated_start = evaluated
        self.bars_closed += 1
        d, line = self._supertrend(rows)
        if d is None:
            return []
        self.last_line = line
        chain = ctx.option_chain()
        if chain is None:
            self.last_dir = d
            return []

        # Engine closed our legs (settlement backstop)? Reset the position.
        if self.legs and not any(ctx.lots(leg["symbol"]) for leg in self.legs):
            self._flat()

        # flip → confirmation window → actionable signal (fires once)
        signal: str | None = None
        want = "bull" if d > 0 else "bear"
        if self.last_dir is not None and d != self.last_dir:
            self.pending_signal = {"dir": want, "seen": 0}
        elif self.pending_signal is not None:
            if self.pending_signal["dir"] == want:
                self.pending_signal["seen"] += 1
            else:
                self.pending_signal = None
        if self.pending_signal is not None and self.pending_signal["seen"] >= self.confirm_bars:
            signal = self.pending_signal["dir"]
            self.pending_signal = None
        self.last_dir = d

        signals: list[Signal] = []

        # 1. the opposite confirmed flip: reverse — or, inside min_hold_bars, just go flat
        if signal is not None and signal != self.direction:
            self.min_expiry = None                    # a fresh signal picks its own month
            if self.legs:
                held = self.bars_closed - (self.entry_bar or self.bars_closed)
                if held < self.min_hold_bars:
                    signals += self._exit_all("whipsaw")
                    self.direction, self.armed = None, False
                else:
                    signals += self._exit_all("reverse")
                    self.direction, self.armed = signal, True
            else:
                self.direction, self.armed = signal, True
        elif signal is not None and not self.legs:
            self.armed = True                 # a re-affirming flip while flat re-arms

        # 2. take profit (whole % of the entry credit) — direction holds, but no re-entry
        #    until the next fresh signal
        if self.legs and self.take_profit_pct > 0 and self.entry_credit > 0:
            value = self._spread_value(ctx)
            if value is not None:
                profit = self.entry_credit - value
                if profit >= self.take_profit_pct / 100.0 * self.entry_credit:
                    banked = self.entry_expiry
                    signals += self._exit_all("target")
                    if self.tp_rollover:
                        # owner 2026-09-15: a banked 75% rolls straight into NEXT month —
                        # same direction, the expiry after the one just closed
                        self.armed = True
                        self.min_expiry = banked
                    else:
                        self.armed = False

        # 3. rollover: never into expiry week; the still-armed direction re-enters below
        if self.legs and self.entry_expiry is not None and \
                (self.entry_expiry - today).days <= self.roll_days_before:
            signals += self._exit_all("roll")

        # 4. flat and armed → build the spread behind the line
        if self.direction is not None and self.armed and not self.legs:
            signals += self._try_enter(ctx, chain, today, line)
        return signals

    def _spread_value(self, ctx) -> float | None:
        """What closing the spread would cost per share now (short leg − long leg)."""
        try:
            total = 0.0
            for leg in self.legs:
                px = ctx.close(leg["symbol"])
                if _bad(px):
                    return None
                total += -leg["dir"] * float(px)
            return total
        except Exception:
            return None

    # ------------------------------------------------------------------ entry
    def _target_expiry(self, chain, today: date) -> date | None:
        """Monthly per the before/after-15th rule: the LAST listed expiry of the target
        month; too close to the roll → the following month."""
        listed = chain.expiries(self.underlying, today)
        if not listed:
            return None
        y, m = today.year, today.month
        if today.day >= self.expiry_switch_day:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        for _ in range(4):
            month_exps = [e for e in listed if (e.year, e.month) == (y, m) and e >= today]
            if month_exps:
                exp = max(month_exps)
                if ((exp - today).days > self.roll_days_before
                        and (self.min_expiry is None or exp > self.min_expiry)):
                    return exp
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None

    def _try_enter(self, ctx, chain, today: date, line: float) -> list[Signal]:
        expiry = self._target_expiry(chain, today)
        if expiry is None:
            return self._skip("no usable monthly expiry", today)
        spot = chain.spot(self.underlying, today)
        if spot is None or _bad(spot):
            return self._skip("no spot", today)
        rows = {(r.strike, r.right): r
                for r in chain.chain(self.underlying, today, expiry)}
        right = "PE" if self.direction == "bull" else "CE"
        combo = self._find_spread(rows, right, float(spot), float(line))
        if combo is None:
            return self._skip(f"no {right} spread in the credit window behind the line "
                              f"({line:.0f})", today)
        sell_row, buy_row, credit = combo
        try:
            per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return self._skip("no lot size for the expiry", today)
        units = self.lots * per_lot
        self.legs = [
            {"symbol": sell_row.symbol, "dir": -1, "units": float(units),
             "entry": float(sell_row.close)},
            {"symbol": buy_row.symbol, "dir": 1, "units": float(units),
             "entry": float(buy_row.close)},
        ]
        self.entry_credit = float(credit)
        self.entry_expiry = expiry
        self.entry_bar = self.bars_closed
        self._entered()
        reason = f"st_{self.direction}"
        return [
            Signal(sell_row.symbol, SignalAction.ENTER_SHORT, quantity=int(units),
                   reason=reason, meta={"multiplier": 1}),
            Signal(buy_row.symbol, SignalAction.ENTER_LONG, quantity=int(units),
                   reason=reason, meta={"multiplier": 1}),
        ]

    def _find_spread(self, rows, right: str, spot: float, line: float):
        """(sell_row, buy_row, net_credit) — the spread behind the SuperTrend line.

        The short strike starts at the line (puts: the highest multiple ≤ line; calls: the
        lowest ≥ line) and, when no width fits the credit window there, steps TOWARD spot
        up to ``max_strike_steps`` times, never past the first OTM strike. At the first
        strike that fits, the width is chosen by the ideal rule (closest to the ideal
        midpoint, ideal window preferred). Both legs need a print and oi > 0."""
        step = self.strike_step
        sign = -1 if right == "PE" else 1            # further OTM
        anchor = (math.floor(line / step) * step if right == "PE"
                  else math.ceil(line / step) * step)
        # the nearest OTM multiple — the short strike may not cross it toward spot
        first_otm = (math.floor(spot / step) * step if right == "PE"
                     else math.ceil(spot / step) * step)
        if first_otm == spot:
            first_otm += sign * step
        # the line sits on the trend side of spot; a line-anchored strike that is not OTM
        # (a very tight band) is pulled back to the first OTM strike
        if (right == "PE" and anchor >= spot) or (right == "CE" and anchor <= spot):
            anchor = first_otm
        ideal_mid = (self.credit_ideal_lo + self.credit_ideal_hi) / 2.0
        for i in range(self.max_strike_steps + 1):
            sell_k = anchor - sign * i * step            # toward spot
            if (right == "PE" and sell_k > first_otm) or (right == "CE" and sell_k < first_otm):
                break
            sell = rows.get((float(sell_k), right))
            if sell is None or _bad(sell.close) or not (sell.oi or 0) > 0:
                continue
            best_ideal = best_ok = None
            for width in range(self.width_max, self.width_min - step, -step):
                buy_k = sell_k + sign * width
                buy = rows.get((float(buy_k), right))
                if buy is None or _bad(buy.close) or not (buy.oi or 0) > 0:
                    continue
                credit = float(sell.close) - float(buy.close)
                if not (self.credit_min <= credit <= self.credit_max):
                    continue
                cand = (abs(credit - ideal_mid), sell, buy, credit)
                if self.credit_ideal_lo <= credit <= self.credit_ideal_hi:
                    if best_ideal is None or cand[0] < best_ideal[0]:
                        best_ideal = cand
                elif best_ok is None or cand[0] < best_ok[0]:
                    best_ok = cand
            pick = best_ideal or best_ok
            if pick:
                return pick[1], pick[2], pick[3]
        return None

    # ------------------------------------------------------------------ exits
    def _exit_all(self, reason: str) -> list[Signal]:
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason)
                for leg in self.legs]
        self._flat()
        return sigs

    def _flat(self) -> None:
        self.legs = []
        self.entry_credit = 0.0
        self.entry_expiry = None
        self.entry_bar = None

    # ---------------------------------------------------------------- monitor
    def st_status(self, market, portfolio, margin: float | None = None) -> dict:
        out: dict = {"kind": "supertrend_spread", "direction": self.direction,
                     "armed": self.armed, "line": self.last_line,
                     "st_dir": self.last_dir, "bars": len(self.bars),
                     "entry_credit": self.entry_credit,
                     "expiry": self.entry_expiry.isoformat() if self.entry_expiry else None,
                     "legs": [dict(leg) for leg in self.legs]}
        try:
            if self.entry_expiry and getattr(market, "current_date", None):
                out["days_to_roll"] = ((self.entry_expiry - market.current_date).days
                                       - self.roll_days_before)
            if self.entry_bar is not None:
                out["bars_held"] = self.bars_closed - self.entry_bar
        except Exception:  # pragma: no cover - monitoring must never break a snapshot
            pass
        return out

    basket_status = st_status

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "bars": [list(b) for b in self.bars],
            "pending": dict(self.pending) if self.pending else None,
            "evaluated_start": self.evaluated_start, "bars_closed": self.bars_closed,
            "last_dir": self.last_dir,
            "pending_signal": dict(self.pending_signal) if self.pending_signal else None,
            "direction": self.direction, "armed": self.armed,
            "legs": [dict(leg) for leg in self.legs],
            "entry_credit": self.entry_credit,
            "entry_expiry": self.entry_expiry.isoformat() if self.entry_expiry else None,
            "entry_bar": self.entry_bar, "last_line": self.last_line,
            "min_expiry": self.min_expiry.isoformat() if self.min_expiry else None,
        }

    def load_state(self, state: dict) -> None:
        self.bars = [list(b) for b in state.get("bars", [])]
        self.pending = dict(state["pending"]) if state.get("pending") else None
        self.evaluated_start = state.get("evaluated_start")
        self.bars_closed = int(state.get("bars_closed", 0))
        self.last_dir = state.get("last_dir")
        ps = state.get("pending_signal")
        self.pending_signal = dict(ps) if ps else None
        self.direction = state.get("direction")
        self.armed = bool(state.get("armed", False))
        self.legs = [dict(leg) for leg in state.get("legs", [])]
        self.entry_credit = float(state.get("entry_credit", 0.0))
        ee = state.get("entry_expiry")
        self.entry_expiry = date.fromisoformat(ee) if ee else None
        self.entry_bar = state.get("entry_bar")
        self.last_line = state.get("last_line")
        me = state.get("min_expiry")
        self.min_expiry = date.fromisoformat(me) if me else None
        if self.bars:
            self._seeded = True
