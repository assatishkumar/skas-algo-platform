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
    EXIT = "EXIT"  # close a specific lot (lot_id) or reduce by quantity
    EXIT_ALL = "EXIT_ALL"  # close the whole position (all lots) as one transaction
    REDUCE = "REDUCE"


@dataclass
class Signal:
    """A strategy's intent, before override resolution and risk checks.

    Strategies never touch the broker directly — they emit Signals in the order
    they want them executed (e.g. all exits before entries). The engine resolves
    overrides, applies risk, and routes each through the broker.
    """

    symbol: str
    action: SignalAction
    quantity: int | None = None
    lot_id: int | None = None  # for EXIT of a specific lot
    qty_pct: float | None = None  # e.g. exit 50%
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
