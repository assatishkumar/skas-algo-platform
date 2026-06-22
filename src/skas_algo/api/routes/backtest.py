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
    SaveBacktestRequest,
    UniverseOut,
    iso_utc,
)
from skas_algo.data import universes
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.db.enums import InstrumentClass, TradingMode
from skas_algo.db.models import Algo, AlgoRun, Order, StrategyTemplate
from skas_algo.engine.market import PriceLoader
from skas_algo.services.backtest import persist_backtest, run_backtest
from skas_algo.services.benchmark import BENCHMARK_INDICES, benchmark_series
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


@router.get("/benchmarks")
def list_benchmarks() -> dict:
    """Index series available to overlay on the equity curve."""
    return {"benchmarks": BENCHMARK_INDICES}


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


def _resolve_universe(req: BacktestRequest, avail: set[str]) -> None:
    """Expand a named equity universe to its cached symbols, validating the request in place."""
    # Options (DERIV) runs trade a dynamic option chain for an underlying, so they
    # need neither explicit symbols nor a named equity universe.
    if req.instrument_class.upper() == "DERIV":
        if not (req.underlying or req.params.get("underlying")):
            raise HTTPException(status_code=422, detail="underlying required for a DERIV backtest")
        return
    if req.universe:
        try:
            req.symbols = universes.resolve(req.universe, avail)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not req.symbols:
        raise HTTPException(status_code=422, detail="symbols or a valid universe required")


@router.post("/backtest", response_model=BacktestResponse)
def post_backtest(
    req: BacktestRequest,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> BacktestResponse:
    _resolve_universe(req, avail)
    try:
        result = run_backtest(db, loader, req)
    except KeyError as exc:  # unknown strategy_id
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BacktestResponse(**result)


@router.post("/backtest/save", response_model=BacktestResponse)
def save_backtest(
    body: SaveBacktestRequest,
    db: Session = Depends(get_db),
    avail: set[str] = Depends(get_available_symbols),
) -> BacktestResponse:
    """Persist a previously-previewed backtest (its report + trades) without recomputing."""
    _resolve_universe(body.request, avail)
    try:
        result = persist_backtest(db, body.request, body.report, body.trades)
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
                batch_id=run.batch_id,
                started_at=iso_utc(run.started_at),
                metrics=run.metrics.get("metrics", {}) if run.metrics else {},
            )
        )
    return out


def _get_run(db: Session, run_id: int) -> AlgoRun:
    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/runs/compare")
def compare_runs(ids: str, db: Session = Depends(get_db)) -> dict:
    """Compare up to 5 backtest runs: metrics + rebased-to-100 equity curves."""
    try:
        run_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="ids must be comma-separated integers") from exc
    if not 2 <= len(run_ids) <= 5:
        raise HTTPException(status_code=422, detail="compare between 2 and 5 runs")

    out = []
    for rid in run_ids:
        run = db.get(AlgoRun, rid)
        if run is None or run.mode != TradingMode.BACKTEST:
            raise HTTPException(status_code=422, detail=f"run {rid} is not a backtest")
        algo = db.get(Algo, run.algo_id)
        curve = (run.metrics or {}).get("equity_curve", [])
        base = curve[0]["equity"] if curve and curve[0]["equity"] else 0.0
        growth = (
            [{"date": p["date"], "value": 100.0 * p["equity"] / base} for p in curve]
            if base
            else []
        )
        entry = {
            "run_id": run.id,
            "name": algo.name if algo else f"run {rid}",
            "strategy_id": algo.strategy_id if algo else None,
            "params": algo.params if algo else {},
            "capital": algo.capital if algo else None,
            "metrics": (run.metrics or {}).get("metrics", {}),
            "growth": growth,
        }
        opt = (run.metrics or {}).get("options")
        if opt:
            # Slim per-cycle rows so options runs can be compared position-by-position
            # (aligned by entry month in the UI) without shipping full legs_detail.
            cycle_keys = ("entry_date", "exit_date", "expiry", "exit_reason", "holding_days",
                          "premium_collected", "realized_pnl", "charges", "net_pnl",
                          "underlying_entry", "underlying_exit", "vix_entry", "vix_exit")
            entry["options"] = {
                "summary": opt.get("summary", {}),
                "charges": opt.get("charges", {}),
                "exit_reasons": opt.get("exit_reasons", {}),
                "cycles": [{k: c.get(k) for k in cycle_keys} | {"n_legs": len(c.get("legs", []))}
                           for c in opt.get("cycles", [])],
            }
        out.append(entry)
    return {"runs": out}


@router.patch("/runs/{run_id}")
def update_run(run_id: int, body: DeploymentUpdate, db: Session = Depends(get_db)) -> dict:
    run = _get_run(db, run_id)
    algo = db.get(Algo, run.algo_id)
    if body.name is not None:
        algo.name = body.name
    if body.notes is not None:
        algo.notes = body.notes
    return {"run_id": run_id, "name": algo.name, "notes": algo.notes}


def _template_out(t: StrategyTemplate) -> dict:
    return {"strategy_id": t.strategy_id, "run_id": t.run_id, "name": t.name,
            "capital": t.capital, "params": t.params}


@router.get("/strategies/templates")
def list_templates(db: Session = Depends(get_db)) -> dict:
    """Per-strategy default-params templates, keyed by strategy_id."""
    rows = db.execute(select(StrategyTemplate)).scalars().all()
    return {"templates": {t.strategy_id: _template_out(t) for t in rows}}


