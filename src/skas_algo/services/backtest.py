"""Backtest orchestration: run the engine, build a report, persist the run."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from skas_algo.api.models import BacktestRequest
from skas_algo.db.enums import InstrumentClass, TradingMode
from skas_algo.db.models import Algo, AlgoRun
from skas_algo.engine.jsonutil import to_native
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.overrides import OverrideRule
from skas_algo.engine.report import build_report
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.registry import get_strategy


def _serialize_trades(transactions: list[dict]) -> list[dict]:
    out = []
    for t in transactions:
        row = to_native(dict(t))
        row["date"] = t["date"].strftime("%Y-%m-%d")
        out.append(row)
    return out


def run_backtest(session: Session, loader: PriceLoader, req: BacktestRequest) -> dict:
    factory = get_strategy(req.strategy_id)
    strategy = factory(universe=list(req.symbols), initial_capital=req.capital, **req.params)

    overrides = [OverrideRule(scope=o.scope, target=o.target, rule=o.rule) for o in req.overrides]

    runner = BacktestRunner(
        strategy=strategy,
        universe=list(req.symbols),
        loader=loader,
        initial_capital=req.capital,
        lookback=req.lookback,
        tax_rate=req.tax_rate,
        withdrawal_rate=req.withdrawal_rate,
        overrides=overrides,
    )
    result = runner.run(req.start_date, req.end_date)
    report = build_report(result, req.capital)
    trades = _serialize_trades(result.transactions)

    # Persist as an Algo + AlgoRun (BACKTEST mode).
    algo = Algo(
        name=req.name or f"{req.strategy_id} backtest",
        notes=req.notes,
        strategy_id=req.strategy_id,
        instrument_class=InstrumentClass.STOCK,
        mode=TradingMode.BACKTEST,
        capital=req.capital,
        params={
            "universe": req.universe,
            "symbols": req.symbols,
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "lookback": req.lookback,
            "tax_rate": req.tax_rate,
            "withdrawal_rate": req.withdrawal_rate,
            **req.params,
        },
    )
    session.add(algo)
    session.flush()

    run = AlgoRun(
        algo_id=algo.id,
        mode=TradingMode.BACKTEST,
        batch_id=req.batch_id,
        started_at=datetime.now(UTC),
        stopped_at=datetime.now(UTC),
        params_snapshot=algo.params,
        metrics=report,
        trade_log=trades,
    )
    session.add(run)
    session.flush()

    return {
        "run_id": run.id,
        "algo_id": algo.id,
        "strategy_id": req.strategy_id,
        "report": report,
        "trades": trades,
    }
