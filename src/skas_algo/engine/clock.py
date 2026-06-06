"""Clock abstraction — one of the three pieces that swap by mode.

BACKTEST uses a SimulatedClock (jumps bar->bar, runs as fast as the CPU allows);
PAPER and LIVE use a RealClock (wall-clock, gated to market hours).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Supplies 'now' to the engine and strategies."""

    def now(self) -> datetime: ...


class RealClock:
    """Wall-clock time (PAPER / LIVE)."""

    def now(self) -> datetime:
        return datetime.now()


class SimulatedClock:
    """Backtest clock — time is advanced explicitly by the replay loop."""

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        self._now = ts
