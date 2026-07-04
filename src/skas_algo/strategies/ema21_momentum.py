"""21_ema_momentum — daily EMA(21)-channel credit-spread strategy on NIFTY (positional).

Two EMA(21) lines — one on the daily HIGH, one on the daily LOW — form a channel. Checked
ONCE per day at ``decision_time`` (15:20 IST):

- close crosses ABOVE the upper band (yesterday's close was NOT above yesterday's band)
  → BULL PUT SPREAD (sell higher-strike OTM put + buy lower-strike put);
- close crosses BELOW the lower band (freshness mirrored) → BEAR CALL SPREAD.

Strikes are 100-point multiples only, OTM; the spread is 300–500 points wide; the net
credit must land in ₹80–140 per share (ideal ₹90–130 preferred). No qualifying spread on
signal day → SKIP and retry each 15:20 while the direction stays active (owner's rule:
never take a bad-credit trade). Hold until the OPPOSITE signal (close + reverse in one
decision), and never into expiry week: exit ``roll_days_before`` (5) calendar days before
expiry — if the direction still holds, re-enter next month's expiry in the same decision.
Expiry pick: before the 15th → current month, on/after → next month (monthly only).

Mode notes:
- **Bands INCLUDE today's forming bar** (chart-at-15:20 semantics). The daily OHLC comes
  through ``set_daily_bars_fn`` (wired by the backtest service / live manager — cache OHLC;
  live appends today's intraday H/L + LTP close before the EMA). No hook → no decisions.
- Live DERIV runs tick every ``refresh_seconds`` with NO engine time gate — the strategy
  self-gates on ``ctx.now() >= decision_time`` plus a once-per-day guard; backtest's
  ``ctx.now()`` collapses to 15:30 so both modes decide exactly once per day.
- The margin model charges shorts only (no long-leg offset), so reported margin reads
  ≈2× the real broker requirement for this defined-risk spread (call_ratio caveat).
"""

from __future__ import annotations

import math
from datetime import date, time

from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.types import Signal, SignalAction


def _bad(x) -> bool:
    return x is None or x != x or x <= 0  # None / NaN / non-positive premium


