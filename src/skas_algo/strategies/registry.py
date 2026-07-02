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
from .custom_equity import CustomEquityStrategy
from .custom_options import CustomOptionsStrategy
from .donchian_strangle_bt import DonchianStrangleBtStrategy
from .donchian_strangle_monthly import DonchianStrangleMonthlyStrategy
from .hni_weekly import HniWeeklyStrategy
from .nifty_shop import NiftyShopStrategy
from .short_premium import ShortPremiumStrategy
from .sst_fifo import SSTFifoStrategy
from .sst_lifo import SSTLifoStrategy
from .sst_weekly import SSTWeeklyFifoStrategy, SSTWeeklyStrategy
from .supertrend_momentum import SuperTrendMomentumStrategy
from .staggered_covered_call import StaggeredCoveredCallStrategy

# strategy_id -> factory(universe, **params) -> strategy instance
_REGISTRY: dict[str, Callable[..., Any]] = {
    SSTLifoStrategy.strategy_id: SSTLifoStrategy,
    SSTFifoStrategy.strategy_id: SSTFifoStrategy,
    SSTWeeklyStrategy.strategy_id: SSTWeeklyStrategy,
    SSTWeeklyFifoStrategy.strategy_id: SSTWeeklyFifoStrategy,
    SuperTrendMomentumStrategy.strategy_id: SuperTrendMomentumStrategy,
    NiftyShopStrategy.strategy_id: NiftyShopStrategy,
    ShortPremiumStrategy.strategy_id: ShortPremiumStrategy,
    CallRatioMonthlyStrategy.strategy_id: CallRatioMonthlyStrategy,
    PutRatioMonthlyStrategy.strategy_id: PutRatioMonthlyStrategy,
    BatmanRatioMonthlyStrategy.strategy_id: BatmanRatioMonthlyStrategy,
    HniWeeklyStrategy.strategy_id: HniWeeklyStrategy,
    StaggeredCoveredCallStrategy.strategy_id: StaggeredCoveredCallStrategy,
    CustomOptionsStrategy.strategy_id: CustomOptionsStrategy,
    CustomEquityStrategy.strategy_id: CustomEquityStrategy,
    DonchianStrangleMonthlyStrategy.strategy_id: DonchianStrangleMonthlyStrategy,
    DonchianStrangleBtStrategy.strategy_id: DonchianStrangleBtStrategy,
}


def get_strategy(strategy_id: str) -> Callable[..., Any]:
    if strategy_id not in _REGISTRY:
        raise KeyError(f"Unknown strategy '{strategy_id}'. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[strategy_id]


def register(strategy_id: str, factory: Callable[..., Any]) -> None:
    _REGISTRY[strategy_id] = factory


def available() -> list[str]:
    return sorted(_REGISTRY)
