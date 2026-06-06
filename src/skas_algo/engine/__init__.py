"""The mode-agnostic algo engine.

The engine wires together three swappable components — a Clock, a DataFeed, and a
BrokerAdapter — and runs the same Strategy code in BACKTEST, PAPER, or LIVE mode.
See docs/PLAN.md → "Execution modes — one engine".

Phase 1 will implement the run loop here. Phase 0 defines the seams.
"""

from .types import AlgoContext, Bar, Signal, SignalAction, Tick

__all__ = ["AlgoContext", "Bar", "Tick", "Signal", "SignalAction"]
