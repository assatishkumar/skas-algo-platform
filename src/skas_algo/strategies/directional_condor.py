"""directional_condor — the "Biased Condor": a ONE-SIDED long condor in the direction of the
daily SuperTrend (owner deck, Upsurge.club "NIFTY Biased Condor — Strategy Note", 2026-09-21).

The deck calls it an iron condor; the rules describe a LONG condor bought for a DEBIT, all
four legs on one side. Daily SuperTrend (11, 2.9) on NIFTY green → bullish CALL condor,
red → bearish PUT condor, Buy → Sell → Sell → Buy on the monthly expiry:

    bull, spot 24,299 (deck sample)         bear, spot 23,981 (deck sample)
    BUY  24350 CE   ← K1, first OTM          BUY  23900 PE   ← K1, first OTM
    SELL 24600 CE   ← K1 + w                 SELL 23600 PE   ← K1 − w
    SELL 24850 CE   ← K1 + 2w                SELL 23300 PE   ← K1 − 2w
    BUY  25100 CE   ← K1 + 3w                BUY  23000 PE   ← K1 − 3w
    max profit ₹11,661 · max loss ₹4,589     max profit ₹15,814 · max loss ₹3,685

Max loss = the debit (defined risk by construction); max profit = w − debit, on the plateau
between the two shorts; breakevens at K1 + debit (near) and K4 − debit (far), mirrored for
puts. Owner decisions (2026-09-21): K1 is the first OTM strike on the 100 grid and w is
``width_pct`` (1%) of spot rounded to the grid; entry at 09:30 the session after a CONFIRMED
flip (a fresh deploy inside a trend WAITS for the next flip unless forced); the profit lock and
the breakeven rules read a ₹ base that is the ``margin_per_set`` MANUAL ANCHOR (form/deploy
default ₹1,00,000 per lot-set — a long condor's real broker margin is ≈ the debit, so "2% of
margin" would be ~₹90 and fire on noise; the deck's ~10%/month on an ~₹11k max profit implies
~₹1L of capital per lot) or, with the anchor off, the broker margin frozen at entry.

Management, from the deck:
- PROFIT LOCK: once open profit > ``lock_start_pct`` of the base, and at every further
  ``lock_step_pct``, BOTH long wings roll one grid step toward their short (never within one
  step of it) — the closed wing's profit is banked, the structure narrows.
- SAME-SIDE BREAKOUT: spot past the FAR breakeven → exit and rebuild a fresh condor in the
  same direction from the new spot (the next slice, once the book is flat).
- IN PROFIT, THEN REVERSAL: spot had crossed the near breakeven (the structure was paying),
  the cycle showed ≥ ``lock_start_pct`` of the base, and spot comes back through the near
  breakeven → exit, keep what is showing, stay flat until the NEXT flip.
- STRAIGHT REVERSAL: the stop at −``half_loss_pct`` (50) of the entry max loss — the deck's
  "exit at half the visible payoff loss". Always armed (a stop is a stop); the breach rule
  above is simply the EARLIER exit on the profitable path.
- An OPPOSITE confirmed flip while holding → exit and reverse (rebuild the other side); the
  roll ``roll_days_before`` days before expiry re-enters the SAME side on the next month.

Every price-driven decision is cadence-sampled (``profit_check`` / ``stop_check``), held
before 09:20 (the opening-window rule), marked at exit prices against the real fills
(``mark_basis="exit"``), and deferred on a stale print. Subclasses the delta-neutral base for
its margin freeze, payoff utilities and MARKS logging (the put_condor / volcano precedent);
``phase="condor"`` keeps the base's adjustments inert. Replays on the 1-min store; deploys on
the generic path with a broker quote source (the daily bars come from the broker, cache fallback).
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

import pandas as pd

from skas_algo.engine.indicators.supertrend import _supertrend_bars
from skas_algo.engine.options.contract_specs import lot_size_for, selection_step
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction
from skas_algo.strategies.delta_neutral_monthly import DeltaNeutralMonthlyStrategy

from ._options_common import bad_close

log = logging.getLogger(__name__)

# ATR(11) needs ~11 bars to be defined and the band carry-over a few dozen more before the
# direction stops depending on where the series was cut. A shorter history must NOT latch
# the day: a thin cache reads a direction the backtest never saw (the supertrend_momentum
# VPS lesson, 2026-09-08).
_MIN_SETTLED_BARS_EXTRA = 30


class DirectionalCondorStrategy(DeltaNeutralMonthlyStrategy):
    """Daily-SuperTrend biased LONG condor (calls when green, puts when red) — see the module
    docstring for the rules."""

    strategy_id = "directional_condor"
    intraday = True

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 500_000,
        underlying: str | None = None,
        lots: int = 1,
        # ---- the signal ----
        st_period: int = 11,
        st_multiplier: float = 2.9,
        confirm_bars: int = 1,          # settled bars in the new direction AFTER the flip bar
        # ---- the structure ----
        width_pct: float = 1.0,         # segment width as % of spot, rounded to the 100 grid
        # ---- management (whole percents of the ₹ base — the manual anchor / entry margin) ----
        lock_start_pct: float = 2.0,
        lock_step_pct: float = 1.0,
        half_loss_pct: float = 50.0,    # % of the ENTRY max loss; 0 = off
        target_pct: float = 0.0,        # optional flat target, % of the base; 0 = off
        # ---- entry ----
        entry_time: str = "09:30",
        entry_window_end: str = "15:00",
        force_entry: bool = False,
        margin_per_set: float = 0.0,
        expiry_switch_day: int = 15,
        roll_days_before: int = 5,
        # ---- cadence / misc ----
        profit_check: str = "tick",
        stop_check: str = "tick",
        eod_time: str = "15:20",
        min_leg_oi: int = 1,
        mark_basis: str = "exit",
        risk_free_rate: float = 0.065,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        super().__init__(
            universe=universe,
            initial_capital=initial_capital,
            underlying=(underlying or (universe[0] if universe else "NIFTY")),
            lots=lots,
            margin_per_set=margin_per_set,
            entry_time=entry_time,
            entry_window_end=entry_window_end,
            force_entry=force_entry,
            ironfly_adjust=False,          # the base's fly adjustment never applies here
            profit_target_pct=target_pct,  # the base's exit_amounts reads target_pct
            stop_loss_pct=0.0,             # our stop is % of MAX LOSS, not of the base
            exit_margin_basis="entry",     # the base is the cycle's — a wing walk never re-bases
            pnl_basis="total",             # banked wing profits count toward the % rules
            risk_free_rate=risk_free_rate,
            profit_check=profit_check,
            stop_check=stop_check,
            eod_time=eod_time,
            min_leg_oi=min_leg_oi,
            mark_basis=mark_basis,
            lot_overrides=lot_overrides,
        )
        self.st_period = max(2, int(st_period))
        self.st_multiplier = float(st_multiplier)
        self.confirm_bars = max(0, int(confirm_bars))
        self.width_pct = float(width_pct)
        self.lock_start_pct = float(lock_start_pct)
        self.lock_step_pct = float(lock_step_pct)
        self.half_loss_pct = float(half_loss_pct)
        self.expiry_switch_day = int(expiry_switch_day)
        self.roll_days_before = int(roll_days_before)
        self._daily_bars_fn = None

        # ---- state (persisted) ----
        self.direction: str | None = None      # "bull" | "bear": the side held or armed for
        self.armed: bool = False               # flat + armed → build on the next entry slice
        self.last_dir: int | None = None       # ±1 on the last SETTLED daily bar
        self.pending_signal: dict | None = None   # {"dir", "seen"} — a flip awaiting confirmation
        self.last_st_date: str | None = None   # the session the SuperTrend was last read
        self.last_line: float | None = None
        self.entry_max_loss: float = 0.0       # ₹ — the debit × units, at entry
        self.entry_debit: float = 0.0          # per unit
        self.entry_spot: float | None = None
        self.be_near: float | None = None
        self.be_far: float | None = None
        self.peak_pnl: float = 0.0             # ₹ high-water of the cycle's P&L
        self.crossed_near: bool = False        # spot has been past the near breakeven
        self.lock_level: int = 0               # profit-lock steps fired this cycle
        self.lock_exhausted: bool = False      # the wings sit one step from their shorts
        self.rebuild_pending: dict | None = None   # {"reason"}: flat now, re-enter next slice
        self.min_expiry: date | None = None    # after a roll: the month just closed

    # ------------------------------------------------------------ live hooks
    def set_daily_bars_fn(self, fn) -> None:
        """``fn(underlying, start, end) -> DataFrame[date, high, low, close]`` — the same
        provider ema21 reads: broker-first daily bars live, the cache + a forming today-bar
        in the replay. Only SETTLED rows (date < today) feed the SuperTrend."""
        self._daily_bars_fn = fn

    def request_force_entry(self) -> str:
        self.force_pending = True
        return "next tick builds the condor in the current daily SuperTrend direction"

    # ------------------------------------------------------------ the signal
    def _st_eval(self, today: date) -> str | None:
        """Read the daily SuperTrend ONCE per session off settled bars. Returns a confirmed
        signal ("bull"/"bear") the moment the pending flip has ``confirm_bars`` closed bars
        behind it, else None. Latches ``last_st_date`` only after a good compute, so a data
        hiccup retries on the next slice instead of burning the day."""
        if self.last_st_date == today.isoformat() or self._daily_bars_fn is None:
            return None
        try:
            df = self._daily_bars_fn(self.underlying, today - timedelta(days=400), today)
        except Exception:  # pragma: no cover - a provider blip must not stop the run
            log.exception("directional_condor: daily bars failed for %s", today)
            return None
        if df is None or len(df) == 0:
            return None
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[df["date"] < today].sort_values("date").reset_index(drop=True)
        if len(df) < self.st_period + _MIN_SETTLED_BARS_EXTRA:
            return None
        st = _supertrend_bars(df[["high", "low", "close"]].astype(float),
                              self.st_period, self.st_multiplier)
        d, line = st["direction"].iloc[-1], st["supertrend"].iloc[-1]
        if pd.isna(d):
            return None
        d = int(d)
        self.last_st_date = today.isoformat()
        self.last_line = float(line) if not pd.isna(line) else None

        # flip → confirmation window → actionable signal (fires once) — supertrend_spread's
        # machine, fed one settled bar a day.
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
        return signal

    @property
    def _st_side(self) -> str | None:
        return None if self.last_dir is None else ("bull" if self.last_dir > 0 else "bear")

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()
        live = self._live_legs(ctx)

        # Defence in depth against an uncloseable book (put_condor's rule): an expired
        # contract has no marks, so _manage would defer forever and never reach an exit.
        if live and self.cycle_expiry and today > date.fromisoformat(self.cycle_expiry):
            return self._close_cycle(live, "dc_expired_stale", armed=self.direction is not None,
                                     rebuild="expired")

        signal = self._st_eval(today) if now.time() >= self.entry_time else None
        if signal is not None:
            if live and signal != self.direction:
                # the trend turned against the book: out, and rebuild the other way
                sigs = self._close_cycle(live, "dc_reverse", armed=True, rebuild="reverse")
                self.direction = signal
                return sigs
            if not live:
                self.direction, self.armed = signal, True
                self.rebuild_pending = None
        if (not live and self.direction is None and self.last_dir is not None
                and (self.force_entry or self.force_pending)):
            # forced: take the CURRENT side instead of waiting for the next flip (the
            # ctor flag still respects the entry window; the tile button enters at once)
            self.direction, self.armed = self._st_side, True

        if live:
            return self._manage(ctx, live, now)

        if self.phase != "idle":            # a cycle ended in the engine (settlement)
            self.phase = "idle"
            self.cycle_expiry = None
            self._reset_cycle()

        if self.force_pending:
            if self.direction is None:
                if self.last_dir is None:
                    return self._skip("force: waiting for the first daily SuperTrend read", today)
                self.direction, self.armed = self._st_side, True
            got = self._try_enter(ctx, now, today)
            if got:
                self.force_pending = False
            return got
        if self.rebuild_pending is not None and self.armed and self.direction:
            if not self._open_settled(now):
                return []
            return self._try_enter(ctx, now, today)
        if not (self.armed and self.direction):
            if self.last_dir is not None and now.time() >= self.entry_time \
                    and self.entered_day != today.isoformat():
                self.entered_day = today.isoformat()
                side = self._st_side
                what = ("waiting for a confirmed daily SuperTrend flip "
                        f"(currently {side}, {'held' if self.direction else 'no entry yet'})")
                return self._skip(what, today)
            return []
        if self.entered_day == today.isoformat():
            return []
        if not (self.entry_time <= now.time() <= self.entry_window_end):
            return []
        return self._try_enter(ctx, now, today)

    # ----------------------------------------------------------------- entry
    def _target_expiry(self, ctx, today: date) -> date | None:
        """Monthly per the before/after-``expiry_switch_day`` rule: the LAST listed expiry
        of the target month; inside the roll window → the following month; after a roll,
        strictly past the month just closed (supertrend_spread's rule)."""
        listed = self._listed_expiries(ctx, today)
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

    def _try_enter(self, ctx, now: datetime, today: date) -> list[Signal]:
        if self.direction not in ("bull", "bear"):
            return self._skip("no direction yet", today)
        # a refused leg retries every slice inside the window (put_condor's rule) —
        # ``entered_day`` latches only once the legs land
        expiry = self._target_expiry(ctx, today)
        if expiry is None:
            return self._skip("no usable monthly expiry", today)
        rows = self._chain_rows(ctx, expiry.isoformat())
        if not rows:
            return self._skip(f"no chain for {expiry.isoformat()}", today)
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(self.underlying, expiry.isoformat()) if chain_fn else None
        spot = (chain or {}).get("spot")
        if spot is None or bad_close(spot):
            return self._skip("no spot", today)
        spot = float(spot)
        step = selection_step(self.underlying, 100)
        bull = self.direction == "bull"
        sign = 1 if bull else -1
        right = "CE" if bull else "PE"
        # K1 = the first OTM strike on the grid; a spot exactly on a strike is not OTM
        k1 = (math.ceil(spot / step) if bull else math.floor(spot / step)) * step
        if k1 == spot:
            k1 += sign * step
        w = max(step, round(spot * self.width_pct / 100.0 / step) * step)
        strikes = [float(k1 + sign * i * w) for i in range(4)]
        dirs = [1, -1, -1, 1]
        try:
            per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return self._skip("no lot size for the expiry", today)
        units = float(self.lots * per_lot)
        legs, prems = [], []
        for k, direction in zip(strikes, dirs, strict=True):
            cell = (rows.get(k) or {}).get(right.lower())
            prem = self._ltp(cell)
            if prem is None or not self._oi_ok(cell):
                return self._skip(f"{right} {k:.0f} unpriced or thin — condor needs all four legs",
                                  today)          # all-or-nothing: never half-enter a condor
            prems.append(prem)
            legs.append(self._leg(expiry, k, right, direction, units, prem, per_lot))
        debit = prems[0] + prems[3] - prems[1] - prems[2]
        if debit <= 0:
            return self._skip("the chain prices a credit, not a long condor", today)

        self.legs = legs
        self.phase = "condor"
        self.cycle_expiry = expiry.isoformat()
        self.entered_day = today.isoformat()
        self.adjust_count = 0
        self.last_adjust_at = None
        self.adjust_realized = 0.0
        self.entry_debit = float(debit)
        self.entry_spot = spot
        self.entry_max_loss = max(0.0, -self._payoff_min(legs, spot))
        self.be_near, self.be_far = self._breakevens(legs, spot)
        self.peak_pnl = 0.0
        self.crossed_near = False
        self.lock_level = 0
        self.lock_exhausted = False
        self.rebuild_pending = None
        self.min_expiry = None
        self.armed = False
        self._entered()
        self._freeze_margin(ctx, spot)
        reason = f"dc_entry_{self.direction}"
        # LONGS FIRST: signal order is honoured, so a partial fill leaves the book
        # over-hedged rather than short a naked option.
        return [
            Signal(leg["symbol"],
                   SignalAction.ENTER_LONG if leg["dir"] > 0 else SignalAction.ENTER_SHORT,
                   quantity=int(leg["units"]), reason=reason, meta={"multiplier": 1})
            for leg in sorted(legs, key=lambda x: -x["dir"])
        ]

    # ------------------------------------------------------------- structure
    def _leg(self, expiry: date, k: float, right: str, direction: int, units: float,
             entry: float, per_lot: int) -> dict:
        sym = make(self.underlying, expiry, float(k), right,
                   lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
        return {"symbol": sym, "right": right, "dir": int(direction),
                "units": float(units), "entry": float(entry)}

    def _k(self, leg: dict) -> float:
        return float(leg["symbol"].split("|")[2])

    def _ordered(self) -> list[dict]:
        """K1 → K4 in the trend's direction (calls ascending, puts descending)."""
        return sorted(self.legs, key=self._k, reverse=(self.direction == "bear"))

    def _held_strikes(self, right: str) -> set[float]:
        return {self._k(x) for x in self.legs if x["right"] == right}

    def _breakevens(self, legs: list[dict], spot: float) -> tuple[float | None, float | None]:
        """Roots of the expiry payoff (banked wing profits included), split into the NEAR
        one (the trend side of the entry — the structure starts paying past it) and the FAR
        one (past which the structure gives its profit back). None when the payoff never
        crosses zero on that side — that rule is then disarmed rather than misread. Read at
        ENTRY for the cycle's decision levels; after a wing walk only for the tile."""
        grid = self._payoff_grid(legs, spot)
        roots: list[float] = []
        prev_s, prev_v = None, None
        for s in grid:
            v = self._payoff_at(legs, s)
            if prev_s is not None and (prev_v < 0 <= v or v < 0 <= prev_v):
                roots.append(prev_s + (s - prev_s) * (-prev_v) / (v - prev_v))
            prev_s, prev_v = s, v
        if not roots:
            return None, None
        if self.direction == "bull":
            return min(roots), max(roots)
        return max(roots), min(roots)

    def _beyond(self, spot: float, level: float | None) -> bool:
        """Spot is past ``level`` in the TREND's direction."""
        if level is None:
            return False
        return spot > level if self.direction == "bull" else spot < level

    def max_loss(self, spot: float) -> float:
        if not self.legs:
            return 0.0
        return max(0.0, -self._payoff_min(self.legs, spot))

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, live: list[dict], now: datetime) -> list[Signal]:
        today: date = ctx.today()
        self._adopt_fills(ctx)
        has_print = getattr(ctx.market, "has_print", None)
        marks: dict[str, float] = {}
        pnl_ltp = pnl_exit = 0.0
        exit_marks: dict[str, float] = {}
        for leg in live:
            if has_print is not None and not has_print(leg["symbol"]):
                return []                      # a stale mark would make the judgement dishonest
            try:
                cur = ctx.close(leg["symbol"])
            except KeyError:
                return []
            marks[leg["symbol"]] = cur
            xm = self._exit_mark(ctx, leg, cur)
            exit_marks[leg["symbol"]] = xm
            pnl_ltp += (cur - leg["entry"]) * leg["units"] * leg["dir"]
            pnl_exit += (xm - leg.get("entry_fill", leg["entry"])) * leg["units"] * leg["dir"]
        pnl_ltp += self.adjust_realized
        pnl_exit += self.adjust_realized
        self._exit_marks = exit_marks
        self._pnl_pair = (pnl_ltp, pnl_exit)
        pnl = pnl_exit if self.mark_basis == "exit" else pnl_ltp
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        if spot is None or bad_close(spot):
            return []
        spot = float(spot)
        self._apply_margin_push()
        if self.margin_source not in ("broker", "manual") or self.margin_base <= 0:
            return []                          # the base is what every rule below reads

        # _due CONSUMES its window: sample each kind ONCE, after every guard above.
        due_profit = self._due("profit", now)
        due_stop = self._due("stop", now)
        if pnl_ltp != pnl_exit:
            self._log_marks_divergence(now, pnl_ltp, pnl_exit)

        base = self.margin_base
        self.peak_pnl = max(self.peak_pnl, pnl)
        if self._beyond(spot, self.be_near):
            self.crossed_near = True
        pnl_pct = 100.0 * pnl / base
        lock_amt = base * self.lock_start_pct / 100.0

        # (a) the roll backstop — never into expiry week; the same side re-enters next month
        if due_stop and self.cycle_expiry:
            expiry = date.fromisoformat(self.cycle_expiry)
            if (expiry - today).days <= self.roll_days_before:
                sigs = self._close_cycle(live, "dc_roll", armed=True, rebuild="roll")
                self.min_expiry = expiry
                return sigs

        # (b) the breakeven rules
        if due_profit and self._beyond(spot, self.be_far):
            # same-side breakout: the structure is giving its profit back past the far
            # breakeven → out, and a fresh condor from the new spot (next slice, flat book)
            return self._close_cycle(live, "dc_breakout", armed=True, rebuild="breakout")
        if due_stop and self.crossed_near and self.be_near is not None \
                and not self._beyond(spot, self.be_near) and self.peak_pnl >= lock_amt:
            # it paid, then reversed back through the near breakeven: keep what is showing
            return self._close_cycle(live, "dc_lock_exit", armed=False)
        if due_profit and self.target_pct > 0 and pnl >= base * self.target_pct / 100.0:
            return self._close_cycle(live, "dc_target", armed=False)

        # (c) the stop — half the entry max loss
        if due_stop and self.half_loss_pct > 0 and self.entry_max_loss > 0 \
                and pnl <= -self.entry_max_loss * self.half_loss_pct / 100.0:
            return self._close_cycle(live, "dc_half_loss", armed=False)

        # (d) the profit lock: walk both wings one step in, once per level
        if due_profit and pnl_pct >= self.lock_start_pct and not self.lock_exhausted \
                and self.lock_step_pct > 0:
            want = int(math.floor((pnl_pct - self.lock_start_pct) / self.lock_step_pct)) + 1
            if want > self.lock_level:
                got = self._walk_wings(ctx, marks, spot, now)
                if got:
                    self.lock_level = want
                    return got
        return []

    def _walk_wings(self, ctx, marks: dict[str, float], spot: float, now: datetime) -> list[Signal]:
        """Roll BOTH long wings one grid step toward their short, in ONE slice: the two closes
        first, then the two opens (EXIT_ALL resolves against the pre-action book, and a
        destination the book already holds would merge — the run-#203 shape — so it refuses).
        A wing already one step from its short cannot move; the lock is then exhausted."""
        ordered = self._ordered()
        if len(ordered) != 4 or self.direction not in ("bull", "bear"):
            return []
        k1, k2, k3, k4 = (self._k(x) for x in ordered)
        long_near, short_near, short_far, long_far = ordered
        step = float(selection_step(self.underlying, 100))
        sign = 1.0 if self.direction == "bull" else -1.0
        new_near = k1 + sign * step
        new_far = k4 - sign * step
        if (sign * (k2 - new_near) < step - 1e-9) or (sign * (new_far - k3) < step - 1e-9):
            self.lock_exhausted = True
            return []
        right = long_near["right"]
        if new_near in self._held_strikes(right) or new_far in self._held_strikes(right):
            self.lock_exhausted = True
            return []
        rows = self._chain_rows(ctx, self.cycle_expiry) or {}
        fresh: list[dict] = []
        for old, new_k in ((long_near, new_near), (long_far, new_far)):
            cell = (rows.get(float(new_k)) or {}).get(right.lower())
            prem = self._ltp(cell)
            if prem is None or not self._oi_ok(cell) or marks.get(old["symbol"]) is None:
                return []                      # all-or-nothing; retry on the next sample
            per_lot = int(old["units"] // self.lots) or 1
            fresh.append(self._leg(date.fromisoformat(self.cycle_expiry), new_k, right,
                                   old["dir"], old["units"], prem, per_lot))
        for old in (long_near, long_far):
            self.adjust_realized += ((marks[old["symbol"]] - old["entry"])
                                     * old["units"] * old["dir"])
        gone = {long_near["symbol"], long_far["symbol"]}
        keep = [x for x in self.legs if x["symbol"] not in gone]
        self.legs = keep + fresh
        self.last_adjust_at = now.isoformat()
        self.adjust_count += 1
        # The decision levels stay the ENTRY's breakevens for the whole cycle. The walked
        # wing costs more than the one it replaces, so the CURRENT payoff's near breakeven
        # moves AWAY from spot — recomputing it here made the breach rule fire on the very
        # next sample with spot unmoved (caught by test). The walk reshapes the payoff the
        # tile draws (`basket_status.be_now_*`), never what the rules read.
        return (
            [Signal(x["symbol"], SignalAction.EXIT_ALL, reason="dc_lock")
             for x in (long_near, long_far)]
            + [Signal(x["symbol"], SignalAction.ENTER_LONG, quantity=int(x["units"]),
                      reason="dc_lock", meta={"multiplier": 1}) for x in fresh]
        )

    # ------------------------------------------------------------------ exit
    def _close_cycle(self, live: list[dict], reason: str, *, armed: bool,
                     rebuild: str | None = None) -> list[Signal]:
        """The base's ``_exit_all`` (MARKS log, base cleared for the next cycle) plus this
        strategy's cycle state. ``armed`` says whether the standing direction may re-enter;
        ``rebuild`` names why the next slice should build again without waiting for 09:30."""
        sigs = self._exit_all(live, reason)
        self._reset_cycle()
        self.armed = bool(armed)
        self.rebuild_pending = {"reason": rebuild} if (rebuild and armed) else None
        return sigs

    def _reset_cycle(self) -> None:
        self.entry_max_loss = 0.0
        self.entry_debit = 0.0
        self.entry_spot = None
        self.be_near = self.be_far = None
        self.peak_pnl = 0.0
        self.crossed_near = False
        self.lock_level = 0
        self.lock_exhausted = False

    # ------------------------------------------------------------- monitoring
    def exit_amounts(self) -> tuple[float | None, float | None]:
        if self.margin_source not in ("broker", "manual") or self.margin_base <= 0:
            return None, None
        tgt = self.margin_base * self.target_pct / 100.0 if self.target_pct > 0 else None
        stp = (self.entry_max_loss * self.half_loss_pct / 100.0
               if self.half_loss_pct > 0 and self.entry_max_loss > 0 else None)
        return tgt, stp

    def exit_rules(self) -> list[str]:
        side = {"bull": "CE", "bear": "PE"}.get(self.direction or "", "CE/PE")
        mlabel = self._margin_label()
        rules = [
            f"Long {side} condor on the daily SuperTrend({self.st_period}, "
            f"{self.st_multiplier:g}): K1 = the first OTM strike, width {self.width_pct:g}% "
            "of spot on the 100 grid — "
            "a net debit, max loss is the debit paid",
            f"Same-side breakout past the far breakeven → exit and rebuild in the trend "
            f"({self._cadence_phrase('profit')})",
            f"Back through the near breakeven after the cycle showed +{self.lock_start_pct:g}% "
            f"of {mlabel} → exit, flat until the next flip ({self._cadence_phrase('stop')})",
        ]
        if self.half_loss_pct > 0:
            rules.append(f"Stop at −{self.half_loss_pct:g}% of the entry max loss "
                         f"({self._cadence_phrase('stop')})")
        if self.target_pct > 0:
            rules.append(f"Book at +{self.target_pct:g}% of {mlabel} "
                         f"({self._cadence_phrase('profit')})")
        rules.append(f"Profit lock: from +{self.lock_start_pct:g}% and every further "
                     f"+{self.lock_step_pct:g}% of {mlabel}, both long wings roll one strike "
                     "toward their short (never within one step of it)")
        rules.append(f"Opposite confirmed flip ({self.confirm_bars} closed bar"
                     f"{'s' if self.confirm_bars != 1 else ''}) → exit and reverse; "
                     f"roll {self.roll_days_before} days before expiry, same side, next month")
        return rules

    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        spot = getattr(market, "index_spot", lambda _u: None)(self.underlying)
        out: dict = {
            "kind": "directional_condor", "phase": self.phase,
            "underlying": self.underlying, "spot": spot,
            "direction": self.direction, "armed": self.armed,
            "st_dir": self.last_dir, "line": self.last_line, "last_st_date": self.last_st_date,
            "pending_signal": dict(self.pending_signal) if self.pending_signal else None,
            "legs": [dict(x) for x in self.legs],
            "cycle_expiry": self.cycle_expiry,
            "entry_debit": round(self.entry_debit, 2),
            "entry_max_loss": round(self.entry_max_loss, 2),
            "entry_spot": self.entry_spot,
            "be_near": self.be_near, "be_far": self.be_far,
            "lock_level": self.lock_level, "lock_exhausted": self.lock_exhausted,
            "peak_pnl": round(self.peak_pnl, 2), "crossed_near": self.crossed_near,
            "rebuild_pending": dict(self.rebuild_pending) if self.rebuild_pending else None,
            "adjust_realized": round(self.adjust_realized, 2),
            "margin_base": self.margin_base, "margin_source": self.margin_source,
            "mark_basis": self.mark_basis,
            "entry_shortfall": round(self.entry_shortfall, 2),
            "pnl_ltp": round(self._pnl_pair[0], 2) if self._pnl_pair else None,
            "pnl_exit": round(self._pnl_pair[1], 2) if self._pnl_pair else None,
        }
        try:
            if self.legs and spot:
                out["be_now_near"], out["be_now_far"] = self._breakevens(self.legs, float(spot))
                out["max_loss"] = round(self.max_loss(float(spot)), 2)
                out["max_profit"] = round(self._payoff_max(self.legs, float(spot)), 2)
                out["payoff"] = [
                    {"spot": round(s, 2), "expiry_pnl": round(self._payoff_at(self.legs, s), 2)}
                    for s in self._payoff_grid(self.legs, float(spot))
                ]
        except Exception:  # pragma: no cover - monitoring never breaks a snapshot
            pass
        return out

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        st = super().export_state()
        st.update({
            "direction": self.direction, "armed": self.armed,
            "last_dir": self.last_dir,
            "pending_signal": dict(self.pending_signal) if self.pending_signal else None,
            "last_st_date": self.last_st_date, "last_line": self.last_line,
            "entry_max_loss": self.entry_max_loss, "entry_debit": self.entry_debit,
            "entry_spot": self.entry_spot,
            "be_near": self.be_near, "be_far": self.be_far,
            "peak_pnl": self.peak_pnl, "crossed_near": self.crossed_near,
            "lock_level": self.lock_level, "lock_exhausted": self.lock_exhausted,
            "rebuild_pending": dict(self.rebuild_pending) if self.rebuild_pending else None,
            "min_expiry": self.min_expiry.isoformat() if self.min_expiry else None,
        })
        return st

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        self.direction = state.get("direction")
        self.armed = bool(state.get("armed", False))
        ld = state.get("last_dir")
        self.last_dir = int(ld) if ld is not None else None
        ps = state.get("pending_signal")
        self.pending_signal = dict(ps) if ps else None
        self.last_st_date = state.get("last_st_date")
        self.last_line = state.get("last_line")
        self.entry_max_loss = float(state.get("entry_max_loss", 0.0) or 0.0)
        self.entry_debit = float(state.get("entry_debit", 0.0) or 0.0)
        self.entry_spot = state.get("entry_spot")
        self.be_near = state.get("be_near")
        self.be_far = state.get("be_far")
        self.peak_pnl = float(state.get("peak_pnl", 0.0) or 0.0)
        self.crossed_near = bool(state.get("crossed_near", False))
        self.lock_level = int(state.get("lock_level", 0) or 0)
        self.lock_exhausted = bool(state.get("lock_exhausted", False))
        rp = state.get("rebuild_pending")
        self.rebuild_pending = dict(rp) if rp else None
        me = state.get("min_expiry")
        self.min_expiry = date.fromisoformat(me) if me else None
