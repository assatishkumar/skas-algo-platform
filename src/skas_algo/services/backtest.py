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
        if req.strategy_id == "donchian_strangle_bt":
            # Basket run: ~50 stock underlyings priced via Black-Scholes (no stock-option
            # history exists) + the REAL cached NIFTY chain for the hedge legs, and the
            # per-cycle leg schedule (the "backtest screener") injected post-construction —
            # deterministic from (universe, dates, params), so it is NOT persisted.
            from skas_algo.data.basket_options import build_basket_options_run
            from skas_algo.services.donchian_bt import (
                build_cycle_schedule,
                estimate_capital,
                resolve_basket,
            )

            names = resolve_basket(
                req.universe or "nifty50",
                set(sd.list_cached_symbols(asset_type="stock")),
                exclude=req.params.get("exclude_symbols"),
                include=req.params.get("include_symbols"),
            )
            price_kwargs = {k: req.params[k] for k in ("r", "vol_window", "vol_multiplier")
                            if k in req.params}
            market_view, _chain, settler, margin_model = build_basket_options_run(
                sd, names, req.start_date, req.end_date,
                lot_overrides=req.params.get("contract_specs"),
                margin_params=req.params.get("margin"), equity_loader=loader, **price_kwargs,
            )
            sched_kwargs = {k: req.params[k] for k in (
                "r", "vol_window", "vol_multiplier", "skip_leg_min_premium_pct", "round_out",
                "breakout_atm", "lots_per_name", "hedge_enabled", "hedge_otm_pct",
                "notional_per_name", "min_hv_ratio", "min_channel_width_pct",
                "vix_half_threshold", "vix_skip_threshold",
            ) if k in req.params}
            schedule = build_cycle_schedule(sd, names, req.start_date, req.end_date,
                                            **sched_kwargs)
            strategy.set_cycles(schedule)
            # Auto-capital (capital <= 0 from the form): the basket's size is fixed by
            # lots-per-name, so capital is a funding consequence — modelled peak entry
            # margin × 1.10, rounded up to the lakh. Persisted on the run like any capital.
            if req.capital <= 0:
                req.capital = estimate_capital(
                    schedule, req.params.get("margin")) or 10_000_000
        elif synthetic:
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
        # Strategies that need the index's daily OHLC (21_ema_momentum's EMA channel —
        # the options views expose close-only spot) get a cache-backed provider. The end
        # bound is whatever the strategy passes (its ctx.today()) so there's no lookahead.
        bars_hook = getattr(strategy, "set_daily_bars_fn", None)
        if bars_hook is not None:
            from skas_algo.data.options_provider import INDEX_SYMBOL

            def _daily_bars(u: str, start, end):
                sym = INDEX_SYMBOL.get(u.upper()) or u.upper()
                return sd.get_prices(symbol=sym, start_date=start, end_date=end)

            bars_hook(_daily_bars)
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
        # SuperTrend strategies precompute their direction from OHLC in the market view.
        supertrend = (
            strategy.supertrend_config()
            if getattr(strategy, "needs_supertrend", False) and hasattr(strategy, "supertrend_config")
            else None
        )
        runner = BacktestRunner(
            strategy=strategy,
            universe=list(req.symbols),
            loader=loader,
            initial_capital=req.capital,
            lookback=req.lookback,
            tax_rate=req.tax_rate,
            withdrawal_rate=req.withdrawal_rate,
            overrides=overrides,
            supertrend=supertrend,
        )
    result = runner.run(req.start_date, req.end_date)
    # Strategies that opt in (e.g. SuperTrend Momentum) get the deployed-capital + idle-cash
    # CAGR overlay; idle rate is configurable (default 6%). Other strategies are unchanged.
    want_deployed = getattr(strategy, "report_deployed_metrics", False)
    report = build_report(
        result, req.capital,
        deployed_metrics=want_deployed,
        idle_return=float(req.params.get("idle_return", 0.06)) if want_deployed else 0.0,
    )
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
    if req.strategy_id == "donchian_strangle_bt" and report.get("options") is not None:
        # Cycle-first basket view (cycle → names → legs) — the generic per-leg positions
        # table is unreadable for a ~50-underlying basket. The UI renders this instead.
        from skas_algo.services.donchian_bt import basket_cycles_report

        report["options"]["basket_cycles"] = to_native(
            basket_cycles_report(trades, result.history))

    # Preview: hand back the computed report/trades WITHOUT writing to the DB. The client can
    # later persist the same result via persist_backtest (/backtest/save) — no recompute.
    if not req.persist:
        return {"run_id": None, "algo_id": None, "strategy_id": req.strategy_id,
                "report": report, "trades": trades}
    return persist_backtest(session, req, report, trades)


def persist_backtest(session: Session, req: BacktestRequest, report: dict, trades: list[dict]) -> dict:
    """Persist an already-computed backtest as an Algo + AlgoRun (BACKTEST mode). No recompute —
    used both by run_backtest (when persist=True) and by the explicit /backtest/save endpoint."""
    is_deriv = req.instrument_class.upper() == "DERIV"
    underlying = (req.underlying or req.params.get("underlying")
                  or (req.symbols[0] if req.symbols else "NIFTY"))
    factory = get_strategy(req.strategy_id)

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

    from skas_algo.services.vault_export import export_run_safe
    export_run_safe(run, algo)  # write a run-card into the Obsidian vault (no-op if not configured)

    return {
        "run_id": run.id,
        "algo_id": algo.id,
        "strategy_id": req.strategy_id,
        "report": report,
        "trades": trades,
    }
