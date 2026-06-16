"""SST Weekly — the SST Donchian breakout system on a WEEKLY timeframe.

Same idea as SST-LIFO, but every level and decision is weekly instead of daily:
  * Track a symbol when its (weekly) close prints a ``donchian_weeks``-week LOW.
  * Buy when the close breaks above the ``donchian_weeks``-week HIGH (Donchian breakout).
  * Exit each lot independently once it is up ``profit_target`` from its own entry (LIFO).
  * Size each buy at capital/parts (fixed) or equity/parts (equity-scaled, compounds).

The platform engine runs on daily bars, so — like ``hni_weekly`` — this strategy keeps a
per-symbol buffer of completed WEEKLY closes (built from the daily stream) and only acts on
the first trading day of each new ISO week, evaluating the breakout against the prior weeks.
No engine/data changes are needed; it runs unchanged in BACKTEST, PAPER and LIVE.

Note: the run's ``lookback`` only gates when a symbol becomes visible (valid daily levels);
the weekly Donchian window is ``donchian_weeks``. Allow ~``donchian_weeks`` weeks of warmup
from the start date before the first signals.
"""

from __future__ import annotations

from typing import Any

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class SSTWeeklyStrategy:
    strategy_id = "sst_weekly"

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 2_500_000,
        capital_parts: int = 50,
        profit_target: float = 0.15,       # per-lot exit; weekly trends run further than daily
        donchian_weeks: int = 20,          # the weekly Donchian window (high/low)
        max_lots: int = 0,                 # 0 = unlimited
        allocation_mode: str = "fixed",    # "fixed" | "equity_scaled"
        **_ignored,                        # tolerate SST-style extras from shared forms
    ):
        self.universe = universe
        self.profit_target = float(profit_target)
        self.donchian_weeks = int(donchian_weeks)
        self.max_lots = int(max_lots)
        self.capital_parts = int(capital_parts)
        self.allocation_mode = allocation_mode
        self.allocation_amount = initial_capital / capital_parts
        self.tracking: dict[str, bool] = {s: False for s in universe}
        # Weekly aggregation state (built from the daily stream).
        self._cur_week: dict[str, tuple[int, int]] = {}   # symbol -> ISO (year, week)
        self._cur_close: dict[str, float] = {}            # latest close in the current week
        self._weeks: dict[str, list[float]] = {}          # completed weekly closes, old→new

    def _allocation(self, ctx: AlgoContext) -> float:
        if self.allocation_mode == "equity_scaled":
            return ctx.equity() / self.capital_parts
        return self.allocation_amount

    # ------------------------------------------------------- (de)serialize
    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.export_state()

    def export_state(self) -> dict[str, Any]:
        return {
            "tracking": dict(self.tracking),
            "cur_week": {s: list(w) for s, w in self._cur_week.items()},
            "cur_close": dict(self._cur_close),
            "weeks": {s: list(v) for s, v in self._weeks.items()},
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.tracking = {**self.tracking, **state.get("tracking", {})}
        self._cur_week = {s: tuple(w) for s, w in state.get("cur_week", {}).items()}
        self._cur_close = dict(state.get("cur_close", {}))
        self._weeks = {s: list(v) for s, v in state.get("weeks", {}).items()}

    # ------------------------------------------------------------------ decide
    def _weekly_levels(self, sym: str) -> tuple[float, float] | None:
        """(high, low) over the last ``donchian_weeks`` COMPLETED weeks, or None if warming up."""
        weeks = self._weeks.get(sym, [])
        if len(weeks) < self.donchian_weeks:
            return None
        window = weeks[-self.donchian_weeks :]
        return max(window), min(window)

    def _exit_for_symbol(self, ctx: AlgoContext, sym: str):
        """LIFO exit: each lot up >= ``profit_target`` from its own entry sells independently.

        Returns ``(exit_signals, lots_sold, cash_freed, all_lots_closed)``. Overridden by the
        FIFO subclass for a pooled tiered exit.
        """
        close = ctx.close(sym)
        lots = ctx.lots(sym)
        exits: list[Signal] = []
        freed = 0.0
        sold = 0
        kept = False
        for lot in lots:
            if (close - lot.price) / lot.price >= self.profit_target:
                exits.append(Signal(symbol=sym, action=SignalAction.EXIT, lot_id=lot.id))
                freed += lot.units * close
                sold += 1
            else:
                kept = True
        return exits, sold, freed, (bool(lots) and not kept)

    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        present = ctx.present_symbols()
        signals: list[Signal] = []

        # --- Roll the weekly buffers; note which symbols crossed into a new week ---
        wk = ctx.today().isocalendar()[:2]
        new_week: set[str] = set()
        for sym in present:
            close = ctx.close(sym)
            cur = self._cur_week.get(sym)
            if cur is None:
                self._cur_week[sym] = wk
                self._cur_close[sym] = close
            elif tuple(cur) != wk:
                # Prior week completed → finalize its close into the weekly buffer.
                self._weeks.setdefault(sym, []).append(self._cur_close[sym])
                self._cur_week[sym] = wk
                self._cur_close[sym] = close
                new_week.add(sym)
            else:
                self._cur_close[sym] = close  # latest print becomes this week's running close

        if not new_week:
            return signals  # mid-week: hold; SST Weekly only decides at week boundaries

        running_cash = ctx.cash
        allocation = self._allocation(ctx)
        sold_counts: dict[str, int] = {}

        # --- Step 1: exits at the week boundary (LIFO per-lot here; the FIFO subclass
        #     overrides _exit_for_symbol with a pooled tiered exit) ---
        for sym in ctx.lot_symbols():
            if sym not in new_week:
                continue
            exits, n_sold, freed, all_closed = self._exit_for_symbol(ctx, sym)
            signals.extend(exits)
            running_cash += freed
            if n_sold:
                sold_counts[sym] = n_sold
            if all_closed:
                self.tracking[sym] = False  # all lots closed — reset tracking

        # --- Step 2: tracking update (new N-week low) ---
        for sym in new_week:
            lv = self._weekly_levels(sym)
            if lv is not None and ctx.close(sym) < lv[1]:
                self.tracking[sym] = True

        # --- Step 3: buys (weekly Donchian breakout) ---
        for sym in new_week:
            if not self.tracking.get(sym, False):
                continue
            lv = self._weekly_levels(sym)
            if lv is None:
                continue
            close = ctx.close(sym)
            if close <= lv[0]:
                continue
            if running_cash < allocation:
                continue
            current_lots = len(ctx.lots(sym)) - sold_counts.get(sym, 0)
            if self.max_lots > 0 and current_lots >= self.max_lots:
                self.tracking[sym] = False
                continue
            units = int(allocation // close)
            if units <= 0:
                continue
            running_cash -= units * close
            signals.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG, quantity=units))
            self.tracking[sym] = False

        return signals


