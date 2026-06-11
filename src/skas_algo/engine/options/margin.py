"""Simplified margin model for short index options.

Real exchange margin (SPAN + exposure) is portfolio/path dependent; this is a
defensible flat-percentage approximation good enough for sizing a short-premium
strategy and reporting capital efficiency. Constants are overridable via
``BacktestRequest.params["margin"]``. Margin is *blocked*, not spent — cash is not
reduced; we track ``margin_used`` (and its high-water mark) for the report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from .instrument import parse

SpotProvider = Callable[[str, date], "float | None"]


@dataclass(frozen=True)
class MarginParams:
    span_pct: float = 0.10       # SPAN proxy: % of notional
    exposure_pct: float = 0.03   # exposure proxy: % of notional

    @classmethod
    def from_dict(cls, d: dict | None) -> "MarginParams":
        if not d:
            return cls()
        return cls(span_pct=float(d.get("span_pct", 0.10)),
                   exposure_pct=float(d.get("exposure_pct", 0.03)))


def short_option_margin(spot: float, units: int, multiplier: int, p: MarginParams) -> float:
    """Approx margin to short ``units`` (= lots·lot_size) of one option contract."""
    notional = spot * units * multiplier
    return (p.span_pct + p.exposure_pct) * notional


class MarginModel:
    """Computes margin used by the open short option book and tracks a high-water mark."""

    def __init__(self, spot_provider: SpotProvider, params: MarginParams | None = None,
                 lot_overrides: dict | None = None):
        self.spot_provider = spot_provider
        self.params = params or MarginParams()
        self._lot_overrides = lot_overrides
        self.max_margin_used = 0.0

    def margin_used(self, portfolio, on_date: date) -> float:
        """Total approximate margin blocked by open short option lots today."""
        total = 0.0
        for symbol in portfolio.lot_symbols():
            inst = parse(symbol, lot_overrides=self._lot_overrides)
            if inst is None:
                continue
            spot = self.spot_provider(inst.underlying, on_date)
            if spot is None:
                continue
            for lot in portfolio.lots(symbol):
                if lot.direction == -1:
                    total += short_option_margin(spot, lot.units, lot.multiplier, self.params)
        self.max_margin_used = max(self.max_margin_used, total)
        return total

    def lots_affordable(self, spot: float, lot_size: int, multiplier: int,
                        capital: float, utilization: float,
                        margin_already_used: float = 0.0) -> int:
        """How many whole lots can be shorted within ``capital*utilization`` of margin."""
        per_lot = short_option_margin(spot, lot_size, multiplier, self.params)
        if per_lot <= 0:
            return 0
        budget = max(0.0, capital * utilization - margin_already_used)
        return int(budget // per_lot)