@router.post("/runs/{run_id}/set-template")
def set_template(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Make this run's params the default template for its strategy (one per strategy).
    Params are copied, so the template survives if the run is later deleted."""
    run = _get_run(db, run_id)
    algo = db.get(Algo, run.algo_id)
    if algo is None:
        raise HTTPException(status_code=404, detail="run's algo not found")
    t = db.get(StrategyTemplate, algo.strategy_id) or StrategyTemplate(strategy_id=algo.strategy_id)
    t.run_id = run.id
    t.name = algo.name
    t.capital = algo.capital
    t.params = dict(algo.params or {})
    db.merge(t)
    db.flush()
    return _template_out(t)


@router.delete("/strategies/{strategy_id}/template")
def clear_template(strategy_id: str, db: Session = Depends(get_db)) -> dict:
    t = db.get(StrategyTemplate, strategy_id)
    if t is not None:
        db.delete(t)
    return {"strategy_id": strategy_id, "cleared": t is not None}


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
        "batch_id": run.batch_id,
        "capital": algo.capital if algo else None,
        "params": algo.params if algo else {},  # symbols, lookback, tax, sizing, etc.
        "mode": run.mode.value,
        "report": run.metrics,
        "trades": run.trade_log or [],
    }


def _instrument_class(algo: Algo | None, trades: list[dict] | None = None) -> str:
    """Effective instrument class for the analysis view. Robust to older live deployments
    whose Algo row was created before the class was threaded through (it was hardcoded to
    STOCK): prefer the column, then ``params.instrument_class`` (the deploy carried it), then
    detect option-symbol tickers (``UNDERLYING|EXPIRY|STRIKE|RIGHT``) in the trades."""
    if algo is not None and algo.instrument_class == InstrumentClass.DERIV:
        return "DERIV"
    if algo is not None and str((algo.params or {}).get("instrument_class", "")).upper() == "DERIV":
        return "DERIV"
    if trades and any((t.get("ticker") or "").count("|") == 3 for t in trades):
        return "DERIV"
    return "STOCK"


@router.get("/analysis/runs")
def analysis_runs(db: Session = Depends(get_db)) -> list[dict]:
    """All runs selectable in the Trade Analysis page (backtests + paper/live deployments)."""
    rows = db.execute(
        select(AlgoRun, Algo).join(Algo, AlgoRun.algo_id == Algo.id).order_by(AlgoRun.id.desc())
    ).all()
    out: list[dict] = []
    for run, algo in rows:
        mode = run.mode.value
        if mode == TradingMode.BACKTEST.value:
            status = "backtest"
        elif run.archived:
            status = "archived"
        elif run.stopped_at:
            status = "stopped"
        else:
            status = "active"
        out.append({
            "run_id": run.id,
            "name": algo.name if algo else None,
            "strategy_id": algo.strategy_id if algo else None,
            "instrument_class": _instrument_class(algo),
            "mode": mode,
            "status": status,
        })
    return out


def _resolve_run_trades(run: AlgoRun, db: Session) -> list[dict]:
    """Trades for any run, preferring the richest available source — so the Analysis page
    shows exactly what the Live page does for an active deployment. Order of preference:
    the running session's in-memory transactions (exit_reason/holding_days/per-leg P&L) →
    the finalized ``trade_log`` (backtests + stopped deployments) → a reconstruction from
    the durable Order rows. Mirrors the /live/{id}/trades resolution."""
    from skas_algo.api.routes.live import _orders_to_trades
    from skas_algo.live.manager import _serialize_event, manager

    live = manager.get(run.id)
    if live is not None and live.session.transactions:
        return [_serialize_event(t) for t in live.session.transactions]
    trades = run.trade_log or []
    if trades:
        return trades
    if run.mode != TradingMode.BACKTEST:
        orders = db.execute(
            select(Order).where(Order.algo_id == run.algo_id).order_by(Order.id)
        ).scalars().all()
        return _orders_to_trades(orders)
    return []


@router.get("/runs/{run_id}/analysis")
def run_analysis(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Unified trade feed for the analysis page — works for any run. Prefers a running
    deployment's in-memory transactions, then the finalized ``trade_log`` (backtests +
    stopped deployments), then a reconstruction from the durable Order rows."""
    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    algo = db.get(Algo, run.algo_id)
    trades = _resolve_run_trades(run, db)
    return {
        "run_id": run.id,
        "name": algo.name if algo else None,
        "strategy_id": algo.strategy_id if algo else None,
        "instrument_class": _instrument_class(algo, trades),
        "params": algo.params if algo else {},
        "capital": algo.capital if algo else None,
        "trades": trades,
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


@router.get("/runs/{run_id}/benchmark")
def get_run_benchmark(
    run_id: int,
    index: str,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
) -> dict:
    """Index buy-and-hold of the run's initial capital, aligned to its equity dates."""
    if index not in BENCHMARK_INDICES:
        raise HTTPException(status_code=400, detail=f"unknown index {index!r}")
    run = _get_run(db, run_id)
    algo = db.get(Algo, run.algo_id)
    curve = (run.metrics or {}).get("equity_curve", [])
    dates = [p["date"] for p in curve]
    try:
        points = benchmark_series(loader, index, dates, algo.capital if algo else 0.0)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"index": index, "points": points}
