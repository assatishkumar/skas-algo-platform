"""Trade — deploy ad-hoc, user-built positions (custom option structures + managed equity trades).

Thin endpoints that validate the structured Trade-UI input, translate it into a LiveStartRequest,
and reuse the exact same deployment path as POST /live/start (``start_deployment``). "Save" deploys
immediately; the result is a normal paper/live deployment that shows on the Live page with the usual
tiles/metrics/reports. Real-money LIVE stays gated behind ``armed`` + ``SKAS_LIVE_TRADING_ENABLED``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import EquityTradeDeploy, LiveStartRequest, OptionsTradeDeploy
from skas_algo.api.routes.live import start_deployment
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.engine.market import PriceLoader

router = APIRouter(prefix="/trade", tags=["trade"])


def _frac(v: float | None) -> float | None:
    """A whole-number percent from the UI → a fraction the strategy uses (50 → 0.5)."""
    return None if v is None else v / 100.0


@router.post("/options/deploy")
async def deploy_option_trade(
    body: OptionsTradeDeploy,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    if not body.legs:
        raise HTTPException(status_code=422, detail="at least one leg is required")
    params = {
        "expiry": body.expiry,
        "legs": [leg.model_dump() for leg in body.legs],
        "lot_size": body.lot_size,
        "spot_upper": body.spot_upper,
        "spot_lower": body.spot_lower,
        "target_pct": _frac(body.target_pct),
        "stop_pct": _frac(body.stop_pct),
        "leg_targets": {int(k): _frac(v) for k, v in (body.leg_targets or {}).items()},
        "leg_stops": {int(k): _frac(v) for k, v in (body.leg_stops or {}).items()},
    }
    req = LiveStartRequest(
        strategy_id="custom_options",
        name=body.name,
        notes=body.notes,
        instrument_class="DERIV",
        underlying=body.underlying.upper(),
        capital=body.capital,
        params=params,
        mode=body.mode,
        quote_source=body.quote_source,
        broker_account_id=body.broker_account_id,
        ignore_market_hours=body.ignore_market_hours,
        auto=body.auto,
    )
    return start_deployment(req, db, loader, avail).snapshot()


@router.post("/equity/deploy")
async def deploy_equity_trade(
    body: EquityTradeDeploy,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    if not body.symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    params = {
        "symbol": body.symbol.upper(),
        "qty": body.qty,
        "entry_mode": body.entry_mode,
        "trigger_price": body.trigger_price,
        "target_pct": _frac(body.target_pct),
        "stop_pct": _frac(body.stop_pct),
        "trailing": body.trailing,
        "trail_pct": _frac(body.trail_pct),
    }
    req = LiveStartRequest(
        strategy_id="custom_equity",
        name=body.name,
        notes=body.notes,
        instrument_class="STOCK",
        symbols=[body.symbol.upper()],
        capital=body.capital,
        params=params,
        mode=body.mode,
        quote_source=body.quote_source,
        broker_account_id=body.broker_account_id,
        ignore_market_hours=body.ignore_market_hours,
        auto=body.auto,
    )
    return start_deployment(req, db, loader, avail).snapshot()
