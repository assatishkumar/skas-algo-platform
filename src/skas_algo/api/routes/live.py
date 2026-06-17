"""Live/paper run control: start, manual refresh/decision, stop, list + WebSocket.

Async endpoints call the manager directly (loop thread) so broadcasts to WebSocket
subscribers are thread-safe. The "cache" quote source works offline; "zerodha" needs
a logged-in account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import (
    DeploymentUpdate,
    LiveControlsInput,
    LiveStartRequest,
    ManualOrderInput,
    OverrideInput,
    QuoteSourceInput,
    iso_utc,
)
from skas_algo.data import universes
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.db.enums import TradingMode
from skas_algo.db.models import Algo, AlgoRun, BrokerAccount, GreeksSnapshot, Order
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.overrides import OverrideRule
from skas_algo.live.manager import LiveConfig, manager
from skas_algo.live.quotes import CacheQuoteSource, ZerodhaQuoteSource
from skas_algo.services import broker as broker_svc
from skas_algo.services.runs import delete_algo_cascade

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
    is_deriv = req.instrument_class.upper() == "DERIV"
    underlying = (req.underlying or (req.symbols[0] if req.symbols else None))
    if is_deriv:
        if not underlying:
            raise HTTPException(status_code=422, detail="underlying required for a DERIV deployment")
        symbols = [underlying.upper()]
    else:
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
            instrument_class=req.instrument_class,
            underlying=underlying.upper() if is_deriv else None,
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
            warm_from_date=req.warm_from_date if is_deriv else None,
        )
        live = manager.start(config, loader, quote_source)
    except KeyError as exc:  # unknown strategy
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:  # bad warm_from_date / missing option-chain data to seed
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
            "instrument_class": (run.params_snapshot or {}).get("instrument_class"),
            "underlying": (run.params_snapshot or {}).get("underlying"),
            "started_at": iso_utc(run.started_at),
            "stopped_at": iso_utc(run.stopped_at),
        }
        # Broker connection: which account routes quotes/orders and whether its session
        # is currently valid. Lets the tile show a connected/disconnected indicator.
        account_id = (run.params_snapshot or {}).get("broker_account_id")
        tile["broker_account_id"] = account_id
        tile["broker_label"] = None
        tile["broker_connected"] = None
        if account_id is not None:
            account = db.get(BrokerAccount, account_id)
            if account is not None:
                tile["broker_label"] = account.label
                tile["broker_connected"] = broker_svc.has_valid_session(account)
        tile["on_cache_fallback"] = False
        live = manager.get(run.id)
        if live is not None and st == "active":
            snap = live.snapshot()
            tile["on_cache_fallback"] = snap.get("on_cache_fallback", False)
            upnl = sum(p["unrealized_pnl"] for p in snap.get("positions", []))
            tile["metrics"] = {
                "equity": snap.get("equity"),
                "cash": snap.get("cash"),
                "invested": snap.get("invested", 0),
                "open_positions": snap.get("open_positions", 0),
                "open_lots": snap.get("open_lots", 0),
                "parts_total": snap.get("parts_total"),
                "unrealized_pnl": upnl,
                # Options tiles surface margin + net credit/debit instead of equity value.
                "margin_used": snap.get("margin_used"),
                "margin_source": snap.get("margin_source"),
                "net_credit": snap.get("net_credit"),
                "net_delta": snap.get("net_delta"),
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


@router.post("/{run_id}/reconnect-quotes")
async def reconnect_quotes(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Promote a cache-fallback run back to live Zerodha quotes (needs a valid session)."""
    live = _get(run_id)
    if not manager.promote_quote_source(run_id, db):
        raise HTTPException(
            status_code=400,
            detail="cannot reconnect — run isn't on cache fallback or no valid session",
        )
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


@router.post("/{run_id}/controls")
async def set_controls(run_id: int, body: LiveControlsInput) -> dict:
    """Edit a running deployment's loop controls and exclusion list, in place.

    Any field left null is unchanged. ``excluded_symbols`` replaces the blocklist;
    excluded names get no new entries while open positions keep being managed.
    """
    _get(run_id)
    live = manager.update_controls(
        run_id,
        auto=body.auto,
        ignore_market_hours=body.ignore_market_hours,
        refresh_seconds=body.refresh_seconds,
        excluded_symbols=body.excluded_symbols,
        lots=body.lots,
    )
    return live.snapshot()


