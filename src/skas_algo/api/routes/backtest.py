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


# Strategies that are only ever deployed live/paper from the Trade UI (a user-built or
# screener-resolved position) — they have no backtest config form, so keep them out of the
# New-backtest dropdown. ``donchian_strangle_monthly`` additionally has no backtest path at all.
_DEPLOY_ONLY = {"custom_options", "call_put_ratio_expiry", "delta_neutral_monthly", "iron_fly_monthly", "momentum_theta_gainer_intra", "custom_equity", "donchian_strangle_monthly", "intraday_straddle", "weekly_intraday_straddle", "broker_smoke_test", "double_diagonal_calendar"}


# The intraday-basis list of the unified backtest page: deploy-only options strategies the
# replay harness (services/intraday_replay.py) drives over the 1-min option store, plus
# momentum_theta (its dedicated BS service, adapted). Order = display order.
_INTRADAY_REPLAY = ["intraday_straddle", "weekly_intraday_straddle", "call_put_ratio_expiry",
                    "delta_neutral_monthly", "iron_fly_monthly", "momentum_theta_gainer_intra",
                    # The positional family joined the store (2026-07-18): ALL index-options
                    # strategies replay on 1-min data; only stock-option strategies keep EOD.
                    "call_ratio_monthly", "put_ratio_monthly", "batman_ratio_monthly",
                    "hni_weekly", "21_ema_momentum"]


@router.get("/strategies")
def list_strategies(basis: str = "eod") -> dict:
    if basis == "intraday":
        return {"strategies": list(_INTRADAY_REPLAY)}
    return {"strategies": [s for s in available() if s not in _DEPLOY_ONLY]}


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


@router.get("/universes/{name}/symbols")
def universe_symbols(
    name: str, avail: set[str] = Depends(get_available_symbols)
) -> dict:
    """The cached symbols a named universe resolves to — lets the client chunk a cache refresh
    into small batches (with a progress indicator) instead of one long blocking call."""
    try:
        syms = universes.resolve(name, avail)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not syms:
        raise HTTPException(status_code=404,
                            detail=f"universe {name!r} resolved to no cached symbols")
    return {"name": name, "symbols": syms}


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


@router.post("/backtest/intraday")
def post_backtest_intraday(req: BacktestRequest) -> dict:
    """The unified page's INTRADAY basis: replay a deploy-only options strategy over the
    self-captured 1-min option store (services/intraday_replay), or dispatch momentum_theta
    to its BS service — emitting the SAME run contract as POST /backtest.

    Runs as a BACKGROUND JOB (a 5-year window is minutes of replay — one blocking request
    had no progress and its preview died with the page, 2026-07-17): returns {job_id}
    immediately; poll GET /backtest/intraday/progress for {done,total,day} and, when
    status=="done", the full {report,trades} result (feed it to POST /backtest/save to
    persist — unchanged). Cheap validation stays synchronous (404/422/409 up front)."""
    from datetime import date as _date

    from skas_algo.data.option_intraday_store import captured_days
    from skas_algo.services import replay_jobs
    from skas_algo.services.intraday_replay import (
        REPLAYABLE,
        run_intraday_backtest,
        run_mtg_backtest,
    )

    underlying = (req.underlying or (req.symbols[0] if req.symbols else None) or "NIFTY").upper()
    req.underlying = underlying
    req.symbols = req.symbols or [underlying]
    req.instrument_class = "DERIV"
    req.end_date = req.end_date or _date.today()
    req.params["data_basis"] = "intraday"   # run tag (ParametersCard shows it; no migration)
    is_mtg = req.strategy_id == "momentum_theta_gainer_intra"
    if not is_mtg and req.strategy_id not in REPLAYABLE:
        raise HTTPException(status_code=404,
                            detail=f"{req.strategy_id} has no intraday replay")
    if not is_mtg and not any(
            req.start_date.isoformat() <= d <= req.end_date.isoformat()
            for d in captured_days()):
        raise HTTPException(status_code=422,
                            detail="the option store has no captured days in this window — "
                                   "see Data → Options for coverage")
    # Strategy params only — the tags aren't constructor args (harmless via **_ignored, but
    # keep the replay's param surface clean).
    sparams = {k: v for k, v in req.params.items()
               if k not in ("data_basis", "premium_source")}
    if is_mtg:
        req.params["premium_source"] = "black_scholes"  # labeled: NOT real store premiums

    def work(progress):
        if is_mtg:
            result = run_mtg_backtest(req.start_date, req.end_date, req.capital, sparams)
        else:
            result = run_intraday_backtest(req.strategy_id, underlying, req.start_date,
                                           req.end_date, req.capital, sparams,
                                           progress=progress)
        out = {"run_id": None, "algo_id": None, "strategy_id": req.strategy_id,
               "report": result["report"], "trades": result["trades"]}
        if req.persist:
            # The job thread outlives the request — persist on its OWN session.
            from skas_algo.db.base import session_scope

            with session_scope() as db:
                out.update({k: v for k, v in
                            persist_backtest(db, req, result["report"],
                                             result["trades"]).items()
                            if k in ("run_id", "algo_id")})
        return out

    try:
        return {"job_id": replay_jobs.start(work)}
    except RuntimeError as exc:   # single-flight: one replay at a time
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/backtest/intraday/progress")
def get_backtest_intraday_progress() -> dict:
    """Snapshot of the (single-flight) replay job: status running|done|error|idle, the
    day counter for the bar, and the full result once done — retained until the next job
    starts, so a page revisit simply re-attaches."""
    from skas_algo.services import replay_jobs

    return replay_jobs.snapshot()


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
        # Live equity (paper) runs build the report on-demand; trades resolve to the running
        # session's live transactions so the report view matches the deployment in real time.
        "report": _run_report(run, algo),
        "trades": _resolve_run_trades(run, db),
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


