"""DataFeed abstraction — one of the three pieces that swap by mode.

BACKTEST uses HistoricalReplayFeed (replays cached OHLC from skas-data);
PAPER and LIVE use LiveFeed (real-time quotes + KiteTicker). Both yield the same
Bar/Tick types, so the engine and strategies are identical across modes.

Phase 0 defines the interface; implementations land in Phase 1.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from .types import Bar, Tick


@runtime_checkable
class DataFeed(Protocol):
    """Yields market events in time order."""

    def bars(self) -> Iterator[Bar]: ...

    def ticks(self) -> Iterator[Tick]: ...