@router.get("/{run_id}/greeks-history")
async def greeks_history(run_id: int, limit: int = 1000, db: Session = Depends(get_db)) -> dict:
    """Sampled greeks time-series for an options deployment (net delta + IV + per-leg)."""
    rows = (
        db.execute(
            select(GreeksSnapshot)
            .where(GreeksSnapshot.algo_run_id == run_id)
            .order_by(GreeksSnapshot.ts.desc())
            .limit(max(1, min(limit, 5000)))
        )
        .scalars()
        .all()
    )
    points = [
        {
            "ts": iso_utc(r.ts),
            "spot": r.spot,
            "net_delta": r.net_delta,
            "net_iv": r.net_iv,
            "pnl": r.pnl,
            "legs": r.legs,
        }
        for r in reversed(rows)  # oldest → newest for charting
    ]
    return {"run_id": run_id, "points": points}


def _orders_to_trades(orders: list[Order]) -> list[dict]:
    """Reconstruct trades (entry legs + exits with per-leg P&L) from the persisted Order rows —
    the durable audit trail — so a closed cycle survives restarts even before it's finalized.
    FIFO match per symbol; profit is directional (short entry SELL → cover BUY)."""
    from collections import defaultdict, deque

    open_lots: dict[str, deque] = defaultdict(deque)  # symbol -> [units, price, side]
    out: list[dict] = []
    for o in orders:
        side = o.side.value if hasattr(o.side, "value") else str(o.side)
        sym, units, px = o.symbol, int(o.quantity), float(o.price or 0.0)
        d = o.created_at.date().isoformat() if o.created_at else None
        q = open_lots[sym]
        closing = q and q[0][2] != side  # opposite side of the open position → an exit
        if closing:
            rem, profit = units, 0.0
            while rem > 0 and q:
                lot = q[0]
                take = min(rem, lot[0])
                profit += (lot[1] - px) * take if lot[2] == "SELL" else (px - lot[1]) * take
                lot[0] -= take
                rem -= take
                if lot[0] == 0:
                    q.popleft()
            out.append({"date": d, "ticker": sym, "action": "COVER" if side == "BUY" else "SELL",
                        "units": units, "price": px, "profit": profit, "pnl_pct": 0.0,
                        "lots": 1, "tag": o.tag or ""})
            if rem > 0:
                q.append([rem, px, side])
        else:
            q.append([units, px, side])
            out.append({"date": d, "ticker": sym, "action": "SHORT" if side == "SELL" else "BUY",
                        "units": units, "price": px, "profit": 0.0, "pnl_pct": 0.0,
                        "lots": 1, "tag": o.tag or ""})
    return out


@router.get("/{run_id}/trades")
async def live_trades(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Executed trades for a deployment — entry legs + exits with per-leg P&L, holding days and
    exit reason — so a CLOSED cycle still shows what was traded, when it exited, and the booked
    P&L. Prefers the running session's in-memory transactions (richest: exit_reason/holding_days),
    then the persisted trade log, then a reconstruction from the durable Order rows."""
    from skas_algo.live.manager import _serialize_event

    live = manager.get(run_id)
    if live is not None and live.session.transactions:
        trades = [_serialize_event(t) for t in live.session.transactions]
    else:
        run = db.get(AlgoRun, run_id)
        trades = (run.trade_log if run is not None else None) or []
        if not trades and run is not None:
            orders = db.execute(
                select(Order).where(Order.algo_id == run.algo_id).order_by(Order.id)
            ).scalars().all()
            trades = _orders_to_trades(orders)
    return {"run_id": run_id, "trades": trades}


@router.post("/{run_id}/flatten")
async def flatten(run_id: int) -> dict:
    """Exit-all: close every open position now, at live prices. The strategy adopts the
    now-flat book (it won't try to manage legs that no longer exist)."""
    live = _get(run_id)
    events = live.flatten()
    return {"run_id": run_id, "closed": len(events), "snapshot": live.snapshot()}


@router.post("/{run_id}/manual-order")
async def manual_order(run_id: int, body: ManualOrderInput) -> dict:
    """Option-aware live intervention: close selected legs/lots and/or open new legs now.

    Executes immediately at live prices; afterwards the strategy adopts the resulting book.
    """
    live = _get(run_id)
    try:
        events = live.manual_order(
            closes=[c.model_dump() for c in body.closes],
            opens=[o.model_dump() for o in body.opens],
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"run_id": run_id, "executed": len(events), "snapshot": live.snapshot()}


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
    delete_algo_cascade(db, run.algo_id)
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
