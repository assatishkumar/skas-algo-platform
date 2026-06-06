"""Simulated brokers: BacktestBroker and PaperBroker.

Both execute orders synchronously through the same FillModel; they differ only in
the reference-price source. This is what makes "backtest == forward-test" hold: the
fill logic is one module, exercised by both.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from itertools import count

from skas_algo.engine.sim_fill import FillModel

from .base import BrokerOrder, Fill, Funds, Session


class SimBroker(ABC):
    """Base for brokers that simulate fills against a reference price."""

    def __init__(self, fill_model: FillModel | None = None):
        self.fill_model = fill_model or FillModel()
        self._ids = count(1)

    @abstractmethod
    def reference_price(self, symbol: str) -> float:
        """The current price to fill against (bar close, or live LTP)."""

    def login(self) -> Session:  # no auth needed for simulation
        return Session(access_token="sim")

    def execute(self, order: BrokerOrder) -> Fill:
        ref = self.reference_price(order.symbol)
        price = self.fill_model.fill_price(ref, order.side)
        commission = self.fill_model.commission(price, order.quantity)
        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=price,
            commission=commission,
            broker_order_id=f"sim-{next(self._ids)}",
        )


class BacktestBroker(SimBroker):
    """Fills against the current historical bar close (BACKTEST)."""

    def __init__(self, price_fn: Callable[[str], float], fill_model: FillModel | None = None):
        super().__init__(fill_model)
        self._price_fn = price_fn

    def reference_price(self, symbol: str) -> float:
        return self._price_fn(symbol)

    def funds(self) -> Funds:  # cash is tracked by the Portfolio in sim
        return Funds(available=0.0)


class PaperBroker(SimBroker):
    """Fills against the latest live quote (forward-test).

    Phase 4 wires ``price_fn`` to the live feed's last tick. The fill math is shared
    with BacktestBroker above.
    """

    def __init__(self, price_fn: Callable[[str], float], fill_model: FillModel | None = None):
        super().__init__(fill_model)
        self._price_fn = price_fn

    def reference_price(self, symbol: str) -> float:
        return self._price_fn(symbol)

    def funds(self) -> Funds:
        return Funds(available=0.0)
