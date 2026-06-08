"""Strategy implementations and the Strategy interface.

Strategies are written once and run unchanged in BACKTEST, PAPER, and LIVE modes.
"""

from .base import Strategy
from .registry import available, get_strategy, register
from .sst_fifo import SSTFifoStrategy
from .sst_lifo import SSTLifoStrategy

__all__ = [
    "Strategy",
    "SSTLifoStrategy",
    "SSTFifoStrategy",
    "get_strategy",
    "register",
    "available",
]
