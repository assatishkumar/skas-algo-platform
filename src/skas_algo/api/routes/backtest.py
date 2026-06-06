"""Strategy + backtest + reports endpoints."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import BacktestRequest, BacktestResponse, RunSummary
from skas_algo.data.provider import get_price_loader
from skas_algo.db.models import Algo, AlgoRun
from skas_algo.engine.market import PriceLoader
from skas_algo.services.backtest import run_backtest
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


@router.post("/backtest", response_model=BacktestResponse)
def post_backtest(
    req: BacktestRequest,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
) -> BacktestResponse:
    if not req.symbols:
        raise HTTPException(status_code=422, detail="symbols must not be empty")
    try:
        result = run_backtest(db, loader, req)
    except KeyError as exc:  # unknown strategy_id
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BacktestResponse(**result)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(db: Session = Depends(get_db)) -> list[RunSummary]:
    rows = db.execute(
        select(AlgoRun, Algo).join(Algo, AlgoRun.algo_id == Algo.id).order_by(AlgoRun.id.desc())
    ).all()
    return [
        RunSummary(
            run_id=run.id,
            algo_id=algo.id,
            name=algo.name,
            strategy_id=algo.strategy_id,
            mode=run.mode.value,
            started_at=run.started_at.isoformat() if run.started_at else None,
            metrics=run.metrics.get("metrics", {}) if run.metrics else {},
        )
        for run, algo in rows
    ]


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
        "mode": run.mode.value,
        "report": run.metrics,
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
