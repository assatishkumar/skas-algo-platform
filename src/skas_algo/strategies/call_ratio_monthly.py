"""Call Ratio Monthly — a 1:2 call ratio spread with an outer hedge on NIFTY monthly.

Structure (all CE, next month's monthly expiry):
  * BUY  1× at ~spot+buy_offset   (long, near)
  * SELL 2× at ~spot+sell_offset  (short body)
  * BUY  1× at ~spot+hedge_offset (long, far hedge — caps upside loss)
  * BUY  tail_hedge_lots× at ~spot+tail_hedge_offset (OPTIONAL far 'disaster' hedge —
    its long vega/gamma cushions gap moves the MTM stop can't catch; beyond it the
    wing turns net long, so a violent enough move becomes profit. Cost counts against
    the entry credit; min_credit_pct < 0 allows paying a small debit for it.)

Net is balanced (long 2 / short 2 contracts) → **zero downside risk** (all calls; if NIFTY
falls they expire worthless and you keep/pay the small net credit/debit), risk is upside-only
and capped by the hedge. Entered on the last Tuesday of each month for the next month's
contract (EOD in backtest — the 3:16 PM intraday rule can't be honored on EOD bhavcopy),
held with a fixed profit-target / stop-loss / max-holding exit and **zero adjustments**.

Long legs are ``ENTER_LONG`` (buy-to-open), the body is one ``ENTER_SHORT`` for 2 lots; exits
are ``EXIT_ALL`` per leg (the resolver sells longs / buys-to-close the short). Anything left at
expiry is settled to intrinsic by the engine's ExpirySettler.
"""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, time, timedelta

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import expected_monthly_expiry, lot_size_for
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close as _bad
from ._options_common import next_monthly_expiry


def _last_weekday_of_month(d: date, weekday: int) -> date:
    """Date of the last ``weekday`` (Mon=0 … Sun=6) in d's calendar month."""
    last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


