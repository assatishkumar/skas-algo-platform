"""Strategy implementations and the Strategy interface.

Strategies are written once and run unchanged in BACKTEST, PAPER, and LIVE modes.
SST / SST-LIFO (ported from skas-trading) land in Phase 1.
"""

from .base import Strategy

__all__ = ["Strategy"]
