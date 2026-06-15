"""Seed a live (PAPER) options deployment from a historical backtest.

When a deployment is started with ``warm_from_date`` in the past, we replay the strategy
as a backtest from that date up to today and carry the resulting open position + strategy
state forward as the live starting book. This lets an "enter a month before expiry"
strategy (e.g. Batman) be forward-tested today instead of waiting for the next cycle.

PAPER only. Requires the option chain (bhavcopy) to be cached back to ``warm_from_date``;
a clear error is raised when the replay can't be built.
"""

from __future__ import annotations

from datetime import date

from skas_algo.engine.options.charges import ChargeModel
from skas_algo.engine.overrides import OverrideRule
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.registry import get_strategy


def seed_state_from_backtest(config, loader, *, end_date: date) -> dict:
    """Replay ``config``'s strategy from ``warm_from_date`` → ``end_date`` and return
    ``{"state", "transactions", "history"}`` to load into a LiveSession before going live.

    ``state`` is the LiveSession state (portfolio + strategy + overrides + month) — the final
    open book becomes the live starting position. ``transactions``/``history`` carry the
    replay's trades + equity curve so a run that booked (and is now flat) still shows its
    realized P&L and trade log instead of looking like an empty deployment."""
    from skas_algo.data.provider import get_data_cache
    from skas_algo.data.synthetic_options import build_synthetic_options_run, is_synthetic

    start = config.warm_from_date
    if start is None or start >= end_date:
        raise ValueError("warm_from_date must be a past date")
    underlying = (config.underlying or (config.symbols[0] if config.symbols else "NIFTY")).upper()

    reserved = {"universe", "initial_capital", "start_date", "end_date"}
    strategy_params = {k: v for k, v in config.params.items() if k not in reserved}
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

    overrides = [OverrideRule(scope=o.scope, target=o.target, rule=o.rule) for o in config.overrides]
    runner = BacktestRunner(
        strategy=strategy, universe=[underlying], loader=loader,
        initial_capital=config.capital, lookback=config.lookback,
        tax_rate=0.0, withdrawal_rate=config.withdrawal_rate, overrides=overrides,
        market_view=mv, settler=settler, margin_model=margin, charge_model=ChargeModel(),
    )
    result = runner.run(start, end_date)

    state = {
        "portfolio": result.portfolio.export_state(),
        "stops": [],  # option ratio strategies use MTM exits, not the StopBook
        "strategy": (
            runner.strategy.export_state() if hasattr(runner.strategy, "export_state") else {}
        ),
        "overrides": [
            {"scope": o.scope, "target": o.target, "rule": o.rule, "active": True}
            for o in overrides
        ],
        "current_month": [end_date.year, end_date.month],
    }
    return {"state": state, "transactions": result.transactions, "history": result.history}
