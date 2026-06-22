"""Live run manager: owns running paper/live sessions and drives them.

Each LiveRun wraps a LiveSession + a QuoteSource + DB persistence + a broadcast bus.
The sync methods (refresh / run_decision / end_day / stop) are the tested, reliable
path and are also exposed via REST for manual control. An optional async loop drives
periodic quote refresh and a once-daily decision near the close.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time

from skas_algo.db.base import session_scope
from skas_algo.db.models import AlgoRun
from skas_algo.engine.jsonutil import to_native
from skas_algo.engine.live import LiveSession
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.overrides import OverrideRule
from skas_algo.engine.report import build_report
from skas_algo.engine.runner import RunResult
from skas_algo.strategies.registry import get_strategy

from .persistence import (
    finalize_live_run,
    persist_state,
    record_greeks,
    record_trades,
    start_live_run,
    sync_positions,
)
from .quotes import IST, QuoteSource, is_market_open, warmup_history

logger = logging.getLogger("skas_algo.live")


# Deploy/backtest bookkeeping that lives in a run's persisted params but is NOT a strategy
# constructor arg (universe/capital are passed explicitly). Stripped before building a strategy.
_BOOKKEEPING_PARAM_KEYS = {
    "universe", "initial_capital", "start_date", "end_date", "instrument_class",
    "symbols", "lookback", "tax_rate", "withdrawal_rate", "warm_from_date",
    "quote_source", "broker_account_id", "name", "notes", "batch_id",
}


def strategy_kwargs(factory, params: dict) -> dict:
    """Filter persisted params down to valid strategy constructor args.

    Forward-testing a backtest replays its *persisted* params, which include bookkeeping keys
    (instrument_class, underlying, dates, …). Strategies with **kwargs swallow extras; a strict
    constructor (e.g. SST) would raise, so for those we keep only its named parameters.
    """
    import inspect

    cleaned = {k: v for k, v in params.items() if k not in _BOOKKEEPING_PARAM_KEYS}
    init = factory.__init__ if isinstance(factory, type) else factory
    try:
        sig = inspect.signature(init)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return cleaned
    if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        return cleaned  # strategy accepts **kwargs (e.g. options strategies) → pass through
    accepted = set(sig.parameters) - {"self", "universe", "initial_capital"}
    return {k: v for k, v in cleaned.items() if k in accepted}


def _quote_error_message(exc: Exception) -> str:
    """A short, user-facing reason for a failed live-quote fetch."""
    msg = str(exc)
    if "access_token" in msg or "api_key" in msg or exc.__class__.__name__ == "TokenException":
        return "Zerodha session rejected — log in again on Brokers, then Reconnect quotes."
    return f"Live quotes unavailable: {msg[:160]}"


@dataclass
class LiveConfig:
    name: str
    strategy_id: str
    symbols: list[str]
    notes: str | None = None
    capital: float = 2_500_000
    instrument_class: str = "STOCK"   # "STOCK" | "DERIV" (options)
    underlying: str | None = None     # DERIV: NIFTY/BANKNIFTY (option underlying)
    params: dict = field(default_factory=dict)
    tax_rate: float = 0.20
    withdrawal_rate: float = 0.0
    lookback: int = 20
    overrides: list[OverrideRule] = field(default_factory=list)
    excluded_symbols: list[str] = field(default_factory=list)  # blocked from new entries
    mode: str = "PAPER"
    quote_source: str = "cache"  # persisted so the run can be rebuilt after a restart
    broker_account_id: int | None = None
    refresh_seconds: int = 30
    decision_time: str = "15:20"  # IST; daily decision fires at/after this
    ignore_market_hours: bool = False
    auto: bool = False  # whether the background refresh/decision loop runs
    # Options PAPER only: replay from this past date as a backtest, then continue live.
    warm_from_date: "date | None" = None


def _serialize_event(ev: dict) -> dict:
    out = to_native(dict(ev))
    dt = ev["date"]
    out["date"] = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)
    return out


def _build_session(config: "LiveConfig", strategy, loader, is_deriv: bool, underlying: str) -> LiveSession:
    """A LiveSession wired for the deployment's instrument class. DERIV builds the live
    options stack (chain/lazy-marks/settler/charges/margin); STOCK is the Donchian view."""
    common = dict(
        initial_capital=config.capital, lookback=config.lookback, tax_rate=config.tax_rate,
        withdrawal_rate=config.withdrawal_rate, overrides=config.overrides,
        excluded_symbols=config.excluded_symbols,
    )
    if is_deriv:
        from skas_algo.data.options_provider import build_live_options_run
        from skas_algo.data.provider import get_data_cache
        from skas_algo.engine.options.charges import ChargeModel

        mv, _chain, settler, margin = build_live_options_run(
            get_data_cache(), underlying,
            lot_overrides=config.params.get("contract_specs"), now=datetime.now(IST),
        )
        return LiveSession(strategy, market_view=mv, settler=settler,
                           charge_model=ChargeModel(), margin_model=margin, **common)
    session = LiveSession(strategy, **common)
    session.warmup(warmup_history(loader, config.symbols, config.lookback))
    _seed_supertrend(session, strategy, loader, config.symbols)
    return session


def _seed_supertrend(session, strategy, loader, symbols) -> None:
    """For a SuperTrend strategy, compute each symbol's latest completed-bar direction from the
    cached OHLC and set it on the live view (live quotes carry no high/low, so ATR comes from the
    cache). Refreshed daily by the run loop. No-op for other strategies."""
    if not getattr(strategy, "needs_supertrend", False) or not hasattr(strategy, "supertrend_config"):
        return
    market = getattr(session, "market", None)
    if market is None or not hasattr(market, "set_supertrend_dir"):
        return
    from datetime import timedelta

    import pandas as pd

    from skas_algo.engine.indicators.supertrend import supertrend_bands

    cfg = strategy.supertrend_config()
    today = datetime.now(IST).date()
    start = today - timedelta(days=1500)  # ~4y → enough for a monthly ATR window
    for sym in symbols:
        try:
            df = loader(sym, start, today)
        except Exception:  # pragma: no cover - missing cache → no signal
            df = None
        if df is None or getattr(df, "empty", True):
            market.set_supertrend_dir(sym, None)
            continue
        # Latest completed bar's direction (+1/−1) AND the trailing SuperTrend line — the line
        # lets the watchlist show each name's trend + distance-to-flip.
        bands = supertrend_bands(
            df, period=cfg["period"], multiplier=cfg["multiplier"], timeframe=cfg["timeframe"]
        ).dropna(subset=["direction"])
        if len(bands):
            last = bands.iloc[-1]
            line = last["supertrend"]
            market.set_supertrend_dir(
                sym, float(last["direction"]),
                float(line) if pd.notna(line) else None,
            )
        else:
            market.set_supertrend_dir(sym, None)


class Broadcaster:
    """Tiny pub/sub over asyncio queues for WebSocket fan-out (single-user)."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, message: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(to_native(message))
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                pass


