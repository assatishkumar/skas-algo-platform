"""Strategy registry — new algos onboard by registering here, not by changing the engine.

(See docs/PLAN.md recommendation #8.)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .call_ratio_monthly import (
    BatmanRatioMonthlyStrategy,
    CallRatioMonthlyStrategy,
    PutRatioMonthlyStrategy,
)
from .hni_weekly import HniWeeklyStrategy
from .short_premium import ShortPremiumStrategy
from .sst_fifo import SSTFifoStrategy
from .sst_lifo import SSTLifoStrategy
from .staggered_covered_call import StaggeredCoveredCallStrategy

# strategy_id -> factory(universe, **params) -> strategy instance
_REGISTRY: dict[str, Callable[..., Any]] = {
    SSTLifoStrategy.strategy_id: SSTLifoStrategy,
    SSTFifoStrategy.strategy_id: SSTFifoStrategy,
    ShortPremiumStrategy.strategy_id: ShortPremiumStrategy,
    CallRatioMonthlyStrategy.strategy_id: CallRatioMonthlyStrategy,
    PutRatioMonthlyStrategy.strategy_id: PutRatioMonthlyStrategy,
    BatmanRatioMonthlyStrategy.strategy_id: BatmanRatioMonthlyStrategy,
    HniWeeklyStrategy.strategy_id: HniWeeklyStrategy,
    StaggeredCoveredCallStrategy.strategy_id: StaggeredCoveredCallStrategy,
}


def get_strategy(strategy_id: str) -> Callable[..., Any]:
    if strategy_id not in _REGISTRY:
        raise KeyError(f"Unknown strategy '{strategy_id}'. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[strategy_id]


def register(strategy_id: str, factory: Callable[..., Any]) -> None:
    _REGISTRY[strategy_id] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)
