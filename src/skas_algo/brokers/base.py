"""The BrokerAdapter interface — the third piece that swaps by mode.

skas-data abstracts *market data* only; order execution is net-new here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from skas_algo.db.enums import OrderSide, OrderType
from skas_algo.engine.types import Tick


@dataclass
class Session:
    """An authenticated broker session."""

    access_token: str
    expires_at: datetime | None = None


@dataclass
class Funds:
    available: float
    used: float = 0.0


@dataclass
class BrokerOrder:
    """An order request handed to a broker adapter."""

    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    client_order_id: str | None = None
    tag: str | None = None


@runtime_checkable
class BrokerAdapter(Protocol):
    """Uniform interface across BacktestBroker, PaperBroker, and LiveBroker."""

    def login(self) -> Session:
        """Authenticate (TOTP-automated for live). No-op for sim brokers."""
        ...

    def place_order(self, order: BrokerOrder) -> str:
        """Submit an order; return the broker order id."""
        ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def positions(self) -> list[dict]: ...

    def funds(self) -> Funds: ...

    def subscribe_ticks(self, symbols: list[str], callback: Callable[[Tick], None]) -> None: ...
