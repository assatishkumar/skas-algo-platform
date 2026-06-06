"""Engine-managed protective stops (trailing / hard) attached to lots.

A lot under management is owned by the StopBook, not the strategy: the engine
evaluates the stop every bar and exits the lot when it triggers. This is how the
"trail the rest" half of an override works — after booking part of a position, the
remainder rides a trailing stop independent of the strategy's own logic.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class StopKind(str, enum.Enum):
    TRAILING = "TRAILING"
    HARD = "HARD"


@dataclass
class Stop:
    symbol: str
    lot_id: int
    kind: StopKind
    # TRAILING: trail as a fraction (0.02 = 2%); HARD: stop_price set, trail unused.
    trail: float = 0.0
    stop_price: float = 0.0
    hwm: float = 0.0  # high-water-mark of price since attached (trailing)
    reason: str = ""

    def update_and_check(self, price: float) -> bool:
        """Update the high-water-mark and return True if the stop triggers."""
        if self.kind is StopKind.TRAILING:
            if price > self.hwm:
                self.hwm = price
            return price <= self.hwm * (1 - self.trail)
        return price <= self.stop_price


class StopBook:
    """Tracks managed stops keyed by lot id."""

    def __init__(self) -> None:
        self._stops: dict[int, Stop] = {}

    def attach(self, stop: Stop) -> None:
        self._stops[stop.lot_id] = stop

    def remove(self, lot_id: int) -> None:
        self._stops.pop(lot_id, None)

    def managed_lot_ids(self) -> set[int]:
        return set(self._stops.keys())

    def is_managed(self, lot_id: int) -> bool:
        return lot_id in self._stops

    def evaluate(self, price_of: dict[str, float]) -> list[Stop]:
        """Return stops that trigger at today's prices (HWMs are updated in place).

        ``price_of`` maps symbol -> today's close; symbols absent today are skipped.
        """
        triggered: list[Stop] = []
        for stop in list(self._stops.values()):
            price = price_of.get(stop.symbol)
            if price is None:
                continue
            if stop.update_and_check(price):
                triggered.append(stop)
        return triggered