class SSTWeeklyFifoStrategy(SSTWeeklyStrategy):
    """SST Weekly with the pooled (FIFO) exit: the WHOLE position exits in one transaction
    when its AVERAGE-cost gain reaches a target that tightens as lots accumulate —
      1 lot  -> profit_target_1   (default 20%)
      2 lots -> profit_target_2   (default 15%)
      3+ lots-> profit_target_3   (default 12%)
    Entry (weekly Donchian breakout, pyramiding, weekly cadence) is identical to SST Weekly.
    """

    strategy_id = "sst_weekly_fifo"

    def __init__(self, *args, profit_target_1: float = 0.20, profit_target_2: float = 0.15,
                 profit_target_3: float = 0.12, **kwargs):
        super().__init__(*args, **kwargs)
        self.profit_target_1 = float(profit_target_1)
        self.profit_target_2 = float(profit_target_2)
        self.profit_target_3 = float(profit_target_3)

    def _target(self, lots: int) -> float:
        if lots == 1:
            return self.profit_target_1
        if lots == 2:
            return self.profit_target_2
        return self.profit_target_3

    def _exit_for_symbol(self, ctx: AlgoContext, sym: str):
        lots = ctx.lots(sym)
        if not lots:
            return [], 0, 0.0, False
        close = ctx.close(sym)
        units = sum(lot.units for lot in lots)
        avg = sum(lot.units * lot.price for lot in lots) / units
        if avg > 0 and (close - avg) / avg >= self._target(len(lots)):
            return [Signal(symbol=sym, action=SignalAction.EXIT_ALL)], len(lots), units * close, True
        return [], 0, 0.0, False
