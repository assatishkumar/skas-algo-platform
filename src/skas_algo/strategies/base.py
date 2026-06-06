"""The Strategy interface (see docs/PLAN.md → Strategy & Override interfaces)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal


@runtime_checkable
class Strategy(Protocol):
    """A trading strategy. Emits Signals; never talks to a broker directly.

    The same instance is driven by the engine in any mode — only the data feed,
    clock, and broker adapter differ.

    For daily portfolio strategies (SST, SHOP, ...) the engine calls ``on_slice``
    once per timestamp with the set of symbols printing then; the strategy returns
    an ordered list of Signals (typically exits first, then entries).
    """

    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        """Decide actions for the current market slice (ctx.present_symbols())."""
        ...
