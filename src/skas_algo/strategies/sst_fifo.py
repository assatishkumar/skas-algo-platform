"""SST (pooled / averaged-target) strategy — the plain SST from skas-trading.

Same entry as SST-LIFO: track on a 20-day low, buy on the 20-day-high breakout,
average in on repeat triggers.

Exit differs: all lots of a symbol exit *together* (one transaction) when the
position's **average-cost** profit reaches a target that tightens as lots accumulate:
  1 lot  -> profit_target_1 (default 10%)
  2 lots -> profit_target_2 (default  8%)
  3+ lots-> profit_target_3 (default  6%)

(The user calls this the "FIFO" variant vs the per-lot "LIFO" one; functionally the
whole position exits at once, so lot order is moot on exit.)

Shares the engine's params/reports with SST-LIFO: capital parts, max_lots, tax,
withdrawal, and the fixed vs equity_scaled position sizing.
"""

from __future__ import annotations

from typing import Any

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class SSTFifoStrategy:
    strategy_id = "sst_fifo"

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 2_500_000,
        capital_parts: int = 50,
        profit_target_1: float = 0.10,
        profit_target_2: float = 0.08,
        profit_target_3: float = 0.06,
        max_lots: int = 0,  # 0 = unlimited
        allocation_mode: str = "fixed",  # "fixed" | "equity_scaled"
    ):
        self.universe = universe
        self.profit_target_1 = profit_target_1
        self.profit_target_2 = profit_target_2
        self.profit_target_3 = profit_target_3
        self.max_lots = max_lots
        self.capital_parts = capital_parts
        self.allocation_mode = allocation_mode
        self.allocation_amount = initial_capital / capital_parts
        self.tracking: dict[str, bool] = {s: False for s in universe}

    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tracking": dict(self.tracking)}

    def export_state(self) -> dict[str, Any]:
        return {"tracking": dict(self.tracking)}

    def load_state(self, state: dict[str, Any]) -> None:
        self.tracking = {**self.tracking, **state.get("tracking", {})}

    def _target(self, lots: int) -> float:
        if lots == 1:
            return self.profit_target_1
        if lots == 2:
            return self.profit_target_2
        return self.profit_target_3

    def _allocation(self, ctx: AlgoContext) -> float:
        if self.allocation_mode == "equity_scaled":
            return ctx.equity() / self.capital_parts
        return self.allocation_amount

    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        present = ctx.present_symbols()
        present_set = set(present)
        signals: list[Signal] = []
        running_cash = ctx.cash
        allocation = self._allocation(ctx)

        # --- Step 1: pooled sells (whole position exits at the tiered target) ---
        for ticker in ctx.lot_symbols():
            if ticker not in present_set:
                continue
            lots = ctx.lots(ticker)
            if not lots:
                continue
            close = ctx.close(ticker)
            total_units = sum(lot.units for lot in lots)
            total_cost = sum(lot.units * lot.price for lot in lots)
            avg_cost = total_cost / total_units
            if (close - avg_cost) / avg_cost >= self._target(len(lots)):
                signals.append(Signal(symbol=ticker, action=SignalAction.EXIT_ALL))
                running_cash += total_units * close
                self.tracking[ticker] = False  # position closed — reset tracking

        # --- Step 2: tracking update (new 20-day lows) ---
        for ticker in present:
            if ctx.close(ticker) < ctx.rolling_low(ticker):
                self.tracking[ticker] = True

        # --- Step 3: buys (Donchian breakout) ---
        for ticker in present:
            if not self.tracking.get(ticker, False):
                continue
            close = ctx.close(ticker)
            if close <= ctx.rolling_high(ticker):
                continue
            if running_cash < allocation:
                continue
            # A ticker sold this slice has tracking=False above, so it's never re-bought
            # here; ctx.lots therefore reflects the correct (unchanged) lot count.
            current_lots = len(ctx.lots(ticker))
            if self.max_lots > 0 and current_lots >= self.max_lots:
                self.tracking[ticker] = False
                continue
            units = int(allocation // close)
            if units <= 0:
                continue
            running_cash -= units * close
            signals.append(Signal(symbol=ticker, action=SignalAction.ENTER_LONG, quantity=units))
            self.tracking[ticker] = False

        return signals
