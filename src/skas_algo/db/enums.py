"""Enumerations shared across domain models and the engine."""

from __future__ import annotations

import enum


class TradingMode(str, enum.Enum):
    """The three execution modes that share one engine."""

    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class InstrumentClass(str, enum.Enum):
    STOCK = "STOCK"
    DERIV = "DERIV"


class AlgoStatus(str, enum.Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class PositionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL_M"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OverrideScope(str, enum.Enum):
    ALGO = "ALGO"
    SYMBOL = "SYMBOL"
    POSITION = "POSITION"


class OverrideSource(str, enum.Enum):
    CONFIG = "CONFIG"
    LIVE = "LIVE"


class AlertChannel(str, enum.Enum):
    PUSH = "PUSH"
    TELEGRAM = "TELEGRAM"
    EMAIL = "EMAIL"
    IN_APP = "IN_APP"
