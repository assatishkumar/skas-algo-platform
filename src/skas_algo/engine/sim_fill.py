"""Shared simulated-fill model used by both BacktestBroker and PaperBroker.

They differ only in where the reference price comes from (historical bar vs live
quote); the slippage/commission math is identical. Defaults are zero so BACKTEST
reproduces the reference SST backtest (which models neither). Set non-zero values
for more realistic forward-tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from skas_algo.db.enums import OrderSide


@dataclass(frozen=True)
class FillModel:
    """Slippage (in basis points) and per-trade commission."""

    slippage_bps: float = 0.0
    commission_pct: float = 0.0  # fraction of notional, e.g. 0.0003 = 3 bps

    def fill_price(self, reference_price: float, side: OrderSide) -> float:
        if self.slippage_bps == 0:
            return reference_price
        slip = reference_price * (self.slippage_bps / 10_000.0)
        # Buyers pay up, sellers receive less.
        return reference_price + slip if side is OrderSide.BUY else reference_price - slip

    def commission(self, price: float, quantity: int) -> float:
        if self.commission_pct == 0:
            return 0.0
        return abs(price * quantity) * self.commission_pct
