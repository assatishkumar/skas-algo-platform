"""Rebuild running paper/live sessions after a restart.

A LiveSession lives in memory; a process restart (reload, crash, deploy) would lose
it. We persist each session's state to AlgoRun.state, and on startup rebuild every
run that was still running (stopped_at is null): re-create the strategy, re-warm
history from the cache, restore the saved state, reconnect a quote source, and
(if it was an auto run) resume the background loop.
"""

from __future__ import annotations

import logging
from datetime import date

from skas_algo.db.base import session_scope
from skas_algo.db.enums import TradingMode
from skas_algo.db.models import Algo, AlgoRun, BrokerAccount
from skas_algo.engine.live import LiveSession
from skas_algo.live.manager import LiveConfig, LiveRun, manager
from skas_algo.live.quotes import CacheQuoteSource, ZerodhaQuoteSource, warmup_history
from skas_algo.services import broker as broker_svc
from skas_algo.strategies.registry import get_strategy

logger = logging.getLogger("skas_algo.live")

_NON_STRATEGY_KEYS = {
    "symbols",
    "lookback",
    "tax_rate",
    "withdrawal_rate",
    "quote_source",
    "broker_account_id",
    "auto",
    "refresh_seconds",
    "decision_time",
    "ignore_market_hours",
    "excluded_symbols",
    # passed explicitly below; also backtest bookkeeping a forward-tested run carries.
    "universe",
    "initial_capital",
    "start_date",
    "end_date",
}


def recover_running_sessions() -> int:
    """Rebuild and register all still-running runs. Returns how many were recovered."""
    from skas_algo.data.provider import get_price_loader

    try:
        loader = get_price_loader()
    except Exception:  # pragma: no cover - no cache available
        logger.warning("recovery skipped: market-data cache unavailable")
        return 0

    recovered = 0
    with session_scope() as db:
        runs = (
            db.query(AlgoRun)
            .filter(AlgoRun.stopped_at.is_(None), AlgoRun.mode != TradingMode.BACKTEST)
            .all()
        )
        for run in runs:
            if run.id in manager.runs:
                continue
            try:
                _rebuild(db, run, loader)
                recovered += 1
            except Exception:
                logger.exception("could not recover live run %s", run.id)
    if recovered:
        logger.info("recovered %d running live session(s)", recovered)
    return recovered


def _rebuild(db, run: AlgoRun, loader) -> None:
    algo = db.get(Algo, run.algo_id)
    params = dict(run.params_snapshot or {})
    symbols = params.get("symbols", [])
    lookback = params.get("lookback", 20)
    strategy_params = {k: v for k, v in params.items() if k not in _NON_STRATEGY_KEYS}

    strategy = get_strategy(algo.strategy_id)(
        universe=symbols, initial_capital=algo.capital, **strategy_params
    )
    session = LiveSession(
        strategy,
        initial_capital=algo.capital,
        lookback=lookback,
        tax_rate=params.get("tax_rate", 0.20),
        withdrawal_rate=params.get("withdrawal_rate", 0.0),
        excluded_symbols=params.get("excluded_symbols", []),
    )
    session.warmup(warmup_history(loader, symbols, lookback))
    if run.state:
        session.load_state(run.state)

    config = LiveConfig(
        name=algo.name,
        strategy_id=algo.strategy_id,
        symbols=symbols,
        capital=algo.capital,
        params=strategy_params,
        tax_rate=params.get("tax_rate", 0.20),
        withdrawal_rate=params.get("withdrawal_rate", 0.0),
        lookback=lookback,
        mode=run.mode.value,
        quote_source=params.get("quote_source", "cache"),
        broker_account_id=params.get("broker_account_id"),
        refresh_seconds=params.get("refresh_seconds", 30),
        decision_time=params.get("decision_time", "15:20"),
        ignore_market_hours=params.get("ignore_market_hours", False),
        auto=params.get("auto", False),
        excluded_symbols=params.get("excluded_symbols", []),
    )

    quote_source, on_cache_fallback = _quote_source(db, config, loader)
    live = LiveRun(run.id, run.algo_id, config, session, quote_source, manager.broadcaster)
    live.on_cache_fallback = on_cache_fallback

    last = (run.state or {}).get("last_decision_day")
    if last:
        try:
            live.last_decision_day = date.fromisoformat(last)
        except ValueError:
            pass

    manager.register(live)
    if config.auto:
        manager.start_loop(run.id)


def _quote_source(db, config: LiveConfig, loader):
    """Zerodha if a valid session exists, else fall back to cache (degraded).

    Returns ``(quote_source, on_cache_fallback)`` — the flag is True when the run
    wanted Zerodha but had to degrade to cache, so a later login can promote it.
    """
    if config.quote_source == "zerodha" and config.broker_account_id:
        account = db.get(BrokerAccount, config.broker_account_id)
        if account is not None and broker_svc.has_valid_session(account):
            return ZerodhaQuoteSource(broker_svc.make_adapter(account)), False
        logger.warning(
            "run for account %s has no valid session; recovering with cache quotes",
            config.broker_account_id,
        )
        return CacheQuoteSource(loader), True
    return CacheQuoteSource(loader), False