def _run_report(run: AlgoRun, algo: Algo | None) -> dict | None:
    """The run's full report. For a still-running EQUITY (paper) deployment it's built on-demand from
    the live session's history/transactions so the report view (equity curve, yearly, monthly booked,
    capital utilization) shows live at backtest parity — the stored ``metrics`` are only written on
    stop. Backtests / stopped / options runs return the stored report unchanged."""
    if _instrument_class(algo) != "STOCK":
        return run.metrics
    from skas_algo.engine.jsonutil import to_native
    from skas_algo.engine.report import build_report
    from skas_algo.engine.runner import RunResult
    from skas_algo.live.manager import manager

    live = manager.get(run.id)
    if live is None or not live.session.history:
        return run.metrics
    rr = RunResult(
        history=live.session.history,
        transactions=live.session.transactions,
        monthly_flush_log=live.session.monthly_flush_log,
        portfolio=live.session.portfolio,
    )
    return to_native(build_report(rr, algo.capital if algo else 0.0))


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


@router.get("/runs/{run_id}/cycles/{index}/detail")
def get_cycle_detail(run_id: int, index: int, db: Session = Depends(get_db)) -> dict:
    """The position-lifecycle model for ONE options cycle (entry → rolls/hedges → exit) with
    reconstructed per-event net delta — powers the Cycle Detail page. Cache-only, read-only."""
    from skas_algo.data.options_provider import INDEX_SYMBOL, _ffill_lookup
    from skas_algo.data.provider import get_data_cache
    from skas_algo.engine.jsonutil import to_native
    from skas_algo.services.cycle_detail import build_cycle_detail, reconstruct_cycles

    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    algo = db.get(Algo, run.algo_id)
    trades = _resolve_run_trades(run, db)
    # Backtests + stopped runs have the options cycles in the stored report; a RUNNING live
    # options deployment has none yet → reconstruct from its live trades (same newest-first
    # order the live page displays, so the index the UI links matches).
    cycles = ((_run_report(run, algo) or {}).get("options") or {}).get("cycles") or []
    if not cycles:
        cycles = reconstruct_cycles(trades)
    if not (0 <= index < len(cycles)):
        raise HTTPException(status_code=404, detail="cycle index out of range")
    cycle = cycles[index]
    leg_syms = {leg.get("symbol") for leg in (cycle.get("legs_detail") or [])}
    lo, hi = str(cycle.get("entry_date"))[:10], str(cycle.get("exit_date") or "9999")[:10]
    rows = [t for t in trades if t.get("ticker") in leg_syms
            and lo <= str(t.get("date"))[:10] <= hi]
    sd = get_data_cache()
    sym = INDEX_SYMBOL.get(str(cycle.get("underlying", "")).upper()) or cycle.get("underlying")
    margin_series = (((_run_report(run, algo) or {}).get("options") or {})
                     .get("margin_series")) or []
    model = build_cycle_detail(
        cycle, rows, _ffill_lookup(sd, sym), margin_series,
        index=index, run_id=run_id, strategy_id=(algo.strategy_id if algo else ""),
        name=(algo.name if algo else f"run #{run_id}"))
    # Whether the RUN is a deployment (paper/live) vs a backtest — drives the breadcrumb
    # target. Distinct from the per-cycle ``live`` flag (a CLOSED cycle on a live run is
    # ``live=False`` but still belongs on /live, not /runs).
    model["is_deployment"] = run.mode in (TradingMode.PAPER, TradingMode.LIVE)
    return to_native(model)


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
    curve = (_run_report(run, algo) or {}).get("equity_curve", [])  # live for a running equity run
    dates = [p["date"] for p in curve]
    try:
        points = benchmark_series(loader, index, dates, algo.capital if algo else 0.0)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"index": index, "points": points}