class CallRatioMonthlyStrategy:
    strategy_id = "call_ratio_monthly"
    right = "CE"  # PutRatioMonthlyStrategy flips this to "PE" (the downside mirror)
    entry_reason = "call_ratio"

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 100_000,
        underlying: str | None = None,
        strike_mode: str = "points",   # "points" | "percent" (%OTM) | "delta" (|Δ|) | "sd" (×expected move)
        buy_offset: float = 300,
        sell_offset: float = 600,
        hedge_offset: float = 1600,
        lots: int = 1,
        buy_lots: int = 1,    # leg ratio multiples (×lots): 1:2:1 = the classic ratio;
        sell_lots: int = 2,   # HNI weekly uses 1:3:2 — net contracts must stay balanced
        hedge_lots: int = 1,  # (buy+hedge ≥ sell) or the wing carries a naked tail
        # Lot sizing. "fixed" (legacy): trade exactly ``lots`` lot-sets — §1 backstop, keeps
        # running deploys byte-identical. "margin": ``lots`` is only a fallback — at each
        # entry lots = ⌊current equity × utilization ÷ model margin per lot-set⌋, where the
        # divisor is the ERA-TRUE model formula (span+exposure)% × that day's spot × the
        # short-body units for that expiry's lot size. Same deterministic math in backtest
        # and live (ctx.position_margin() is model-in-BT vs broker-live ≈2× apart — never
        # used for sizing). NOTE the model charges shorts only (no long-hedge offset), so it
        # reads ≈2× the real broker SPAN: utilization 95 ≈ ~50% of broker margin — raise it
        # (sweepable) once live margins are observed.
        sizing: str = "fixed",                    # "fixed" | "margin" (auto-size to capital)
        capital_utilization_pct: float = 95.0,    # % of equity deployed as MODEL margin
        max_auto_lots: int = 0,                   # safety cap on auto lots (0 = uncapped)
        credit_debit_limit_pct: float = 0.01,   # max net CREDIT = this × capital (credit required)
        shift_step: float = 100,                 # strike-adjust step (searched ± both directions)
        max_shifts: int = 10,                    # search up to ±max_shifts × shift_step
        profit_target_pct: float = 0.025,        # exit at +2.5% of capital
        stop_loss_pct: float = 0.03,             # exit at −3% of capital
        max_holding_days: int = 20,              # hard time exit (avoid end-of-month gamma)
        min_vix: float = 0.0,                     # skip entry if ATM IV% (≈ India VIX) below this
        min_dte: int = 18,                        # selects the *next* month's monthly expiry
        entry_weekday: int = 1,                   # Tuesday
        # Entry anchor: "last_weekday" (legacy — first slice on/after the month's last
        # entry_weekday, i.e. ON/just before expiry) | "post_expiry" (first trading day
        # AFTER the calendar-expected monthly expiry — cycle-anchored like donchian).
        # Defaults to the legacy rule so existing runs/deploys are byte-identical (§1).
        entry_rule: str = "last_weekday",
        # post_expiry only: how many calendar days after the expiry the entry may retry
        # (credit-gate failures). Mirrors the legacy window (last weekday → month end,
        # ~5 sessions) — unbounded retry would silently enter debit months mid-cycle.
        entry_window_days: int = 7,
        strike_step: float = 50,                  # informational; strikes are snapped to listings
        risk_free_rate: float = 0.065,
        tail_hedge_offset: float = 0.0,  # 0=off; extra far long per wing (same units as offsets)
        tail_hedge_lots: float = 1.0,    # tail size as a fraction of ``lots`` (whole-lot rounded)
        tail_hedge_side: str = "both",   # "both" | "call" | "put" — which wings carry the tail
        min_credit_pct: float = 0.0,     # credit floor ×capital (negative = allow a small debit)
        # --- live intraday exit cadence (backtest is EOD → every cadence collapses to the
        #     daily bar, so these change nothing in backtest). Each ∈ tick/1/5/15/30/60min/eod.
        entry_time: str | None = None,   # only enter at/after this IST time (None = any time)
        profit_check: str = "eod",       # how often to evaluate the profit target
        stop_check: str = "eod",         # how often to evaluate the stop loss
        time_check: str = "eod",         # how often to evaluate the time exit
        eod_time: str = "15:15",         # what "eod" means (at/after this IST time)
        margin_per_lotset: float = 130_000.0,  # ~SPAN+exposure margin per ratio lot-set
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.initial_capital = float(initial_capital)
        self.strike_mode = strike_mode
        self.buy_offset = float(buy_offset)
        self.sell_offset = float(sell_offset)
        self.hedge_offset = float(hedge_offset)
        self.lots = int(lots)
        self.buy_lots = int(buy_lots)
        self.sell_lots = int(sell_lots)
        self.hedge_lots = int(hedge_lots)
        self.credit_debit_limit_pct = float(credit_debit_limit_pct)
        self.shift_step = float(shift_step)
        self.max_shifts = int(max_shifts)
        self.profit_target_pct = float(profit_target_pct)
        self.stop_loss_pct = float(stop_loss_pct)
        self.max_holding_days = int(max_holding_days)
        self.min_vix = float(min_vix)
        self.min_dte = int(min_dte)
        self.entry_weekday = int(entry_weekday)
        self.entry_rule = entry_rule
        self.entry_window_days = int(entry_window_days)
        self.sizing = sizing
        self.capital_utilization_pct = float(capital_utilization_pct)
        self.max_auto_lots = int(max_auto_lots)
        # The rupee base the CURRENT entry's credit gates use — stashed by _maybe_enter so
        # _build_side / Batman's combined cap scale with the same equity that sized the lots
        # (fixed mode: always initial_capital → gates byte-identical to legacy).
        self._entry_capital_base = float(initial_capital)
        self._entered_after_expiry: date | None = None  # post_expiry cycle lock
        self.strike_step = float(strike_step)
        self.r = float(risk_free_rate)
        self.tail_hedge_offset = float(tail_hedge_offset)
        self.tail_hedge_lots = float(tail_hedge_lots)
        self.tail_hedge_side = str(tail_hedge_side).lower()
        self.min_credit_pct = float(min_credit_pct)
        self.entry_time = entry_time
        self.profit_check = str(profit_check)
        self.stop_check = str(stop_check)
        self.time_check = str(time_check)
        self.eod_time = str(eod_time)
        self.margin_per_lotset = float(margin_per_lotset)
        self.lot_overrides = lot_overrides
        # Per-exit last-evaluation timestamps (for interval cadences); transient.
        self._last_check: dict[str, "datetime"] = {}
        # +1 for calls (OTM = above spot), −1 for puts (OTM = below spot).
        self._sign = 1 if self.right == "CE" else -1

        # State (persisted for live recovery). Each leg: {symbol, dir, units, entry}.
        self.legs: list[dict] = []
        self.entry_expiry: date | None = None
        self.entry_date: date | None = None
        self.last_entry_month: tuple[int, int] | None = None

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx) -> list[Signal]:
        chain = ctx.option_chain()
        if chain is None:
            return []  # not an options run
        if self.legs:
            return self._manage(ctx)
        return self._maybe_enter(ctx, chain, ctx.today())

    # ------------------------------------------------- subclass seams (timing/risk)
    # The weekly variants (HNI) override these four; the monthly base behaviour is
    # unchanged: month-locked entry from the last entry_weekday, monthly expiry,
    # capital-based target/stop, max-holding-days time exit.
    def _entry_allowed(self, today: date) -> bool:
        if self.entry_rule == "post_expiry":
            # One entry per expiry CYCLE: the first slice strictly after the month's
            # (calendar-expected) monthly expiry. Falls back to the legacy rule when the
            # calendar can't resolve (e.g. an unseeded underlying) so a run never sits idle.
            anchor = self._last_completed_expiry(today)
            if anchor is not None:
                # New cycle AND still inside the entry window. The window cap matters:
                # without it a credit-gate miss keeps retrying ALL cycle — entering a
                # skipped (debit) month weeks late, sometimes onto the FOLLOWING expiry.
                return (anchor != self._entered_after_expiry
                        and (today - anchor).days <= self.entry_window_days)
        if self.last_entry_month == (today.year, today.month):
            return False  # already traded this month (one entry / month, zero adjustments)
        return today >= _last_weekday_of_month(today, self.entry_weekday)

    def _last_completed_expiry(self, today: date) -> date | None:
        # Latest calendar-expected monthly expiry STRICTLY before today — on the expiry
        # day itself the completing expiry does not count (entry is the day AFTER).
        anchors = []
        y, m = today.year, today.month
        for _ in range(2):  # this month + previous cover every day-after case
            a = expected_monthly_expiry(self.underlying, y, m)
            if a is not None and a < today:
                anchors.append(a)
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        return max(anchors) if anchors else None

    def _mark_entered(self, today: date) -> None:
        self.last_entry_month = (today.year, today.month)
        if self.entry_rule == "post_expiry":
            self._entered_after_expiry = self._last_completed_expiry(today)

    def _select_expiry(self, chain, today: date) -> date | None:
        return self._next_monthly_expiry(chain, today)

    def _capital_base(self, ctx=None) -> float:
        """Rupee capital the entry sizes/gates against: CURRENT equity when auto-sizing
        (compounds — at a flat monthly entry cash == equity; guarded so stub ctxs without
        an equity accessor fall back), else the fixed initial capital (legacy)."""
        if self.sizing != "margin" or ctx is None:
            return self.initial_capital
        eq = getattr(ctx, "equity", None)
        try:
            val = eq() if callable(eq) else None
        except Exception:  # pragma: no cover - defensive: sizing must never kill a slice
            val = None
        return float(val) if val and val > 0 else self.initial_capital

    def _risk_base(self, ctx=None) -> float:
        """Rupee base the profit-target/stop percentages apply to: the deployed margin (real broker
        margin live, model estimate in backtest) when known, else the account capital."""
        fn = getattr(ctx, "position_margin", None) if ctx is not None else None
        m = fn() if fn is not None else None
        return m if m and m > 0 else self.initial_capital

    def _time_exit(self, today: date) -> bool:
        return bool(self.entry_date) and (today - self.entry_date).days >= self.max_holding_days

    # ------------------------------------------------- intraday exit cadence
    # In backtest there's one slice/day at the EOD bar, so every cadence is "due" once a
    # day → behaviour is unchanged. In live (intraday ticks) these gate how often each
    # exit type is actually evaluated (e.g. profit every 15 min, stop only at 15:15).
    _INTERVAL_MIN = {"tick": 0, "1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60}

    def _now(self, ctx) -> datetime:
        fn = getattr(ctx, "now", None)
        if fn is not None:
            return fn()
        return datetime.combine(ctx.today(), time(15, 30))  # stub ctx → treat as EOD

    def _eod_reached(self, now: datetime) -> bool:
        try:
            return now.time() >= time.fromisoformat(self.eod_time)
        except (ValueError, TypeError):
            return True

    def _due(self, kind: str, now: datetime) -> bool:
        """Is the ``kind`` exit ("profit"/"stop"/"time") due to be evaluated at ``now``?"""
        cadence = getattr(self, f"{kind}_check", "eod")
        if cadence == "eod":
            return self._eod_reached(now)
        mins = self._INTERVAL_MIN.get(cadence, 0)
        last = self._last_check.get(kind)
        if last is None or (now - last).total_seconds() >= mins * 60:
            self._last_check[kind] = now
            return True
        return False

    def _entry_time_ok(self, now: datetime) -> bool:
        if not self.entry_time:
            return True
        try:
            return now.time() >= time.fromisoformat(self.entry_time)
        except (ValueError, TypeError):
            return True

    # ------------------------------------------------------------------ helpers
    def _next_monthly_expiry(self, chain, today: date) -> date | None:
        """Nearest max-OI monthly ≥ min_dte out (shared helper; see _options_common)."""
        return next_monthly_expiry(chain, self.underlying, today, self.min_dte, self.right)

    @staticmethod
    def _snap(strikes: list[float], target: float) -> float | None:
        return min(strikes, key=lambda k: abs(k - target)) if strikes else None

    def _wing_has_tail(self, right: str) -> bool:
        """Does this wing carry the extra far 'disaster' hedge? (tail_hedge_side gates
        which wings — the put wing is where crash/vega convexity pays.)"""
        if self.tail_hedge_offset <= 0:
            return False
        return self.tail_hedge_side == "both" or (
            self.tail_hedge_side == ("call" if right == "CE" else "put"))

    def _tail_units(self, units: int) -> int:
        """Tail leg units: tail_hedge_lots × lots, rounded half-up to whole lots."""
        if self.tail_hedge_offset <= 0:
            return 0
        lot = units // self.lots
        return int(math.floor(self.lots * self.tail_hedge_lots + 0.5)) * lot

    def _delta_strike(self, rows: dict, spot: float, t: float, target_delta: float,
                      right: str | None = None) -> float | None:
        """Listed strike (of ``right``) whose |BS delta| is nearest ``target_delta``."""
        right = right or self.right
        best, best_err = None, 1e9
        for k, row in rows.items():
            if _bad(row.close) or t <= 0:
                continue
            iv = bs.implied_vol(row.close, spot, k, t, self.r, right)
            if iv is None:
                continue
            d = abs(bs.delta(spot, k, t, self.r, iv, right))
            if abs(d - target_delta) < best_err:
                best, best_err = k, abs(d - target_delta)
        return best

    def _atm_iv(self, rows: dict, spot: float, t: float, right: str | None = None) -> float | None:
        """ATM implied vol backed out of the chain (≈ India VIX for a monthly)."""
        if t <= 0 or not rows:
            return None
        atm = self._snap(sorted(rows), spot)
        row = rows.get(atm)
        if row is None or _bad(row.close):
            return None
        return bs.implied_vol(row.close, spot, atm, t, self.r, right or self.right)

    def _target_strikes(self, spot: float, expiry: date, today: date, rows: dict,
                        right: str | None = None) -> list:
        """The three base target strikes (buy, sell, hedge) per ``strike_mode``.

        Offsets are OTM distances: ABOVE spot for calls, BELOW spot for puts (sign).
        - points  : spot ± offset (absolute, level-dependent — legacy)
        - percent : spot × (1 ± offset/100) (constant moneyness across levels)
        - delta   : the strike whose |Δ| ≈ offset (vol/time/spot-aware)
        - sd      : spot ± offset × expected-move, EM = spot·IV·√(dte/365) (constant breach-
                    probability — pushes strikes further OTM when vol is high)
        A leg is None if it can't be resolved → caller skips the month.
        """
        right = right or self.right
        sg = 1 if right == "CE" else -1
        offs = (self.buy_offset, self.sell_offset, self.hedge_offset)
        if self._wing_has_tail(right):
            offs += (self.tail_hedge_offset,)  # 4th base strike: the far tail hedge
        if self.strike_mode == "percent":
            return [spot * (1.0 + sg * o / 100.0) for o in offs]
        t = max((expiry - today).days, 0) / 365.0
        if self.strike_mode == "delta":
            return [self._delta_strike(rows, spot, t, o, right) for o in offs]
        if self.strike_mode in ("sd", "expected_move"):
            iv = self._atm_iv(rows, spot, t, right)
            if iv is None:
                return [None, None, None]
            em = spot * iv * math.sqrt(t)
            return [spot + sg * o * em for o in offs]
        return [spot + sg * o for o in offs]  # points (default)

    def _maybe_enter(self, ctx, chain, today: date) -> list[Signal]:
        if not self._entry_allowed(today):
            return []
        if not self._entry_time_ok(self._now(ctx)):
            return []  # entry window not yet reached today (live intraday; EOD-safe)
        expiry = self._select_expiry(chain, today)
        spot = chain.spot(self.underlying, today)
        if expiry is None or spot is None:
            return []
        lot_size = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        base = self._capital_base(ctx)
        self._entry_capital_base = base  # credit gates scale with the same base as the lots
        if self.sizing == "margin":
            # Era-true divisor: the model margin of ONE lot-set's short body at today's spot
            # and this expiry's lot size — tracks lot-size revisions and price level, and
            # matches the report's Margin Used, so utilization is self-consistent in-run.
            per_set = short_option_margin(spot, self.sell_lots * lot_size, 1, MarginParams())
            if per_set > 0:
                lots = int(base * self.capital_utilization_pct / 100.0 // per_set)
                if self.max_auto_lots > 0:
                    lots = min(lots, self.max_auto_lots)
                # Write self.lots (not a local): _tail_units recovers lot_size via
                # units // self.lots, and the live snapshot/report echo the traded count.
                self.lots = max(1, lots)
        units = self.lots * lot_size
        limit = self.credit_debit_limit_pct * base

        sides = self._entry_sides(chain, today, expiry, spot, units, limit)
        if not sides:
            return []  # one (or more) required side didn't qualify → skip the month

        self.legs = []
        signals: list[Signal] = []
        t_units = self._tail_units(units)
        for buy, sell, hedge, tail in sides:
            b_units, s_units, h_units = (self.buy_lots * units, self.sell_lots * units,
                                         self.hedge_lots * units)
            side_legs = [
                {"symbol": buy.symbol, "dir": 1, "units": b_units, "entry": buy.close},
                {"symbol": sell.symbol, "dir": -1, "units": s_units, "entry": sell.close},
                {"symbol": hedge.symbol, "dir": 1, "units": h_units, "entry": hedge.close},
            ]
            side_sigs = [
                Signal(buy.symbol, SignalAction.ENTER_LONG, quantity=b_units, reason=self.entry_reason),
                Signal(sell.symbol, SignalAction.ENTER_SHORT, quantity=s_units,
                       reason=self.entry_reason, meta={"multiplier": 1}),
                Signal(hedge.symbol, SignalAction.ENTER_LONG, quantity=h_units, reason=self.entry_reason),
            ]
            if tail is not None and t_units:
                if tail.symbol == hedge.symbol:
                    # Tail landed on the hedge strike → one doubled-up hedge leg (two
                    # legs on the same symbol would double-fire EXIT_ALL).
                    side_legs[2]["units"] += t_units
                    side_sigs[2] = Signal(hedge.symbol, SignalAction.ENTER_LONG,
                                          quantity=h_units + t_units, reason=self.entry_reason)
                else:
                    side_legs.append(
                        {"symbol": tail.symbol, "dir": 1, "units": t_units, "entry": tail.close})
                    side_sigs.append(Signal(tail.symbol, SignalAction.ENTER_LONG,
                                            quantity=t_units, reason=self.entry_reason))
            self.legs += side_legs
            signals += side_sigs
        self.entry_expiry = expiry
        self.entry_date = today
        self._mark_entered(today)
        return signals

    def _entry_sides(self, chain, today, expiry, spot, units, limit) -> list | None:
        """The (buy, sell, hedge, tail) structures to enter — one side for a single
        ratio; Batman overrides this to require BOTH wings. None/empty → skip the month."""
        side = self._build_side(chain, today, expiry, spot, units, limit, self.right)
        return [side] if side is not None else None

    def _build_side(self, chain, today, expiry, spot, units, limit, right) -> tuple | None:
        """Pick one wing's (buy, sell, hedge, tail) ChainRows for ``right``, or None.
        ``tail`` is the optional far disaster hedge (None when disabled for this wing).

        Only OI>0 strikes are considered (zero-OI contracts carry frozen phantom
        closes). Entry rule: the wing's net (credit minus tail cost) must lie in
        [min_credit_pct, limit] × capital; a too-rich credit shifts all legs further
        OTM (higher strikes for calls, lower for puts); below the floor (debit) skips —
        debit months were shown to be the losers, being flat is the edge.
        """
        sign = 1 if right == "CE" else -1
        rows = {r.strike: r for r in chain.chain(self.underlying, today, expiry)
                if r.right == right and r.oi > 0}
        if not rows:
            return None
        # IV floor: skip while the chain's ATM IV (≈ India VIX) is below min_vix
        # (retried daily within the entry window — a late vol pickup can still qualify).
        if self.min_vix > 0:
            t = max((expiry - today).days, 0) / 365.0
            iv = self._atm_iv(rows, spot, t, right)
            if iv is None or iv * 100.0 < self.min_vix:
                return None

        with_tail = self._wing_has_tail(right)
        base = self._target_strikes(spot, expiry, today, rows, right)
        if any(b is None for b in base[:3]):
            return None  # couldn't resolve a core leg (e.g. delta on thin data)
        t_units = self._tail_units(units) if with_tail else 0

        floor_amt = self.min_credit_pct * self._entry_capital_base
        strike_list = sorted(rows)
        atm = self._snap(strike_list, spot)
        for i in range(self.max_shifts + 1):
            shift = sign * i * self.shift_step
            bk = self._snap(strike_list, base[0] + shift)
            sk = self._snap(strike_list, base[1] + shift)
            hk = self._snap(strike_list, base[2] + shift)
            buy, sell, hedge = rows.get(bk), rows.get(sk), rows.get(hk)
            if (buy is None or sell is None or hedge is None or len({bk, sk, hk}) < 3
                    or (bk - atm) * sign < 0  # buy leg must stay ATM-or-OTM
                    or _bad(buy.close) or _bad(sell.close) or _bad(hedge.close)):
                continue
            tail = None
            if t_units:
                # Tail target. Snapping degrades gracefully: a chain that doesn't extend
                # that far OTM (e.g. NIFTY 2020) snaps to its last listed strike; a tail
                # AT the hedge strike means a deliberate double-hedge (legs merge); only
                # a strictly-inside tail is pushed to the nearest strike beyond the hedge.
                tk = self._snap(strike_list, base[3] + shift) if len(base) > 3 and base[3] else None
                if tk is None:
                    tk = hk
                elif (tk - hk) * sign < 0:
                    beyond = [k for k in strike_list if (k - hk) * sign > 0]
                    tk = (min(beyond) if sign > 0 else max(beyond)) if beyond else hk
                tail = rows.get(tk)
                if tail is None or _bad(tail.close):
                    tail = hedge  # same-strike merge: hedge is simply doubled up
            net = (self.sell_lots * sell.close - self.buy_lots * buy.close
                   - self.hedge_lots * hedge.close) * units  # +ve = credit received
            if tail is not None:
                net -= tail.close * t_units
            if floor_amt <= net <= limit:
                return (buy, sell, hedge, tail)
            if net < floor_amt:
                break  # debit → further OTM only thins premium more
        return None

    def _manage(self, ctx) -> list[Signal]:
        # If the engine already closed our legs (expiry settlement), reset and wait for next month.
        if not any(ctx.lots(leg["symbol"]) for leg in self.legs):
            self._flat()
            return []
        # Stale-mark guard: ctx.close() forward-fills a leg that didn't print today, which
        # can fire the MTM stop (and fill the exit) on a phantom price — e.g. the long leg
        # marks down while an unprinted short stays at its entry price. Only evaluate
        # exits when EVERY leg has a fresh print; otherwise manage on the next slice.
        market = getattr(ctx, "market", None)
        if market is not None and hasattr(market, "has_print"):
            if not all(market.has_print(leg["symbol"]) for leg in self.legs):
                return []
        try:
            pnl = sum(leg["dir"] * (ctx.close(leg["symbol"]) - leg["entry"]) * leg["units"]
                      for leg in self.legs)
        except KeyError:
            return []  # a leg didn't print today; manage next slice
        base = self._risk_base(ctx)
        today = ctx.today()
        now = self._now(ctx)
        reason = None
        if self._due("profit", now) and pnl >= self.profit_target_pct * base:
            reason = "target"
        elif self._due("stop", now) and pnl <= -self.stop_loss_pct * base:
            reason = "stop"
        elif self._due("time", now) and self._time_exit(today):
            reason = "time"
        if reason is None:
            return []
        signals = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in self.legs]
        self._flat()
        return signals

    def _flat(self) -> None:
        self.legs = []
        self.entry_expiry = None
        self.entry_date = None

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "legs": list(self.legs),
            "strike_mode": self.strike_mode,
            "entry_expiry": self.entry_expiry.isoformat() if self.entry_expiry else None,
            "entry_date": self.entry_date.isoformat() if self.entry_date else None,
            "last_entry_month": list(self.last_entry_month) if self.last_entry_month else None,
            "entered_after_expiry": (self._entered_after_expiry.isoformat()
                                     if self._entered_after_expiry else None),
        }

    def load_state(self, state: dict) -> None:
        self.legs = list(state.get("legs", []))
        self.strike_mode = state.get("strike_mode", self.strike_mode)
        ee, ed, lem = state.get("entry_expiry"), state.get("entry_date"), state.get("last_entry_month")
        self.entry_expiry = date.fromisoformat(ee) if ee else None
        self.entry_date = date.fromisoformat(ed) if ed else None
        self.last_entry_month = tuple(lem) if lem else None
        eae = state.get("entered_after_expiry")
        self._entered_after_expiry = date.fromisoformat(eae) if eae else None


