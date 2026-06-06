"""The mode-agnostic algo engine.

The engine wires together three swappable components — a Clock, a DataFeed, and a
BrokerAdapter — and runs the same Strategy code in BACKTEST, PAPER, or LIVE mode.
See docs/PLAN.md → "Execution modes — one engine".
"""

from .context import AlgoContext
from .portfolio import Lot, Portfolio
from .types import Bar, Signal, SignalAction, Tick

__all__ = ["AlgoContext", "Bar", "Tick", "Signal", "SignalAction", "Portfolio", "Lot"]