class LiveRun:
    def __init__(self, run_id, algo_id, config, session, quote_source, broadcaster):
        self.run_id = run_id
        self.algo_id = algo_id
        self.config: LiveConfig = config
        self.session: LiveSession = session
        self.quote_source: QuoteSource = quote_source
        self.broadcaster: Broadcaster = broadcaster
        # True when the run wants Zerodha live quotes but is degraded to cache (e.g.
        # recovered while logged out). A later login can promote it back to live.
        self.on_cache_fallback = False
        # Last live-quote fetch error (e.g. a rejected Zerodha token), surfaced in the
        # snapshot so the UI can flag "session expired — reconnect" instead of failing silently.
        self.quote_error: str | None = None
        # Last self-heal retry of a stuck (quote_error'd) zerodha run — throttles the loop's
        # rebuild-and-repoll to ~once a minute so it doesn't hammer a rate-limited/dead token.
        self._last_quote_retry: datetime | None = None
        self.last_decision_day = None
        self.status = "running"
        # Greeks history is sampled (~1/min), not every refresh tick → keep a day's
        # forward-test to a few hundred rows.
        self._last_greeks_at: datetime | None = None
        # Real Zerodha basket margin, refreshed ~1/min (overrides the model estimate).
        self._margin: float | None = None
        self._last_margin_at: datetime | None = None
        # Let the options view fetch a freshly-selected contract's live price at fill time
        # (follows quote-source promotion since it reads self.quote_source each call).
        market = getattr(self.session, "market", None)
        if market is not None and hasattr(market, "set_quote_fn"):
            market.set_quote_fn(lambda syms: self.quote_source.get_quotes(syms))

    # ----------------------------------------------------------- actions
    def _quote_symbols(self) -> list[str]:
        """Symbols to pull live quotes for. Equity: the fixed universe. Options: the open
        contract legs (dynamic) — there's no fixed option universe, and pre-entry strike
        selection reads the cache chain, not live quotes."""
        if self.config.instrument_class.upper() == "DERIV":
            return self.session.portfolio.lot_symbols()
        return self.config.symbols

    def refresh(self) -> dict:
        """Pull quotes, mark-to-market, persist positions, broadcast snapshot."""
        symbols = self._quote_symbols()
        idx = None
        if self.config.instrument_class.upper() == "DERIV" and self.config.underlying:
            from skas_algo.data.options_provider import INDEX_SYMBOL
            # Index → its index symbol; a stock F&O underlying → the stock itself, so spot bands
            # and strike selection follow the live underlying price for either.
            idx = INDEX_SYMBOL.get(self.config.underlying.upper()) or self.config.underlying.upper()
            symbols = symbols + [idx]
        try:
            quotes = self.quote_source.get_quotes(symbols) if symbols else {}
            self.quote_error = None
        except Exception as exc:  # e.g. a rejected Zerodha token — don't 500 / crash the loop
            self.quote_error = _quote_error_message(exc)
            logger.warning("quote fetch failed for run %s: %s", self.run_id, exc)
            quotes = {}
        if idx and idx in quotes and hasattr(self.session.market, "set_index_spot"):
            self.session.market.set_index_spot(self.config.underlying, quotes.pop(idx))
        self.session.update_quotes(quotes)
        self._maybe_refresh_margin()
        snap = self.snapshot()
        with session_scope() as db:
            sync_positions(db, self.algo_id, snap)
            self._maybe_record_greeks(db, snap)
        self.broadcaster.publish({"type": "snapshot", "run_id": self.run_id, **snap})
        self._persist_state()
        return snap

    def _maybe_refresh_margin(self) -> None:
        """Throttled (~1/min) real Zerodha basket margin, built from our own legs. Falls
        back silently to the model estimate (in the session snapshot) when unavailable."""
        if self.config.instrument_class.upper() != "DERIV" or self.config.quote_source != "zerodha":
            return
        symbols = self.session.portfolio.lot_symbols()
        if not symbols:
            self._margin = None
            return
        now = datetime.now(IST)
        if self._last_margin_at and (now - self._last_margin_at).total_seconds() < 60:
            return
        self._last_margin_at = now
        adapter = getattr(self.quote_source, "adapter", None)
        if adapter is None or not hasattr(adapter, "basket_margin"):
            return
        legs = [
            {
                "symbol": s,
                "direction": self.session.portfolio.lots(s)[0].direction,
                "units": sum(lot.units for lot in self.session.portfolio.lots(s)),
            }
            for s in symbols
        ]
        try:
            m = adapter.basket_margin(legs)
        except Exception:  # pragma: no cover - never break the loop on a margin call
            m = None
        if m is not None:
            self._margin = m
            # Let the strategy's %-of-margin profit/stop targets apply to the real basket margin.
            self.session.set_margin_override(m)

    def _maybe_record_greeks(self, db, snap: dict) -> None:
        """Sample the deployment's live greeks to history at most once a minute."""
        if snap.get("net_delta") is None:
            return  # equity run / no priceable option legs
        now = datetime.now(IST)
        if self._last_greeks_at and (now - self._last_greeks_at).total_seconds() < 60:
            return
        self._last_greeks_at = now
        record_greeks(db, self.run_id, snap, now, spot=self._underlying_spot())

    def _refresh_supertrend(self) -> None:
        """Recompute SuperTrend from the cached OHLC before a decision (the latest completed
        bar). Cheap and once-per-decision; no-op for non-SuperTrend strategies."""
        strategy = getattr(self.session, "strategy", None)
        if not getattr(strategy, "needs_supertrend", False):
            return
        try:
            from skas_algo.data.provider import get_price_loader

            _seed_supertrend(self.session, strategy, get_price_loader(), self.config.symbols)
        except Exception:  # pragma: no cover - never break the decision loop on a cache hiccup
            logger.exception("supertrend refresh failed for run %s", self.run_id)

    def run_decision(self, ts: datetime | None = None) -> list[dict]:
        """Make today's entry/exit decision; persist trades + positions; broadcast."""
        ts = ts or datetime.now(IST)
        self._refresh_supertrend()
        events = self.session.run_decision(ts)
        snap = self.snapshot()  # wrapper: real margin override + greeks + target/stop, etc.
        with session_scope() as db:
            if events:
                record_trades(db, self.algo_id, events)
            sync_positions(db, self.algo_id, snap)
        if events:
            self.broadcaster.publish(
                {
                    "type": "trades",
                    "run_id": self.run_id,
                    "events": [_serialize_event(e) for e in events],
                }
            )
        self.broadcaster.publish({"type": "snapshot", "run_id": self.run_id, **snap})
        self._persist_state()
        return events

    def flatten(self) -> list[dict]:
        """Exit-all: close every open leg now; persist trades + positions; broadcast."""
        events = self.session.flatten(datetime.now(IST))
        self._after_manual(events)
        return events

    def manual_order(self, *, closes=None, opens=None) -> list[dict]:
        """Option-aware intervention: close selected legs/lots and/or open new legs now."""
        events = self.session.manual_order(datetime.now(IST), closes=closes, opens=opens)
        self._after_manual(events)
        return events

    def _after_manual(self, events: list[dict]) -> None:
        """Persist + broadcast after a manual flatten/order (mirrors run_decision)."""
        self._maybe_refresh_margin()
        snap = self.snapshot()
        with session_scope() as db:
            if events:
                record_trades(db, self.algo_id, events)
            sync_positions(db, self.algo_id, snap)
        if events:
            self.broadcaster.publish(
                {
                    "type": "trades",
                    "run_id": self.run_id,
                    "events": [_serialize_event(e) for e in events],
                }
            )
        self.broadcaster.publish({"type": "snapshot", "run_id": self.run_id, **snap})
        self._persist_state()

    def end_day(self) -> None:
        self.session.end_day()
        self._persist_state()

    def stop(self) -> None:
        self.status = "stopped"
        self._persist_state()  # snapshot the final (flat) book so Activate can resume from it
        rr = RunResult(
            history=self.session.history,
            transactions=self.session.transactions,
            monthly_flush_log=self.session.monthly_flush_log,
            portfolio=self.session.portfolio,
        )
        strategy = getattr(self.session, "strategy", None)
        want_deployed = getattr(strategy, "report_deployed_metrics", False)
        report = build_report(
            rr, self.config.capital,
            deployed_metrics=want_deployed,
            idle_return=getattr(strategy, "idle_return", 0.06) if want_deployed else 0.0,
        )
        with session_scope() as db:
            run = db.get(AlgoRun, self.run_id)
            if run is not None:
                finalize_live_run(
                    db,
                    run,
                    metrics=report,
                    trade_log=[_serialize_event(t) for t in self.session.transactions],
                )
        self.broadcaster.publish({"type": "stopped", "run_id": self.run_id})

    def snapshot(self) -> dict:
        snap = {
            "run_id": self.run_id,
            "status": self.status,
            "name": self.config.name,
            "strategy_id": self.config.strategy_id,
            "instrument_class": self.config.instrument_class,
            "underlying": self.config.underlying,
            "quote_source": self.config.quote_source,
            "on_cache_fallback": self.on_cache_fallback,
            "quote_error": self.quote_error,
            "parts_total": self.config.params.get("capital_parts"),
            # Options deployments expose lot-sets (editable live while flat); equity
            # strategies have no `lots` attr → null → the UI hides the control.
            "lots": getattr(getattr(self.session, "strategy", None), "lots", None),
            # Live underlying spot (for the positions payoff diagram), if known.
            "underlying_spot": self._underlying_spot(),
            # Live controls + exclusion editing surface for the UI.
            "auto": self.config.auto,
            "ignore_market_hours": self.config.ignore_market_hours,
            "refresh_seconds": self.config.refresh_seconds,
            "decision_time": self.config.decision_time,
            "universe": list(self.config.symbols),
            "excluded_symbols": self.session.excluded_symbols,
            **self.session.snapshot(),
        }
        # Prefer the real Zerodha basket margin (throttled) over the model estimate.
        if self._margin is not None:
            snap["margin_used"] = self._margin
            snap["margin_source"] = "zerodha"
        return to_native(snap)

    def _underlying_spot(self):
        market = getattr(self.session, "market", None)
        if self.config.underlying and market is not None and hasattr(market, "index_spot"):
            return market.index_spot(self.config.underlying)
        return None

    def export_state(self) -> dict:
        return {
            **self.session.export_state(),
            "last_decision_day": (
                self.last_decision_day.isoformat() if self.last_decision_day else None
            ),
        }

    def _persist_state(self) -> None:
        try:
            with session_scope() as db:
                persist_state(db, self.run_id, to_native(self.export_state()))
        except Exception:  # pragma: no cover - persistence must never break the loop
            logger.exception("failed to persist state for run %s", self.run_id)


