"""Live/paper run control: start, manual refresh/decision, stop, list + WebSocket.

Async endpoints call the manager directly (loop thread) so broadcasts to WebSocket
subscribers are thread-safe. The "cache" quote source works offline; "zerodha" needs
a logged-in account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import (
    DeploymentUpdate,
    LiveStartRequest,
    OverrideInput,
    QuoteSourceInput,
)
from skas_algo.data import universes
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.db.enums import TradingMode
from skas_algo.db.models import Algo, AlgoRun, BrokerAccount, Fill, Order, Position
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.overrides import OverrideRule
from skas_algo.live.manager import LiveConfig, manager
from skas_algo.live.quotes import CacheQuoteSource, ZerodhaQuoteSource
from skas_algo.services import broker as broker_svc

router = APIRouter(tags=["live"], prefix="/live")


def _build_quote_source(quote_source: str, broker_account_id, loader: PriceLoader, db: Session):
    if quote_source == "cache":
        return CacheQuoteSource(loader)
    if quote_source == "zerodha":
        if broker_account_id is None:
            raise HTTPException(
                status_code=400, detail="broker_account_id required for zerodha quotes"
            )
        account = db.get(BrokerAccount, broker_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="broker account not found")
        if not broker_svc.has_valid_session(account):
            raise HTTPException(
                status_code=400,
                detail="broker account has no valid session — log in (paste request token) first",
            )
        return ZerodhaQuoteSource(broker_svc.make_adapter(account))
    raise HTTPException(status_code=400, detail=f"unknown quote_source '{quote_source}'")


def _quote_source(req: LiveStartRequest, loader: PriceLoader, db: Session):
    return _build_quote_source(req.quote_source, req.broker_account_id, loader, db)


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
            notes=req.notes,
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
            quote_source=req.quote_source,
            broker_account_id=req.broker_account_id if req.quote_source == "zerodha" else None,
            refresh_seconds=req.refresh_seconds,
            decision_time=req.decision_time,
            ignore_market_hours=req.ignore_market_hours,
            auto=req.auto,
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


def _deployment_status(run: AlgoRun) -> str:
    if run.archived:
        return "archived"
    return "active" if run.id in manager.runs else "stopped"


@router.get("/deployments")
async def list_deployments(status: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    """All paper/live deployments as tiles, optionally filtered by status."""
    rows = db.execute(
        select(AlgoRun, Algo)
        .join(Algo, AlgoRun.algo_id == Algo.id)
        .where(AlgoRun.mode != TradingMode.BACKTEST)
        .order_by(AlgoRun.id.desc())
    ).all()
    out: list[dict] = []
    for run, algo in rows:
        st = _deployment_status(run)
        if status and st != status:
            continue
        tile = {
            "run_id": run.id,
            "algo_id": algo.id,
            "name": algo.name,
            "notes": algo.notes,
            "strategy_id": algo.strategy_id,
            "mode": run.mode.value,
            "status": st,
            "quote_source": (run.params_snapshot or {}).get("quote_source", "cache"),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "stopped_at": run.stopped_at.isoformat() if run.stopped_at else None,
        }
        live = manager.get(run.id)
        if live is not None and st == "active":
            snap = live.snapshot()
            upnl = sum(p["unrealized_pnl"] for p in snap.get("positions", []))
            tile["metrics"] = {
                "equity": snap.get("equity"),
                "cash": snap.get("cash"),
                "invested": snap.get("invested", 0),
                "open_positions": snap.get("open_positions", 0),
                "open_lots": snap.get("open_lots", 0),
                "parts_total": snap.get("parts_total"),
                "unrealized_pnl": upnl,
            }
        else:
            m = (run.metrics or {}).get("metrics", {})
            tile["metrics"] = {
                "equity": m.get("Final Equity"),
                "total_return_pct": m.get("Total Return %"),
                "total_trades": m.get("Total Trades"),
                "open_positions": 0,
            }
        out.append(tile)
    return out


@router.get("/{run_id}")
async def get_live(run_id: int) -> dict:
    return _get(run_id).snapshot()


@router.get("/{run_id}/watchlist")
async def watchlist(run_id: int) -> dict:
    """Per-symbol signal status (price, 20-day levels, tracking, holding)."""
    from skas_algo.engine.jsonutil import to_native

    return {"run_id": run_id, "rows": to_native(_get(run_id).session.watchlist())}


@router.post("/{run_id}/quote-source")
async def set_quote_source(
    run_id: int,
    body: QuoteSourceInput,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
) -> dict:
    """Swap a running run's quote source (e.g. cache -> Zerodha live) in place."""
    live = _get(run_id)
    account_id = body.broker_account_id if body.quote_source == "zerodha" else None
    live.quote_source = _build_quote_source(body.quote_source, account_id, loader, db)
    live.config.quote_source = body.quote_source
    live.config.broker_account_id = account_id
    # Persist so a restart recovers with the new source.
    run = db.get(AlgoRun, run_id)
    if run is not None:
        params = dict(run.params_snapshot or {})
        params["quote_source"] = body.quote_source
        params["broker_account_id"] = account_id
        run.params_snapshot = params
    return live.snapshot()


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


def _get_run(db: Session, run_id: int) -> AlgoRun:
    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    return run


@router.post("/{run_id}/archive")
async def archive(run_id: int, db: Session = Depends(get_db)) -> dict:
    if run_id in manager.runs:
        manager.stop(run_id)  # finalize before hiding
    _get_run(db, run_id).archived = True
    return {"run_id": run_id, "status": "archived"}


@router.post("/{run_id}/unarchive")
async def unarchive(run_id: int, db: Session = Depends(get_db)) -> dict:
    _get_run(db, run_id).archived = False
    return {"run_id": run_id, "status": "stopped"}


@router.patch("/{run_id}")
async def update_deployment(
    run_id: int, body: DeploymentUpdate, db: Session = Depends(get_db)
) -> dict:
    run = _get_run(db, run_id)
    algo = db.get(Algo, run.algo_id)
    if body.name is not None:
        algo.name = body.name
    if body.notes is not None:
        algo.notes = body.notes
    live = manager.get(run_id)
    if live is not None:  # keep the in-memory config in sync
        if body.name is not None:
            live.config.name = body.name
        if body.notes is not None:
            live.config.notes = body.notes
    return {"run_id": run_id, "name": algo.name, "notes": algo.notes}


@router.delete("/{run_id}")
async def delete_deployment(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Permanently remove a deployment: its run, orders, fills, positions, and Algo."""
    if run_id in manager.runs:
        manager.stop(run_id)
    run = _get_run(db, run_id)
    algo_id = run.algo_id
    order_ids = db.execute(select(Order.id).where(Order.algo_id == algo_id)).scalars().all()
    if order_ids:
        db.execute(sa_delete(Fill).where(Fill.order_id.in_(order_ids)))
    db.execute(sa_delete(Order).where(Order.algo_id == algo_id))
    db.execute(sa_delete(Position).where(Position.algo_id == algo_id))
    db.execute(sa_delete(AlgoRun).where(AlgoRun.algo_id == algo_id))
    db.execute(sa_delete(Algo).where(Algo.id == algo_id))
    return {"deleted": run_id}


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
