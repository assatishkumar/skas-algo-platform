"""Core value types passed through the engine event flow.

Event flow (identical in all three modes):
    DataFeed -> Strategy -> Signal -> override resolver -> risk -> BrokerAdapter -> Fill
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Bar:
    """One OHLC bar for a symbol at a point in time."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Tick:
    """A single live price update."""

    symbol: str
    ts: datetime
    ltp: float


class SignalAction(str, enum.Enum):
    ENTER_LONG = "ENTER_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT = "EXIT"
    REDUCE = "REDUCE"


@dataclass
class Signal:
    """A strategy's intent, before override resolution and risk checks."""

    symbol: str
    action: SignalAction
    quantity: int | None = None
    qty_pct: float | None = None  # e.g. exit 50%
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class AlgoContext:
    """Runtime context handed to a strategy on each callback.

    Phase 1 will flesh this out: access to positions, funds, the data layer
    (skas-data), the clock, and the override resolver. Phase 0 is a placeholder
    so strategy signatures can be written against a stable type.
    """

    def __init__(self, algo_id: int, params: dict[str, Any]):
        self.algo_id = algo_id
        self.params = params

    def get_position(self, symbol: str) -> Any:  # noqa: ANN401 - filled in Phase 1
        raise NotImplementedError("Position access is implemented in Phase 1")
