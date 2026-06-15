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


def _effective_strategy_params(factory, explicit: dict) -> dict:
    """The strategy's full effective config: explicit params + signature defaults.

    Walks the factory's MRO so subclass-only params (e.g. Batman's
    combined_credit_limit_pct behind *args/**kwargs) are captured too. Makes a run
    self-documenting — defaults applied silently are persisted, not lost.
    """
    import inspect

    skip = {"self", "universe", "initial_capital", "lot_overrides"}
    out: dict = {}
    classes = factory.__mro__ if isinstance(factory, type) else [factory]
    for cls in classes:
        init = cls.__init__ if isinstance(cls, type) else cls
        try:
            sig = inspect.signature(init)
        except (TypeError, ValueError):  # builtins / object.__init__
            continue
        for name, p in sig.parameters.items():
            if (name in skip or name in out
                    or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
                    or p.default is inspect.Parameter.empty):
                continue
            out[name] = explicit.get(name, p.default)
    return out


def _serialize_trades(transactions: list[dict]) -> list[dict]:
    out = []
    for t in transactions:
        row = to_native(dict(t))
        row["date"] = t["date"].strftime("%Y-%m-%d")
        out.append(row)
    return out


def run_backtest(session: Session, loader: PriceLoader, req: BacktestRequest) -> dict:
    is_deriv = req.instrument_class.upper() == "DERIV"
    underlying = (req.underlying or req.params.get("underlying")
                  or (req.symbols[0] if req.symbols else "NIFTY"))

    factory = get_strategy(req.strategy_id)
    strategy_universe = [underlying] if is_deriv else list(req.symbols)
    strategy = factory(universe=strategy_universe, initial_capital=req.capital, **req.params)

    overrides = [OverrideRule(scope=o.scope, target=o.target, rule=o.rule) for o in req.overrides]

    if is_deriv:
        # Options run: build the chain/lazy-market/settlement/margin stack and drive the
        # SAME runner with a prebuilt view. GOLD has no traded-option data, so its chain is
        # synthesized via Black-Scholes (realized vol); NIFTY/BANKNIFTY read the real cache.
        from skas_algo.data.provider import get_data_cache
        from skas_algo.data.synthetic_options import build_synthetic_options_run, is_synthetic

        sd = get_data_cache()
        synthetic = is_synthetic(underlying) or bool(req.params.get("synthetic"))
        if synthetic:
            opt_kwargs = {k: req.params[k]
                          for k in ("r", "vol_window", "strike_step", "strike_count", "vol_premium")
                          if k in req.params}
            market_view, _chain, settler, margin_model = build_synthetic_options_run(
                sd, underlying.upper(), req.start_date, req.end_date,
                lot_overrides=req.params.get("contract_specs"),
                margin_params=req.params.get("margin"), equity_loader=loader, **opt_kwargs,
            )
        else:
            from skas_algo.data.options_provider import build_options_run

            market_view, _chain, settler, margin_model = build_options_run(
                sd, underlying.upper(), req.start_date, req.end_date,
                lot_overrides=req.params.get("contract_specs"),
                margin_params=req.params.get("margin"), equity_loader=loader,
            )
        # Options are business income (slab) → no per-trade tax modelled; instead F&O
        # transaction charges (brokerage + STT + exchange + GST + SEBI + stamp) are
        # deducted at execution so the equity curve is net of costs.
        from skas_algo.engine.options.charges import ChargeModel

        runner = BacktestRunner(
            strategy=strategy, universe=strategy_universe, loader=loader,
            initial_capital=req.capital, lookback=req.lookback,
            tax_rate=0.0, withdrawal_rate=req.withdrawal_rate, overrides=overrides,
            market_view=market_view, settler=settler, margin_model=margin_model,
            charge_model=ChargeModel(),
        )
    else:
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
    if is_deriv and report.get("options"):
        # Tag each position/cycle with underlying (NIFTY/GOLD) + India VIX at entry/exit,
        # and attach the underlying price series for the covered-call timeline charts.
        from skas_algo.data.options_provider import (
            attach_underlying_timeline,
            enrich_with_market,
        )

        enrich_with_market(get_data_cache(), report["options"], underlying.upper())
        attach_underlying_timeline(get_data_cache(), report["options"], underlying.upper(),
                                   req.start_date, req.end_date)
    trades = _serialize_trades(result.transactions)

    # Persist as an Algo + AlgoRun (BACKTEST mode).
    algo = Algo(
        name=req.name or f"{req.strategy_id} backtest",
        notes=req.notes,
        strategy_id=req.strategy_id,
        instrument_class=InstrumentClass.DERIV if is_deriv else InstrumentClass.STOCK,
        mode=TradingMode.BACKTEST,
        capital=req.capital,
        params={
            "universe": req.universe,
            "symbols": req.symbols,
            "instrument_class": req.instrument_class,
            "underlying": underlying if is_deriv else None,
            "start_date": req.start_date.isoformat(),
            "end_date": req.end_date.isoformat(),
            "lookback": req.lookback,
            "tax_rate": req.tax_rate,
            "withdrawal_rate": req.withdrawal_rate,
            # Effective strategy config (defaults included), then explicit overrides.
            **_effective_strategy_params(factory, req.params),
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
