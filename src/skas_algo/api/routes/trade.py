"""Trade — deploy ad-hoc, user-built positions (custom option structures + managed equity trades).

Thin endpoints that validate the structured Trade-UI input, translate it into a LiveStartRequest,
and reuse the exact same deployment path as POST /live/start (``start_deployment``). "Save" deploys
immediately; the result is a normal paper/live deployment that shows on the Live page with the usual
tiles/metrics/reports. Real-money LIVE stays gated behind ``armed`` + ``SKAS_LIVE_TRADING_ENABLED``.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import EquityTradeDeploy, LiveStartRequest, OptionTradeLeg, OptionsTradeDeploy
from skas_algo.api.routes.data import _live_adapter
from skas_algo.api.routes.live import start_deployment
from skas_algo.data.options_provider import make_spot_provider
from skas_algo.data.provider import get_available_symbols, get_data_cache, get_price_loader
from skas_algo.db.models import BrokerAccount
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.services import broker as broker_svc
from skas_algo.services.fibret import FibParams, analyze_symbol

router = APIRouter(prefix="/trade", tags=["trade"])


class OptionMarginRequest(BaseModel):
    underlying: str
    expiry: str
    lot_size: int = 0
    legs: list[OptionTradeLeg]
    broker_account_id: int | None = None


def _per_lot(underlying: str, expiry: date, lot_size: int) -> int:
    if lot_size:
        return lot_size
    try:
        return lot_size_for(underlying, expiry)
    except KeyError:
        return 0


@router.post("/options/margin")
def option_trade_margin(
    body: OptionMarginRequest,
    db: Session = Depends(get_db),
    cache=Depends(get_data_cache),
) -> dict:
    """Margin the basket would block. Prefers Zerodha's real basket margin (live session,
    with spread benefit); falls back to a SPAN+exposure model estimate on the short legs."""
    exp = date.fromisoformat(body.expiry[:10])
    per_lot = _per_lot(body.underlying.upper(), exp, body.lot_size)
    if per_lot <= 0 or not body.legs:
        return {"margin": None, "source": None}
    sized = [
        {
            "symbol": make(body.underlying.upper(), exp, float(leg.strike), leg.right.upper(),
                           lot_size=per_lot).symbol,
            "direction": -1 if leg.side.lower() == "sell" else 1,
            "units": int(leg.lots) * per_lot,
            "right": leg.right.upper(), "strike": float(leg.strike),
        }
        for leg in body.legs
    ]
    # Live basket margin (accurate, spread-netted) when a session is available.
    if body.broker_account_id is not None:
        account = db.get(BrokerAccount, body.broker_account_id)
        if account is not None and broker_svc.has_valid_session(account):
            try:
                m = broker_svc.make_adapter(account).basket_margin(sized)
            except Exception:  # pragma: no cover - API hiccup → fall through to model
                m = None
            if m is not None:
                return {"margin": float(m), "source": "zerodha"}
    # Model estimate: SPAN+exposure on each short leg (longs are debit-paid, no margin).
    spot = make_spot_provider(cache)(body.underlying.upper(), date.today())
    if spot is None:
        return {"margin": None, "source": None}
    p = MarginParams()
    total = sum(short_option_margin(spot, leg["units"], 1, p) for leg in sized if leg["direction"] < 0)
    return {"margin": float(total), "source": "model"}


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


class FibRetRequest(BaseModel):
    broker_account_id: int
    symbols: list[str]
    expiry: str | None = None        # ISO; default = nearest listed expiry with DTE ≥ min_dte
    swing_lookback: int = 20         # trading days for the swing high/low (recent swing)
    entry_fib: float = 1.618         # short-strike extension level
    stop_fib: float = 0.786          # spot stop level
    target_pct: float = 90.0         # whole percent (UI) → fraction at deploy
    min_oi: int = 0                  # liquidity floor for the ⚑ flag
    lots: int = 1
    min_dte: int = 7


def _pick_expiry(adapter, symbol: str, requested: str | None, on_date: date, min_dte: int) -> date | None:
    if requested:
        return date.fromisoformat(requested[:10])
    try:
        exps = adapter.option_expiries(symbol) or []
    except Exception:  # pragma: no cover - network hiccup
        return None
    parsed = sorted({date.fromisoformat(str(e)[:10]) for e in exps})
    future = [e for e in parsed if (e - on_date).days >= min_dte]
    return future[0] if future else (parsed[-1] if parsed else None)


@router.post("/options/fibret/analyze")
def fibret_analyze(
    body: FibRetRequest,
    db: Session = Depends(get_db),
    cache=Depends(get_data_cache),
) -> dict:
    """Screen a watchlist for the FibRet setup using LIVE chains (needs a broker session). One row
    per symbol: suggested short leg, fib levels, live premium/OI/liquidity, R:R and margin. Errors
    are reported per-row (illiquid strike, no swing, no chain) so the table shows what failed."""
    adapter = _live_adapter(body.broker_account_id, db)  # 4xx if no valid session
    on_date = date.today()
    params = FibParams(
        swing_lookback=body.swing_lookback, entry_fib=body.entry_fib, stop_fib=body.stop_fib,
        target_pct=(body.target_pct / 100.0), lots=max(1, body.lots), min_oi=body.min_oi,
    )
    rows: list[dict] = []
    for raw in body.symbols:
        sym = raw.strip().upper()
        if not sym:
            continue
        try:
            df = cache.get_prices(sym, start_date=on_date - timedelta(days=400), end_date=on_date)
        except Exception:
            df = None
        if df is None or len(df) == 0:
            rows.append({"symbol": sym, "error": "no cached price history"})
            continue
        exp = _pick_expiry(adapter, sym, body.expiry, on_date, body.min_dte)
        if exp is None:
            rows.append({"symbol": sym, "error": "no listed option expiries"})
            continue
        try:
            chain = adapter.live_option_chain(sym, exp.isoformat())
        except Exception as exc:  # pragma: no cover - network hiccup
            rows.append({"symbol": sym, "error": f"live chain failed: {exc}"})
            continue
        if not chain:
            rows.append({"symbol": sym, "error": f"no listed options for {exp.isoformat()}"})
            continue
        rows.append(analyze_symbol(symbol=sym, df=df, chain=chain, expiry=exp, on_date=on_date, params=params))
    return {
        "as_of": on_date.isoformat(),
        "target_pct": body.target_pct,
        "entry_fib": body.entry_fib,
        "stop_fib": body.stop_fib,
        "rows": rows,
    }


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
