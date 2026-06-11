"""Call Ratio Monthly — a 1:2 call ratio spread with an outer hedge on NIFTY monthly.

Structure (all CE, next month's monthly expiry):
  * BUY  1× at ~spot+buy_offset   (long, near)
  * SELL 2× at ~spot+sell_offset  (short body)
  * BUY  1× at ~spot+hedge_offset (long, far hedge — caps upside loss)

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
from datetime import date, timedelta

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.types import Signal, SignalAction


def _bad(x) -> bool:
    return x is None or x != x or x <= 0  # None / NaN / non-positive premium


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
        credit_debit_limit_pct: float = 0.01,   # max net CREDIT = this × capital (credit required)
        shift_step: float = 100,                 # strike-adjust step (searched ± both directions)
        max_shifts: int = 10,                    # search up to ±max_shifts × shift_step
        profit_target_pct: float = 0.025,        # exit at +2.5% of capital
        stop_loss_pct: float = 0.03,             # exit at −3% of capital
        max_holding_days: int = 20,              # hard time exit (avoid end-of-month gamma)
        min_vix: float = 0.0,                     # skip entry if ATM IV% (≈ India VIX) below this
        min_dte: int = 18,                        # selects the *next* month's monthly expiry
        entry_weekday: int = 1,                   # Tuesday
        strike_step: float = 50,                  # informational; strikes are snapped to listings
        risk_free_rate: float = 0.065,
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
        self.credit_debit_limit_pct = float(credit_debit_limit_pct)
        self.shift_step = float(shift_step)
        self.max_shifts = int(max_shifts)
        self.profit_target_pct = float(profit_target_pct)
        self.stop_loss_pct = float(stop_loss_pct)
        self.max_holding_days = int(max_holding_days)
        self.min_vix = float(min_vix)
        self.min_dte = int(min_dte)
        self.entry_weekday = int(entry_weekday)
        self.strike_step = float(strike_step)
        self.r = float(risk_free_rate)
        self.lot_overrides = lot_overrides
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

    # ------------------------------------------------------------------ helpers
    def _next_monthly_expiry(self, chain, today: date) -> date | None:
        """The nearest monthly expiry at least ``min_dte`` out.

        "Monthly" = the most LIQUID expiry of its calendar month (highest total open
        interest on today's chain), not simply the latest date — exchanges sometimes list
        odd late-month expiries whose contracts never trade but still carry frozen
        bhavcopy closes (e.g. NIFTY 2025-04-30 vs the real 2025-04-24 monthly); picking
        by date would enter phantom, un-executable positions.
        """
        exps = chain.expiries(self.underlying, today)
        if not exps:
            return None
        by_month: dict[tuple[int, int], list[date]] = {}
        for e in exps:
            if (e - today).days >= self.min_dte:
                by_month.setdefault((e.year, e.month), []).append(e)
        if not by_month:
            return None
        month = min(by_month)  # nearest qualifying month
        cands = by_month[month]
        if len(cands) == 1:
            return cands[0]
        def total_oi(exp: date) -> int:
            return sum(r.oi for r in chain.chain(self.underlying, today, exp) if r.right == self.right)
        return max(cands, key=total_oi)

    @staticmethod
    def _snap(strikes: list[float], target: float) -> float | None:
        return min(strikes, key=lambda k: abs(k - target)) if strikes else None

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
        ym = (today.year, today.month)
        if self.last_entry_month == ym:
            return []  # already traded this month (one entry / month, zero adjustments)
        if today < _last_weekday_of_month(today, self.entry_weekday):
            return []  # entry window (last Tuesday → on/after) not reached
        expiry = self._next_monthly_expiry(chain, today)
        spot = chain.spot(self.underlying, today)
        if expiry is None or spot is None:
            return []
        units = self.lots * lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        limit = self.credit_debit_limit_pct * self.initial_capital

        sides = self._entry_sides(chain, today, expiry, spot, units, limit)
        if not sides:
            return []  # one (or more) required side didn't qualify → skip the month

        self.legs = []
        signals: list[Signal] = []
        for buy, sell, hedge in sides:
            self.legs += [
                {"symbol": buy.symbol, "dir": 1, "units": units, "entry": buy.close},
                {"symbol": sell.symbol, "dir": -1, "units": 2 * units, "entry": sell.close},
                {"symbol": hedge.symbol, "dir": 1, "units": units, "entry": hedge.close},
            ]
            signals += [
                Signal(buy.symbol, SignalAction.ENTER_LONG, quantity=units, reason=self.entry_reason),
                Signal(sell.symbol, SignalAction.ENTER_SHORT, quantity=2 * units,
                       reason=self.entry_reason, meta={"multiplier": 1}),
                Signal(hedge.symbol, SignalAction.ENTER_LONG, quantity=units, reason=self.entry_reason),
            ]
        self.entry_expiry = expiry
        self.entry_date = today
        self.last_entry_month = ym
        return signals

    def _entry_sides(self, chain, today, expiry, spot, units, limit) -> list | None:
        """The (buy, sell, hedge) structures to enter — one side for a single ratio;
        Batman overrides this to require BOTH wings. None/empty → skip the month."""
        side = self._build_side(chain, today, expiry, spot, units, limit, self.right)
        return [side] if side is not None else None

    def _build_side(self, chain, today, expiry, spot, units, limit, right) -> tuple | None:
        """Pick one wing's (buy, sell, hedge) ChainRows for ``right``, or None.

        Only OI>0 strikes are considered (zero-OI contracts carry frozen phantom
        closes). Entry rule: the wing must be a NET CREDIT ≤ ``limit`` (1% of capital);
        a too-rich credit shifts all legs further OTM (higher strikes for calls, lower
        for puts); a DEBIT (low IV / thin premiums) skips — debit months were shown to
        be the losers, being flat is the edge.
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

        base = self._target_strikes(spot, expiry, today, rows, right)
        if any(b is None for b in base):
            return None  # couldn't resolve a leg (e.g. delta on thin data)

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
            net = (2 * sell.close - buy.close - hedge.close) * units  # +ve = credit received
            if 0 <= net <= limit:
                return (buy, sell, hedge)
            if net < 0:
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
        cap = self.initial_capital
        today = ctx.today()
        reason = None
        if pnl >= self.profit_target_pct * cap:
            reason = "target"
        elif pnl <= -self.stop_loss_pct * cap:
            reason = "stop"
        elif self.entry_date and (today - self.entry_date).days >= self.max_holding_days:
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
        }

    def load_state(self, state: dict) -> None:
        self.legs = list(state.get("legs", []))
        self.strike_mode = state.get("strike_mode", self.strike_mode)
        ee, ed, lem = state.get("entry_expiry"), state.get("entry_date"), state.get("last_entry_month")
        self.entry_expiry = date.fromisoformat(ee) if ee else None
        self.entry_date = date.fromisoformat(ed) if ed else None
        self.last_entry_month = tuple(lem) if lem else None


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
    """

    strategy_id = "batman_ratio_monthly"
    entry_reason = "batman"

    def __init__(self, *args, combined_credit_limit_pct: float = 0.02, **kwargs):
        super().__init__(*args, **kwargs)
        # Cap on the COMBINED (both wings) net credit, as a fraction of capital. The
        # per-wing cap (credit_debit_limit_pct, 1%) still applies, so the default 2%
        # changes nothing; a tighter combined cap re-shifts BOTH wings further OTM.
        self.combined_credit_limit_pct = float(combined_credit_limit_pct)

    @staticmethod
    def _wing_credit(side: tuple, units: int) -> float:
        buy, sell, hedge = side
        return (2 * sell.close - buy.close - hedge.close) * units

    def _entry_sides(self, chain, today, expiry, spot, units, limit) -> list | None:
        combined_limit = self.combined_credit_limit_pct * self.initial_capital
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
