"""The Strategy interface (see docs/PLAN.md → Strategy & Override interfaces)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from skas_algo.engine.types import AlgoContext, Bar, Signal, Tick


@runtime_checkable
class Strategy(Protocol):
    """A trading strategy. Emits Signals; never talks to a broker directly.

    The same instance is driven by the engine in any mode — only the data feed,
    clock, and broker adapter differ.
    """

    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return the strategy's starting state from its parameters."""
        ...

    def on_bar(self, ctx: AlgoContext, bar: Bar) -> list[Signal]:
        """Called once per completed bar."""
        ...

    def on_tick(self, ctx: AlgoContext, tick: Tick) -> list[Signal]:
        """Called on each live tick (no-op for daily strategies)."""
        ...
