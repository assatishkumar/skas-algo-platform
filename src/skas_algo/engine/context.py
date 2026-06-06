"""AlgoContext — the handle a strategy uses to observe the world.

It exposes the portfolio (cash, lots), the market view (today's close + rolling
levels), and run parameters. The same context type is used in every mode, so a
strategy written against it runs unchanged in BACKTEST, PAPER, and LIVE.
"""

from __future__ import annotations

from typing import Any

from .market import MarketView
from .portfolio import Lot, Portfolio


class AlgoContext:
    def __init__(
        self,
        algo_id: int | None,
        params: dict[str, Any],
        portfolio: Portfolio,
        market: MarketView,
    ):
        self.algo_id = algo_id
        self.params = params
        self.portfolio = portfolio
        self.market = market

    # ----- portfolio -----
    @property
    def cash(self) -> float:
        return self.portfolio.cash

    def lots(self, symbol: str) -> list[Lot]:
        return self.portfolio.lots(symbol)

    def lot_symbols(self) -> list[str]:
        return self.portfolio.lot_symbols()

    # ----- market (today) -----
    def present_symbols(self) -> list[str]:
        return self.market.present_symbols()

    def close(self, symbol: str) -> float:
        return self.market.close(symbol)

    def rolling_high(self, symbol: str) -> float:
        return self.market.rolling_high(symbol)

    def rolling_low(self, symbol: str) -> float:
        return self.market.rolling_low(symbol)
