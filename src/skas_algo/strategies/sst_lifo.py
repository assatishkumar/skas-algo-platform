"""SST Per-Lot (LIFO) strategy, ported from skas-trading.

Entry: track a symbol when it prints a 20-day low; buy when it breaks above the
20-day high (Donchian breakout), sizing each buy at capital/parts.

Exit: every lot is checked independently — any lot up >= profit_target from its own
entry is sold, regardless of the other lots. Tracking resets when all lots close.

This is the *same logic* as the reference backtest, but expressed as Signals against
an AlgoContext, so the engine can run it in BACKTEST, PAPER, or LIVE unchanged.
Cash/lots/PnL accounting and the monthly tax flush live in the engine, not here.
"""

from __future__ import annotations

from typing import Any

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class SSTLifoStrategy:
    strategy_id = "sst_lifo"

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 2_500_000,
        capital_parts: int = 50,
        profit_target: float = 0.06,
        max_lots: int = 0,  # 0 = unlimited
    ):
        self.universe = universe
        self.profit_target = profit_target
        self.max_lots = max_lots
        self.allocation_amount = initial_capital / capital_parts
        # True => saw a 20-day low, waiting for the 20-day-high breakout to buy.
        self.tracking: dict[str, bool] = {s: False for s in universe}

    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tracking": dict(self.tracking)}

    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        present = ctx.present_symbols()
        present_set = set(present)
        signals: list[Signal] = []

        # Local projection of cash so buy affordability matches the reference, which
        # adds this day's sell proceeds before buying. Fills are at the same close,
        # so the engine's resulting cash equals this projection.
        running_cash = ctx.cash
        sold_counts: dict[str, int] = {}

        # --- Step 1: sells — every lot independently, in portfolio order ---
        for ticker in ctx.lot_symbols():
            if ticker not in present_set:
                continue
            close = ctx.close(ticker)
            kept_any = False
            for lot in ctx.lots(ticker):
                if (close - lot.price) / lot.price >= self.profit_target:
                    signals.append(Signal(symbol=ticker, action=SignalAction.EXIT, lot_id=lot.id))
                    running_cash += lot.units * close
                    sold_counts[ticker] = sold_counts.get(ticker, 0) + 1
                else:
                    kept_any = True
            if not kept_any:
                self.tracking[ticker] = False  # all lots closed — reset tracking

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
            if running_cash < self.allocation_amount:
                continue
            current_lots = len(ctx.lots(ticker)) - sold_counts.get(ticker, 0)
            if self.max_lots > 0 and current_lots >= self.max_lots:
                self.tracking[ticker] = False
                continue
            units = int(self.allocation_amount // close)
            if units <= 0:
                continue
            running_cash -= units * close
            signals.append(Signal(symbol=ticker, action=SignalAction.ENTER_LONG, quantity=units))
            self.tracking[ticker] = False

        return signals
