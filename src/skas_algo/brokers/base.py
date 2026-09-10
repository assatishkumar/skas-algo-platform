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


class LimitNotMarketable(ValueError):
    """A caller's LIMIT would not trade against the current touch. Raised BEFORE anything is
    placed (the session's pre-check, and the paper broker as a backstop) — a ValueError,
    never an order failure, so a refused ticket does not halt the run."""

    def __init__(self, symbol: str, side: str, limit: float, touch: float) -> None:
        self.symbol, self.side, self.limit, self.touch = symbol, side, limit, touch
        super().__init__(f"{side} {symbol}: limit ₹{limit:.2f} is not marketable "
                         f"(touch ₹{touch:.2f})")


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
    # True when this order REDUCES an existing position (an exit/cover) rather than opening
    # one. Only LiveBroker reads it, to decide how hard to chase a fill: being flat matters
    # far more on the way out than the last rupee of price does. PaperBroker and the
    # backtest ignore it entirely, so the shared path stays byte-identical.
    reduce_only: bool = False


@dataclass
class Fill:
    """The result of an executed order."""

    symbol: str
    side: OrderSide
    quantity: int
    price: float
    commission: float = 0.0
    broker_order_id: str | None = None


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