class PutRatioMonthlyStrategy(CallRatioMonthlyStrategy):
    """1:2 PUT ratio spread + outer hedge — the downside mirror of the call ratio.

    BUY 1 put ~offset below spot, SELL 2 puts further below, BUY 1 far put hedge.
    Zero UPSIDE risk (all puts expire worthless on rallies → keep the credit); risk is
    a fast SELL-OFF toward the short strikes, capped beyond the hedge. Same entry
    timing, credit gate (skip debit months), and exits as the call version.
    """

    strategy_id = "put_ratio_monthly"
    right = "PE"
    entry_reason = "put_ratio"


class BatmanRatioMonthlyStrategy(CallRatioMonthlyStrategy):
    """"Batman": BOTH ratio wings in one position — a 1:2 call ratio spread above spot
    AND a 1:2 put ratio spread below spot, each with its outer hedge (6 legs; the payoff
    tent on each side draws the silhouette).

    Each wing is constructed exactly like its standalone strategy (own credit search,
    NET CREDIT ≤ 1% of capital per wing, debit wing → skip). BOTH wings must qualify or
    the month is skipped — a single qualifying wing is just the plain ratio strategy.
    Management is COMBINED: one profit-target / stop-loss / time exit on the 6-leg MTM
    P&L. Profit zone is the whole band between the short strikes (theta from both
    sides); risk is a fast move in EITHER direction, capped beyond the hedges.
    Margin ≈ 2× a single ratio (~₹2L per lot-set) — size capital accordingly.

    Batman defaults to a HALF-SIZE PUT-WING TAIL HEDGE (offset 2100 pts, 0.5× lots):
    the 2020-26 sweep showed gap-crashes jump the EOD MTM stop (Apr-2025: −31k→−218k
    overnight) and the far put's vega cut that worst loss to −124k while keeping ~86%
    of the un-tailed P&L and the best risk-adjusted return. Set tail_hedge_offset=0
    to reproduce the un-hedged variant.
    """

    strategy_id = "batman_ratio_monthly"
    entry_reason = "batman"

    def __init__(self, *args, combined_credit_limit_pct: float = 0.02,
                 tail_hedge_offset: float = 2100.0, tail_hedge_lots: float = 0.5,
                 tail_hedge_side: str = "put", margin_per_lotset: float = 200_000.0, **kwargs):
        # Batman runs BOTH ratio wings → ~2× a single ratio's margin per lot-set.
        super().__init__(*args, tail_hedge_offset=tail_hedge_offset,
                         tail_hedge_lots=tail_hedge_lots, tail_hedge_side=tail_hedge_side,
                         margin_per_lotset=margin_per_lotset, **kwargs)
        # Cap on the COMBINED (both wings) net credit, as a fraction of capital. The
        # per-wing cap (credit_debit_limit_pct, 1%) still applies, so the default 2%
        # changes nothing; a tighter combined cap re-shifts BOTH wings further OTM.
        self.combined_credit_limit_pct = float(combined_credit_limit_pct)

    def _wing_credit(self, side: tuple, units: int) -> float:
        buy, sell, hedge, tail = side
        net = (self.sell_lots * sell.close - self.buy_lots * buy.close
               - self.hedge_lots * hedge.close) * units
        if tail is not None:
            net -= tail.close * self._tail_units(units)
        return net

    def _entry_sides(self, chain, today, expiry, spot, units, limit) -> list | None:
        combined_limit = self.combined_credit_limit_pct * self._entry_capital_base
        # Round 1: each wing under its own cap (≤ the combined cap, in case it's tighter).
        wing_limit = min(limit, combined_limit)
        ce = self._build_side(chain, today, expiry, spot, units, wing_limit, "CE")
        pe = self._build_side(chain, today, expiry, spot, units, wing_limit, "PE")
        if ce is None or pe is None:
            return None  # both wings or nothing
        if self._wing_credit(ce, units) + self._wing_credit(pe, units) <= combined_limit:
            return [ce, pe]
        # Round 2: combined credit too rich → rebuild with half the combined cap per
        # wing (guarantees the sum fits); wings shift further OTM or the month skips.
        wing_limit = combined_limit / 2.0
        ce = self._build_side(chain, today, expiry, spot, units, wing_limit, "CE")
        pe = self._build_side(chain, today, expiry, spot, units, wing_limit, "PE")
        if ce is None or pe is None:
            return None
        return [ce, pe]
