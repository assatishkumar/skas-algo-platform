"""Live/paper run control: start, manual refresh/decision, stop, list + WebSocket.

Async endpoints call the manager directly (loop thread) so broadcasts to WebSocket
subscribers are thread-safe. The "cache" quote source works offline; "zerodha" needs
a logged-in account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import LiveStartRequest, OverrideInput
from skas_algo.data import universes
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.db.models import BrokerAccount
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.overrides import OverrideRule
from skas_algo.live.manager import LiveConfig, manager
from skas_algo.live.quotes import CacheQuoteSource, ZerodhaQuoteSource
from skas_algo.services import broker as broker_svc

router = APIRouter(tags=["live"], prefix="/live")


def _quote_source(req: LiveStartRequest, loader: PriceLoader, db: Session):
    if req.quote_source == "cache":
        return CacheQuoteSource(loader)
    if req.quote_source == "zerodha":
        if req.broker_account_id is None:
            raise HTTPException(
                status_code=400, detail="broker_account_id required for zerodha quotes"
            )
        account = db.get(BrokerAccount, req.broker_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="broker account not found")
        adapter = broker_svc.make_adapter(account)
        try:
            adapter.login()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"broker login failed: {exc}") from exc
        return ZerodhaQuoteSource(adapter)
    raise HTTPException(status_code=400, detail=f"unknown quote_source '{req.quote_source}'")


@router.post("/start")
async def start_live(
    req: LiveStartRequest,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    symbols = universes.resolve(req.universe, avail) if req.universe else list(req.symbols)
    if not symbols:
        raise HTTPException(status_code=422, detail="symbols or a valid universe required")
    try:
        quote_source = _quote_source(req, loader, db)
        config = LiveConfig(
            name=req.name or f"{req.strategy_id} {req.mode.lower()}",
            strategy_id=req.strategy_id,
            symbols=symbols,
            capital=req.capital,
            params=req.params,
            tax_rate=req.tax_rate,
            withdrawal_rate=req.withdrawal_rate,
            lookback=req.lookback,
            overrides=[
                OverrideRule(scope=o.scope, target=o.target, rule=o.rule) for o in req.overrides
            ],
            mode=req.mode,
            refresh_seconds=req.refresh_seconds,
            decision_time=req.decision_time,
            ignore_market_hours=req.ignore_market_hours,
        )
        live = manager.start(config, loader, quote_source)
    except KeyError as exc:  # unknown strategy
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if req.auto:
        manager.start_loop(live.run_id)
    return live.snapshot()


def _get(run_id: int):
    live = manager.get(run_id)
    if live is None:
        raise HTTPException(status_code=404, detail="live run not found")
    return live


@router.get("")
async def list_live() -> list[dict]:
    return [live.snapshot() for live in manager.list()]


@router.get("/{run_id}")
async def get_live(run_id: int) -> dict:
    return _get(run_id).snapshot()


@router.post("/{run_id}/refresh")
async def refresh_live(run_id: int) -> dict:
    live = _get(run_id)
    live.refresh()
    return live.snapshot()


@router.post("/{run_id}/run-decision")
async def run_decision(run_id: int) -> dict:
    live = _get(run_id)
    events = live.run_decision()
    return {
        "run_id": run_id,
        "trades": [
            {
                "ticker": e["ticker"],
                "action": e["action"],
                "units": e["units"],
                "price": e["price"],
                "tag": e["tag"],
            }
            for e in events
        ],
    }


@router.post("/{run_id}/overrides")
async def add_override(run_id: int, override: OverrideInput) -> dict:
    """Live intervention: inject an override rule into the running session.

    The resolver reads its mutable rule list on each decision, so this takes effect
    on the run's next decision (e.g. 'book 50% at 6%, trail the rest').
    """
    live = _get(run_id)
    live.session.resolver.overrides.append(
        OverrideRule(scope=override.scope, target=override.target, rule=override.rule)
    )
    return {"run_id": run_id, "overrides": len(live.session.resolver.overrides)}


@router.post("/{run_id}/stop")
async def stop_live(run_id: int) -> dict:
    _get(run_id)
    manager.stop(run_id)
    return {"stopped": run_id}


@router.websocket("/ws")
async def live_ws(ws: WebSocket) -> None:
    await ws.accept()
    queue = manager.broadcaster.subscribe()
    try:
        while True:
            message = await queue.get()
            await ws.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        manager.broadcaster.unsubscribe(queue)