class LiveRunManager:
    def __init__(self) -> None:
        self.runs: dict[int, LiveRun] = {}
        self.broadcaster = Broadcaster()
        self._tasks: dict[int, asyncio.Task] = {}

    def start(self, config: LiveConfig, loader: PriceLoader, quote_source: QuoteSource) -> LiveRun:
        factory = get_strategy(config.strategy_id)
        # `universe`/`initial_capital` are passed explicitly; the run's params also carry deploy/
        # backtest bookkeeping (instrument_class, dates, …) that strict constructors reject.
        strategy_params = strategy_kwargs(factory, config.params)
        is_deriv = config.instrument_class.upper() == "DERIV"
        underlying = (config.underlying or (config.symbols[0] if config.symbols else "NIFTY")).upper()
        strategy = factory(
            universe=[underlying] if is_deriv else config.symbols,
            initial_capital=config.capital, **strategy_params,
        )

        # Margin guard (options): capital must fund the position's margin for the chosen
        # lot-sets, or the % profit/stop targets are nonsensical (and the broker would reject
        # the order live). Raise with a suggested capital — the route returns it as a 422.
        if is_deriv:
            mpl = getattr(strategy, "margin_per_lotset", None)
            lots = int(getattr(strategy, "lots", 1) or 1)
            if mpl:
                required = mpl * lots
                if config.capital < required:
                    import math

                    suggested = int(math.ceil(required / 50_000.0) * 50_000)
                    raise ValueError(
                        f"Capital ₹{config.capital:,.0f} is below the ~₹{required:,.0f} margin "
                        f"needed for {lots} lot-set(s) of {config.strategy_id}. "
                        f"Deploy with at least ₹{suggested:,.0f}, or reduce the lot-sets."
                    )

        session = _build_session(config, strategy, loader, is_deriv, underlying)

        # Backtest-then-forward seed (PAPER, equity or options): replay from a past date and
        # carry the resulting open book + strategy state forward as the live starting position.
        # The replay's trades/equity curve are carried too, so a seeded run that already booked
        # (and is now flat) still shows its realized P&L + trade log, not an empty deployment.
        if config.warm_from_date:
            from skas_algo.live.seed import seed_state_from_backtest

            seeded = seed_state_from_backtest(config, loader, end_date=date.today())
            session.load_state(seeded["state"])
            session.transactions = list(seeded.get("transactions", []))
            session.history = list(seeded.get("history", []))

        params_snapshot = {
            "symbols": config.symbols,
            "instrument_class": config.instrument_class,
            "underlying": underlying if is_deriv else None,
            "warm_from_date": (
                config.warm_from_date.isoformat() if config.warm_from_date else None
            ),
            "lookback": config.lookback,
            "tax_rate": config.tax_rate,
            "withdrawal_rate": config.withdrawal_rate,
            "quote_source": config.quote_source,
            "broker_account_id": config.broker_account_id,
            "auto": config.auto,
            "refresh_seconds": config.refresh_seconds,
            "decision_time": config.decision_time,
            "ignore_market_hours": config.ignore_market_hours,
            "excluded_symbols": config.excluded_symbols,
            **config.params,
        }
        with session_scope() as db:
            run = start_live_run(
                db,
                name=config.name,
                strategy_id=config.strategy_id,
                capital=config.capital,
                mode=config.mode,
                params=params_snapshot,
                notes=config.notes,
            )
            run_id, algo_id = run.id, run.algo_id

        live = LiveRun(run_id, algo_id, config, session, quote_source, self.broadcaster)
        self.runs[run_id] = live
        live._persist_state()  # initial snapshot so a restart can recover it immediately
        return live

    def register(self, live: LiveRun) -> None:
        """Register a run rebuilt by recovery (already has its DB row + state)."""
        self.runs[live.run_id] = live

    def get(self, run_id: int) -> LiveRun | None:
        return self.runs.get(run_id)

    def list(self) -> list[LiveRun]:
        return list(self.runs.values())

    def stop(self, run_id: int) -> LiveRun | None:
        task = self._tasks.pop(run_id, None)
        if task is not None:
            task.cancel()
        live = self.runs.pop(run_id, None)
        if live is not None:
            live.stop()
        return live

    def update_controls(
        self,
        run_id: int,
        *,
        auto: bool | None = None,
        ignore_market_hours: bool | None = None,
        refresh_seconds: int | None = None,
        excluded_symbols: list[str] | None = None,
        lots: int | None = None,
    ) -> LiveRun:
        """Mutate a running deployment's loop controls / exclusion list / lot-sets, in place.

        Applies to the in-memory run immediately (the loop reads config each tick),
        toggles the background loop on/off to match ``auto``, and persists the new
        values into the run's params_snapshot so a restart recovers them. A ``lots`` change
        takes effect on the strategy's NEXT entry (it doesn't resize open legs).
        """
        live = self.runs[run_id]
        cfg = live.config
        if ignore_market_hours is not None:
            cfg.ignore_market_hours = ignore_market_hours
        if refresh_seconds is not None:
            cfg.refresh_seconds = max(5, int(refresh_seconds))
        if excluded_symbols is not None:
            live.session.set_excluded(excluded_symbols)
            cfg.excluded_symbols = live.session.excluded_symbols
        if lots is not None and hasattr(live.session.strategy, "lots"):
            live.session.strategy.lots = max(1, int(lots))
            cfg.params = {**cfg.params, "lots": live.session.strategy.lots}
        if auto is not None:
            cfg.auto = auto
            running = run_id in self._tasks and not self._tasks[run_id].done()
            if auto and not running:
                self.start_loop(run_id)
            elif not auto and running:
                self._tasks.pop(run_id).cancel()

        with session_scope() as db:
            run = db.get(AlgoRun, run_id)
            if run is not None:
                snap = dict(run.params_snapshot or {})
                snap.update(
                    auto=cfg.auto,
                    ignore_market_hours=cfg.ignore_market_hours,
                    refresh_seconds=cfg.refresh_seconds,
                    excluded_symbols=cfg.excluded_symbols,
                )
                if lots is not None and hasattr(live.session.strategy, "lots"):
                    snap["lots"] = live.session.strategy.lots
                run.params_snapshot = snap
        self.broadcaster.publish({"type": "snapshot", "run_id": run_id, **live.snapshot()})
        return live

    def _retry_quotes(self, live: "LiveRun") -> bool:
        """Self-heal a zerodha run stuck on a quote_error: if a VALID session exists (honest
        expiry → False once the token truly dies, so we never hammer a dead token), rebuild the
        adapter from the current DB token and clear the error so the loop polls again. Throttled
        to ~once/minute. Recovers a re-login (and transient rate-limits) without manual Reconnect."""
        from skas_algo.db.models import BrokerAccount
        from skas_algo.live.quotes import ZerodhaQuoteSource
        from skas_algo.services import broker as broker_svc

        if not live.config.broker_account_id:
            return False
        now = datetime.now(IST)
        if live._last_quote_retry and (now - live._last_quote_retry).total_seconds() < 60:
            return False
        live._last_quote_retry = now
        with session_scope() as db:
            account = db.get(BrokerAccount, live.config.broker_account_id)
            if account is None or not broker_svc.has_valid_session(account):
                return False
            live.quote_source = ZerodhaQuoteSource(broker_svc.make_adapter(account))
        live.on_cache_fallback = False
        live.quote_error = None
        return True

    def promote_quote_source(self, run_id: int, db) -> bool:
        """Upgrade a cache-fallback run back to live Zerodha quotes if a session exists.

        Returns True if promoted. Used by the reconnect endpoint and auto-called when a
        broker login succeeds, so a run no longer stays stuck on cache after you log in.
        """
        live = self.runs.get(run_id)
        # Promote a run that's on cache fallback OR whose live quotes errored (rejected token):
        # rebuilding the adapter from the account picks up the fresh session after a re-login.
        if live is None or not (live.on_cache_fallback or live.quote_error) or not live.config.broker_account_id:
            return False
        from skas_algo.db.models import BrokerAccount
        from skas_algo.live.quotes import ZerodhaQuoteSource
        from skas_algo.services import broker as broker_svc

        account = db.get(BrokerAccount, live.config.broker_account_id)
        if account is None or not broker_svc.has_valid_session(account):
            return False
        live.quote_source = ZerodhaQuoteSource(broker_svc.make_adapter(account))
        live.on_cache_fallback = False
        live.quote_error = None
        self.broadcaster.publish({"type": "snapshot", "run_id": run_id, **live.snapshot()})
        return True

    def promote_account_runs(self, account_id: int, db) -> list[int]:
        """Promote every cache-fallback run on this account (called after a login)."""
        return [rid for rid, live in self.runs.items()
                if (live.on_cache_fallback or live.quote_error)
                and live.config.broker_account_id == account_id
                and self.promote_quote_source(rid, db)]

    # ----------------------------------------------------- async driver
    def start_loop(self, run_id: int) -> None:
        """Kick off the background refresh/decision loop (call from an event loop)."""
        live = self.runs[run_id]
        self._tasks[run_id] = asyncio.create_task(self._loop(live))

    async def _loop(self, live: LiveRun) -> None:
        try:
            is_deriv = live.config.instrument_class.upper() == "DERIV"
            # Tick-driven runs decide every refresh (options, and custom equity trades whose
            # GTT trigger / stop / trailing must react intraday). Plain equity decides once a day.
            strategy = getattr(live.session, "strategy", None)
            tick_driven = is_deriv or getattr(strategy, "intraday", False)
            decide_at = time.fromisoformat(live.config.decision_time)
            while True:
                # Auto-refresh only during market hours (Mon–Fri 09:15–15:30 IST). A
                # live (Zerodha) deployment NEVER polls outside hours — pointless and it
                # hammers the broker with a stale token. ignore_market_hours still allows
                # off-hours ticks for CACHE (offline) testing only. Manual refresh/decision
                # via the REST endpoints is always available.
                market_ok = is_market_open() or (
                    live.config.ignore_market_hours and live.config.quote_source != "zerodha"
                )
                if market_ok and live.quote_error and live.config.quote_source == "zerodha":
                    # Quotes are erroring (rejected/expired token, or a transient rate-limit). Try to
                    # self-heal ~once/min: if a valid session exists (honest expiry → False once the
                    # token truly dies, so we never hammer it), rebuild the adapter from the current
                    # DB token and fall through to poll. Otherwise wait — a re-login auto-recovers
                    # within a minute, no manual Reconnect needed.
                    if not self._retry_quotes(live):
                        await asyncio.sleep(live.config.refresh_seconds)
                        continue
                if market_ok:
                    try:
                        live.refresh()
                        now = datetime.now(IST)
                        if live.quote_error:
                            # Quotes are dead (e.g. token rejected) — the snapshot already
                            # broadcast the error; never make decisions on stale/empty marks.
                            pass
                        elif tick_driven:
                            # Decide EVERY tick — the strategy's own gates decide what fires
                            # (options exit cadences; an equity trade's trigger/stop/trailing).
                            # Roll the day once after the close.
                            live.run_decision(now)
                            if now.time() >= time(15, 30) and live.last_decision_day != now.date():
                                live.end_day()
                                live.last_decision_day = now.date()
                        elif now.time() >= decide_at and live.last_decision_day != now.date():
                            live.run_decision(now)
                            live.end_day()
                            live.last_decision_day = now.date()
                    except Exception:  # pragma: no cover - keep the loop alive
                        logger.exception("live loop tick failed for run %s", live.run_id)
                await asyncio.sleep(live.config.refresh_seconds)
        except asyncio.CancelledError:  # pragma: no cover
            pass


# Process-wide singleton.
manager = LiveRunManager()
