"""Seed a live (PAPER) deployment from a historical backtest.

When a deployment is started with ``warm_from_date`` in the past, we replay the strategy
as a backtest from that date up to today and carry the resulting open position + strategy
state forward as the live starting book. This lets a strategy that enters on a slow signal
(e.g. an "enter a month before expiry" options spread, or an equity trend-rider mid-trend)
be forward-tested today instead of waiting for the next entry.

PAPER only. Equity seeds need the price cache back to ``warm_from_date``; options seeds also
need the option chain (bhavcopy) cached back to that date. A clear error is raised when the
replay can't be built.
"""

from __future__ import annotations

from datetime import date

from skas_algo.engine.options.charges import ChargeModel
from skas_algo.engine.overrides import OverrideRule
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.registry import get_strategy


def _state_from_result(result, strategy, overrides, end_date: date) -> dict:
    """The LiveSession state (open book + strategy + overrides) at the end of a replay."""
    return {
        "portfolio": result.portfolio.export_state(),
        "stops": [],  # trailing stops are re-managed live from the carried-forward lots
        "strategy": strategy.export_state() if hasattr(strategy, "export_state") else {},
        "overrides": [
            {"scope": o.scope, "target": o.target, "rule": o.rule, "active": True}
            for o in overrides
        ],
        "current_month": [end_date.year, end_date.month],
    }


def _seed_equity(config, loader, start, end_date, strategy_params, overrides) -> dict:
    symbols = list(config.symbols)
    if not symbols:
        raise ValueError("no symbols to seed the equity backtest")
    strategy = get_strategy(config.strategy_id)(
        universe=symbols, initial_capital=config.capital, **strategy_params,
    )
    # SuperTrend strategies precompute their direction from OHLC in the replay feed.
    supertrend = (
        strategy.supertrend_config()
        if getattr(strategy, "needs_supertrend", False) and hasattr(strategy, "supertrend_config")
        else None
    )
    runner = BacktestRunner(
        strategy=strategy, universe=symbols, loader=loader,
        initial_capital=config.capital, lookback=config.lookback,
        tax_rate=config.tax_rate, withdrawal_rate=config.withdrawal_rate,
        overrides=overrides, supertrend=supertrend,
    )
    try:
        result = runner.run(start, end_date)
    except Exception as exc:  # pragma: no cover - missing cache → clear message
        raise ValueError(
            f"could not replay the equity backtest from {start} — refresh the price cache "
            f"for those symbols/range first ({exc})"
        ) from exc
    return {
        "state": _state_from_result(result, strategy, overrides, end_date),
        "transactions": result.transactions,
        "history": result.history,
    }


def _seed_options(config, loader, start, end_date, strategy_params, overrides) -> dict:
    from skas_algo.data.provider import get_data_cache
    from skas_algo.data.synthetic_options import build_synthetic_options_run, is_synthetic

    underlying = (config.underlying or (config.symbols[0] if config.symbols else "NIFTY")).upper()
    strategy = get_strategy(config.strategy_id)(
        universe=[underlying], initial_capital=config.capital, **strategy_params,
    )

    sd = get_data_cache()
    try:
        if is_synthetic(underlying) or bool(config.params.get("synthetic")):
            opt_kwargs = {
                k: config.params[k]
                for k in ("r", "vol_window", "strike_step", "strike_count", "vol_premium")
                if k in config.params
            }
            mv, _chain, settler, margin = build_synthetic_options_run(
                sd, underlying, start, end_date,
                lot_overrides=config.params.get("contract_specs"),
                margin_params=config.params.get("margin"), equity_loader=loader, **opt_kwargs,
            )
        else:
            from skas_algo.data.options_provider import build_options_run

            mv, _chain, settler, margin = build_options_run(
                sd, underlying, start, end_date,
                lot_overrides=config.params.get("contract_specs"),
                margin_params=config.params.get("margin"), equity_loader=loader,
            )
    except Exception as exc:  # pragma: no cover - missing cache → clear message
        raise ValueError(
            f"could not build the option chain for {underlying} back to {start} — "
            f"refresh the options cache for that range first ({exc})"
        ) from exc

    runner = BacktestRunner(
        strategy=strategy, universe=[underlying], loader=loader,
        initial_capital=config.capital, lookback=config.lookback,
        tax_rate=0.0, withdrawal_rate=config.withdrawal_rate, overrides=overrides,
        market_view=mv, settler=settler, margin_model=margin, charge_model=ChargeModel(),
    )
    result = runner.run(start, end_date)
    return {
        "state": _state_from_result(result, runner.strategy, overrides, end_date),
        "transactions": result.transactions,
        "history": result.history,
    }


def seed_state_from_backtest(config, loader, *, end_date: date) -> dict:
    """Replay ``config``'s strategy from ``warm_from_date`` → ``end_date`` and return
    ``{"state", "transactions", "history"}`` to load into a LiveSession before going live.

    ``state`` is the LiveSession state (portfolio + strategy + overrides) — the final open book
    becomes the live starting position. ``transactions``/``history`` carry the replay's trades +
    equity curve so a run that booked (and is now flat) still shows its realized P&L and trade
    log instead of looking like an empty deployment. Dispatches by instrument class."""
    from skas_algo.live.manager import strategy_kwargs

    start = config.warm_from_date
    if start is None or start >= end_date:
        raise ValueError("warm_from_date must be a past date")

    strategy_params = strategy_kwargs(get_strategy(config.strategy_id), config.params)
    overrides = [OverrideRule(scope=o.scope, target=o.target, rule=o.rule) for o in config.overrides]

    if config.instrument_class.upper() == "DERIV":
        return _seed_options(config, loader, start, end_date, strategy_params, overrides)
    return _seed_equity(config, loader, start, end_date, strategy_params, overrides)
