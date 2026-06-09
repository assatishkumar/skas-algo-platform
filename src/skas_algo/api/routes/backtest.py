"""Strategy + backtest + reports endpoints."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import (
    BacktestRequest,
    BacktestResponse,
    DeploymentUpdate,
    RunSummary,
    UniverseOut,
)
from skas_algo.data import universes
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.db.enums import TradingMode
from skas_algo.db.models import Algo, AlgoRun
from skas_algo.engine.market import PriceLoader
from skas_algo.services.backtest import run_backtest
from skas_algo.services.runs import delete_algo_cascade
from skas_algo.strategies.registry import available

router = APIRouter(tags=["backtest"])

_TRADE_COLUMNS = [
    "date",
    "ticker",
    "action",
    "units",
    "price",
    "amount",
    "profit",
    "pnl_pct",
    "lots",
    "tag",
]


@router.get("/strategies")
def list_strategies() -> dict:
    return {"strategies": available()}


@router.get("/universes", response_model=list[UniverseOut])
def list_universes(
    avail: set[str] = Depends(get_available_symbols),
) -> list[UniverseOut]:
    return [
        UniverseOut(
            name=name, label=universes.label(name), count=len(universes.resolve(name, avail))
        )
        for name in universes.UNIVERSES
    ]


@router.post("/backtest", response_model=BacktestResponse)
def post_backtest(
    req: BacktestRequest,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> BacktestResponse:
    # A named universe expands to its cached symbols; otherwise use explicit symbols.
    if req.universe:
        try:
            req.symbols = universes.resolve(req.universe, avail)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not req.symbols:
        raise HTTPException(status_code=422, detail="symbols or a valid universe required")
    try:
        result = run_backtest(db, loader, req)
    except KeyError as exc:  # unknown strategy_id
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BacktestResponse(**result)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(status: str | None = None, db: Session = Depends(get_db)) -> list[RunSummary]:
    """Backtest runs as summaries. ``status`` filters active (default) vs archived."""
    rows = db.execute(
        select(AlgoRun, Algo)
        .join(Algo, AlgoRun.algo_id == Algo.id)
        .where(AlgoRun.mode == TradingMode.BACKTEST)
        .order_by(AlgoRun.id.desc())
    ).all()
    out: list[RunSummary] = []
    for run, algo in rows:
        st = "archived" if run.archived else "active"
        if status and st != status:
            continue
        out.append(
            RunSummary(
                run_id=run.id,
                algo_id=algo.id,
                name=algo.name,
                notes=algo.notes,
                strategy_id=algo.strategy_id,
                mode=run.mode.value,
                archived=run.archived,
                started_at=run.started_at.isoformat() if run.started_at else None,
                metrics=run.metrics.get("metrics", {}) if run.metrics else {},
            )
        )
    return out


def _get_run(db: Session, run_id: int) -> AlgoRun:
    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.patch("/runs/{run_id}")
def update_run(run_id: int, body: DeploymentUpdate, db: Session = Depends(get_db)) -> dict:
    run = _get_run(db, run_id)
    algo = db.get(Algo, run.algo_id)
    if body.name is not None:
        algo.name = body.name
    if body.notes is not None:
        algo.notes = body.notes
    return {"run_id": run_id, "name": algo.name, "notes": algo.notes}


@router.post("/runs/{run_id}/archive")
def archive_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    _get_run(db, run_id).archived = True
    return {"run_id": run_id, "archived": True}


@router.post("/runs/{run_id}/unarchive")
def unarchive_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    _get_run(db, run_id).archived = False
    return {"run_id": run_id, "archived": False}


@router.delete("/runs/{run_id}")
def delete_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Permanently remove a run: its AlgoRun, the Algo, and any orders/fills/positions."""
    run = _get_run(db, run_id)
    delete_algo_cascade(db, run.algo_id)
    return {"deleted": run_id}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    algo = db.get(Algo, run.algo_id)
    return {
        "run_id": run.id,
        "algo_id": run.algo_id,
        "strategy_id": algo.strategy_id if algo else None,
        "name": algo.name if algo else None,
        "notes": algo.notes if algo else None,
        "archived": run.archived,
        "capital": algo.capital if algo else None,
        "params": algo.params if algo else {},  # symbols, lookback, tax, sizing, etc.
        "mode": run.mode.value,
        "report": run.metrics,
        "trades": run.trade_log or [],
    }


@router.get("/runs/{run_id}/trades.csv")
def get_run_trades_csv(run_id: int, db: Session = Depends(get_db)) -> Response:
    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_TRADE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in run.trade_log or []:
        writer.writerow(row)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}_trades.csv"'},
    )
