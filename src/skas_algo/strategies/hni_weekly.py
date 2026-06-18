"""HNI Weekly — NIFTY 1-3-2 net-zero call ratio "tent" on the ~8-DTE weekly.

From the HNI deck (StockMock-verified):
  * BUY  1× at ~spot+200 (near long)
  * SELL 3× at ~spot+400 (short body)
  * BUY  2× at ~spot+600 (far hedge)
Net contracts +1−3+2 = 0 → limited risk on BOTH sides, no naked tail. Enter MONDAY
(or the week's first trading day if Monday is a holiday) into the weekly expiring
NEXT Tuesday (~8 DTE — the deck's "bi-weekly"), exit FRIDAY of the entry week (the
deck's 5-day duration; no weekend carry), with ±1% of DEPLOYED MARGIN (₹1.32L per
1-3-2 lot-set) as the profit target / stop loss.

The equidistant 200/400/600 offsets + net-zero ratio make max profit ≈ max loss
(R:R ~1:1) by construction — a small net credit/debit only nudges it — so entry is
NOT gated on the credit sign: the deck offsets are taken every week (min_credit_pct
defaults to a deep negative floor, max_shifts=0 pins the strikes).

EOD engine caveats (documented in docs/PLAN-hni-weekly.md): the deck's 9:45 AM entry
and intraday ±1% exits are approximated at daily closes; weekly Tuesday expiries
exist in the cache from 2025-09-01, so backtest from there.
"""

from __future__ import annotations

from datetime import date

from .call_ratio_monthly import CallRatioMonthlyStrategy


class HniWeeklyStrategy(CallRatioMonthlyStrategy):
    strategy_id = "hni_weekly"
    entry_reason = "hni_weekly"

    def __init__(
        self,
        *args,
        buy_lots: int = 1,
        sell_lots: int = 3,
        hedge_lots: int = 2,
        strike_mode: str = "points",
        buy_offset: float = 200,
        sell_offset: float = 400,
        hedge_offset: float = 600,
        max_shifts: int = 0,                     # strict deck offsets — never shift strikes
        min_credit_pct: float = -10.0,           # ±10× capital ⇒ the credit gate never bites:
        credit_debit_limit_pct: float = 10.0,    # enter every week regardless of credit/debit
        tail_hedge_offset: float = 0.0,          # net-zero already — no disaster tail
        entry_weekday: int = 0,                  # Monday
        exit_weekday: int = 4,                   # Friday force-exit (no weekend carry)
        dte_target: int = 8,                     # next Tuesday's weekly from a Monday
        dte_tolerance: int = 3,                  # no ~8-DTE expiry listed → skip the week
        margin_per_lotset: float = 132_000.0,    # deployed margin per 1-3-2 lot-set
        profit_target_pct: float = 0.01,         # ±1% of deployed margin (not capital)
        stop_loss_pct: float = 0.01,
        # Live cadence: enter 09:45, book profit every 15 min, stop/time at EOD (the deck's
        # intraday rule; backtest stays EOD → these change nothing there).
        entry_time: str | None = "09:45",
        profit_check: str = "15min",
        stop_check: str = "eod",
        time_check: str = "eod",
        **kwargs,
    ):
        super().__init__(
            *args, buy_lots=buy_lots, sell_lots=sell_lots, hedge_lots=hedge_lots,
            strike_mode=strike_mode, buy_offset=buy_offset, sell_offset=sell_offset,
            hedge_offset=hedge_offset, max_shifts=max_shifts,
            min_credit_pct=min_credit_pct, credit_debit_limit_pct=credit_debit_limit_pct,
            tail_hedge_offset=tail_hedge_offset, entry_weekday=entry_weekday,
            profit_target_pct=profit_target_pct, stop_loss_pct=stop_loss_pct,
            entry_time=entry_time, profit_check=profit_check, stop_check=stop_check,
            time_check=time_check,
            **kwargs,
        )
        self.exit_weekday = int(exit_weekday)
        self.dte_target = int(dte_target)
        self.dte_tolerance = int(dte_tolerance)
        self.margin_per_lotset = float(margin_per_lotset)
        self.last_entry_week: tuple[int, int] | None = None
        # First TRADING DAY of the current ISO-week (intraday-safe: many ticks/day don't
        # consume it, unlike a per-slice flag) — stands in for Monday on a holiday week.
        self._week: tuple[int, int] | None = None
        self._week_first_day: date | None = None

    # --------------------------------------------------- weekly timing overrides
    def _entry_allowed(self, today: date) -> bool:
        week = today.isocalendar()[:2]
        if week != self._week:
            self._week, self._week_first_day = week, today
        if self.last_entry_week == week:
            return False  # one trade per ISO-week
        # Monday, or (holiday Monday) the week's first trading day — but not on/after the
        # exit weekday (entering and force-exiting the same day is not the deck).
        if today.weekday() == self.entry_weekday:
            return True
        return today == self._week_first_day and today.weekday() < self.exit_weekday

    def _mark_entered(self, today: date) -> None:
        self.last_entry_week = today.isocalendar()[:2]

    def _select_expiry(self, chain, today: date) -> date | None:
        expiry = chain.expiry_for_dte(self.underlying, today, self.dte_target)
        if expiry is None or abs((expiry - today).days - self.dte_target) > self.dte_tolerance:
            return None  # holiday-shifted week with no ~8-DTE weekly → skip
        return expiry

    def _risk_base(self, ctx=None) -> float:
        # Actual deployed margin (real broker margin live, model estimate in backtest) when known;
        # else the per-lot-set config estimate. Either way it's margin, not account capital.
        fn = getattr(ctx, "position_margin", None) if ctx is not None else None
        m = fn() if fn is not None else None
        return m if m and m > 0 else self.margin_per_lotset * self.lots

    def _time_exit(self, today: date) -> bool:
        if self.entry_date is None:
            return False
        return (today.weekday() >= self.exit_weekday
                or today.isocalendar()[:2] != self.entry_date.isocalendar()[:2])

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        state = super().export_state()
        state["last_entry_week"] = list(self.last_entry_week) if self.last_entry_week else None
        return state

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        lew = state.get("last_entry_week")
        self.last_entry_week = tuple(lew) if lew else None