class Ema21MomentumStrategy:
    strategy_id = "21_ema_momentum"
    intraday = True  # live ticks every refresh; the 15:20 self-gate does the pacing

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 500_000,
        underlying: str | None = None,
        lots: int = 1,
        ema_period: int = 21,
        strike_step: int = 100,          # 100-point strikes ONLY (spec: 50s not allowed)
        width_min: int = 300,
        width_max: int = 500,
        credit_min: float = 80.0,        # acceptable window, per share
        credit_max: float = 140.0,
        credit_ideal_lo: float = 90.0,   # preferred window
        credit_ideal_hi: float = 130.0,
        decision_time: str = "15:20",
        expiry_switch_day: int = 15,     # before the 15th → current month, else next
        roll_days_before: int = 5,       # exit this many calendar days before expiry
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.lots = max(1, int(lots))
        self.ema_period = int(ema_period)
        self.strike_step = int(strike_step)
        self.width_min = int(width_min)
        self.width_max = int(width_max)
        self.credit_min = float(credit_min)
        self.credit_max = float(credit_max)
        self.credit_ideal_lo = float(credit_ideal_lo)
        self.credit_ideal_hi = float(credit_ideal_hi)
        try:
            hh, mm = str(decision_time).split(":")
            self.decision_time = time(int(hh), int(mm))
        except Exception:
            self.decision_time = time(15, 20)
        self.expiry_switch_day = int(expiry_switch_day)
        self.roll_days_before = int(roll_days_before)
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # Daily-bars provider: fn(underlying, start, end) -> DataFrame(date, high, low,
        # close), INCLUSIVE of today when today's bar exists. Wired externally.
        self._daily_bars_fn = None

        # ---- state (all persisted for live recovery) ----
        self.direction: str | None = None       # "bull" | "bear" — active signal side
        self.legs: list[dict] = []               # [{symbol, dir, units, entry}]
        self.entry_credit: float = 0.0           # per-share net credit at entry
        self.entry_expiry: date | None = None
        self.last_decision_date: str | None = None

    # ------------------------------------------------------------------ hooks
    def set_daily_bars_fn(self, fn) -> None:
        self._daily_bars_fn = fn

    # ------------------------------------------------------------------ bands
    def _bands(self, today: date) -> dict | None:
        """EMA(21) of daily high/low + closes, for today AND yesterday (the crossover
        freshness test needs both). Today's forming bar is INCLUDED — the provider
        returns it (backtest: the cached bar; live: intraday H/L + LTP close)."""
        if self._daily_bars_fn is None:
            return None
        from datetime import timedelta

        # ~9 months so the EMA is fully converged (weight beyond ~120 bars ≪ 1e-4).
        df = self._daily_bars_fn(self.underlying, today - timedelta(days=270), today)
        if df is None or len(df) < self.ema_period + 2:
            return None
        span = self.ema_period
        upper = df["high"].ewm(span=span, adjust=False).mean()
        lower = df["low"].ewm(span=span, adjust=False).mean()
        close = df["close"]
        return {
            "upper": float(upper.iloc[-1]), "lower": float(lower.iloc[-1]),
            "close": float(close.iloc[-1]),
            "prev_upper": float(upper.iloc[-2]), "prev_lower": float(lower.iloc[-2]),
            "prev_close": float(close.iloc[-2]),
        }

    # ------------------------------------------------------------------ slice
    def on_slice(self, ctx) -> list[Signal]:
        chain = ctx.option_chain()
        if chain is None:
            return []
        today = ctx.today()

        # Once per day, at/after decision_time (live ticks all day; backtest now()=15:30).
        now = ctx.now()
        if now is not None and now.time() < self.decision_time:
            return []
        if self.last_decision_date == today.isoformat():
            return []

        # Engine closed our legs (settlement backstop)? Reset position, keep direction —
        # the active signal may re-enter next month per the rollover rule.
        if self.legs and not any(ctx.lots(leg["symbol"]) for leg in self.legs):
            self._flat()

        bands = self._bands(today)
        if bands is None:
            return []  # data hiccup — do NOT latch; the next live tick retries
        self.last_decision_date = today.isoformat()

        # Fresh crossover? (today beyond the band AND yesterday was not)
        signal: str | None = None
        if bands["close"] > bands["upper"] and bands["prev_close"] <= bands["prev_upper"]:
            signal = "bull"
        elif bands["close"] < bands["lower"] and bands["prev_close"] >= bands["prev_lower"]:
            signal = "bear"

        signals: list[Signal] = []

        # Opposite signal while holding → close + reverse in this same decision.
        if signal is not None and signal != self.direction:
            if self.legs:
                signals += self._exit_all("reverse")
            self.direction = signal

        # Rollover: never hold into expiry week — exit roll_days_before days out; the
        # still-active direction re-enters next month's expiry below in this decision.
        if self.legs and self.entry_expiry is not None and \
                (self.entry_expiry - today).days <= self.roll_days_before:
            signals += self._exit_all("roll")

        # Flat with an active direction (fresh signal, credit-miss retry, or post-roll
        # re-entry) → try to build the spread. Never entered same-day after a reverse?
        # Yes — spec says open the new trade in the opposite direction immediately.
        if self.direction is not None and not self.legs:
            signals += self._try_enter(ctx, chain, today)

        return signals

    # ------------------------------------------------------------------ entry
    def _target_expiry(self, chain, today: date) -> date | None:
        """Monthly expiry per the before/after-15th rule: pick the LAST listed expiry of
        the target calendar month (weeklies list earlier in the month; the monthly is the
        month's final expiry). Too close to roll → skip to the following month."""
        listed = chain.expiries(self.underlying, today)
        if not listed:
            return None
        y, m = today.year, today.month
        if today.day >= self.expiry_switch_day:
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        for _ in range(3):  # walk forward if the month has nothing usable
            month_exps = [e for e in listed if (e.year, e.month) == (y, m) and e >= today]
            if month_exps:
                exp = max(month_exps)  # the month's last expiry IS the monthly
                if (exp - today).days > self.roll_days_before:
                    return exp
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None

    def _try_enter(self, ctx, chain, today: date) -> list[Signal]:
        expiry = self._target_expiry(chain, today)
        if expiry is None:
            return []
        spot = chain.spot(self.underlying, today)
        if spot is None or _bad(spot):
            return []
        rows = {(r.strike, r.right): r
                for r in chain.chain(self.underlying, today, expiry)}
        right = "PE" if self.direction == "bull" else "CE"
        combo = self._find_spread(rows, right, float(spot))
        if combo is None:
            return []  # credit window missed — retry at tomorrow's 15:20 (owner's rule)
        sell_row, buy_row, credit = combo

        try:
            per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return []
        units = self.lots * per_lot
        self.legs = [
            {"symbol": sell_row.symbol, "dir": -1, "units": float(units),
             "entry": float(sell_row.close)},
            {"symbol": buy_row.symbol, "dir": 1, "units": float(units),
             "entry": float(buy_row.close)},
        ]
        self.entry_credit = float(credit)
        self.entry_expiry = expiry
        reason = f"ema21_{self.direction}"
        return [
            Signal(sell_row.symbol, SignalAction.ENTER_SHORT, quantity=int(units),
                   reason=reason, meta={"multiplier": 1}),
            Signal(buy_row.symbol, SignalAction.ENTER_LONG, quantity=int(units),
                   reason=reason, meta={"multiplier": 1}),
        ]

    def _find_spread(self, rows, right: str, spot: float):
        """(sell_row, buy_row, net_credit) — the best OTM ``strike_step``-multiple spread.

        Walks the SELL strike outward from the first OTM multiple (puts: below spot;
        calls: above) and the width from wide to narrow; collects every combo whose net
        credit sits in the acceptable window [credit_min, credit_max]; prefers combos in
        the ideal window (closest to its midpoint), else the acceptable combo closest to
        the ideal midpoint. Both legs need real prints and oi > 0 (phantom-strike guard).
        Nothing fits → None (skip day)."""
        step = self.strike_step
        sign = -1 if right == "PE" else 1  # walk direction (puts go down, calls up)
        first = math.floor(spot / step) * step if right == "PE" \
            else math.ceil(spot / step) * step
        if first == spot:  # exactly ATM is not OTM — step away once
            first += sign * step
        ideal_mid = (self.credit_ideal_lo + self.credit_ideal_hi) / 2.0
        best_ideal = best_ok = None  # (distance-to-ideal-mid, sell, buy, credit)

        for i in range(12):  # sell strikes: up to 1200 points OTM
            sell_k = first + sign * i * step
            sell = rows.get((float(sell_k), right))
            if sell is None or _bad(sell.close) or not (sell.oi or 0) > 0:
                continue
            for width in range(self.width_max, self.width_min - step, -step):
                buy_k = sell_k + sign * width  # further OTM by `width`
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
        return (pick[1], pick[2], pick[3]) if pick else None

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

    # ------------------------------------------------------------------ monitor
    def ema_status(self, market, portfolio, margin: float | None = None) -> dict:
        """Live-page snapshot: bands vs close, active direction, spread + roll runway."""
        out: dict = {"kind": "ema21", "direction": self.direction,
                     "entry_credit": self.entry_credit,
                     "expiry": self.entry_expiry.isoformat() if self.entry_expiry else None,
                     "legs": [dict(leg) for leg in self.legs]}
        try:
            today = market.current_date
            bands = self._bands(today)
            if bands:
                out.update({k: bands[k] for k in ("upper", "lower", "close")})
            if self.entry_expiry:
                out["days_to_roll"] = (self.entry_expiry - today).days - self.roll_days_before
        except Exception:  # pragma: no cover - monitoring must never break a snapshot
            pass
        return out

    # The live snapshot's generic monitor hook (UI gates the donchian panel by
    # strategy_id, so carrying our dict under snap["basket"] is safe + inspectable).
    basket_status = ema_status

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "direction": self.direction,
            "legs": [dict(leg) for leg in self.legs],
            "entry_credit": self.entry_credit,
            "entry_expiry": self.entry_expiry.isoformat() if self.entry_expiry else None,
            "last_decision_date": self.last_decision_date,
        }

    def load_state(self, state: dict) -> None:
        self.direction = state.get("direction")
        self.legs = [dict(leg) for leg in state.get("legs", [])]
        self.entry_credit = float(state.get("entry_credit", 0.0))
        ee = state.get("entry_expiry")
        self.entry_expiry = date.fromisoformat(ee) if ee else None
        self.last_decision_date = state.get("last_decision_date")
