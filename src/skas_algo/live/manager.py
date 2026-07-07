"""Live run manager: owns running paper/live sessions and drives them.

Each LiveRun wraps a LiveSession + a QuoteSource + DB persistence + a broadcast bus.
The sync methods (refresh / run_decision / end_day / stop) are the tested, reliable
path and are also exposed via REST for manual control. An optional async loop drives
periodic quote refresh and a once-daily decision near the close.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
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
from .quotes import IST, QuoteSource, is_broker_source, is_market_open, warmup_history

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
    # Keep the time for live (datetime) fills so the UI shows WHEN a leg was entered/exited; a
    # backtest stamps a plain date (no intraday time) → date-only, unchanged.
    if isinstance(dt, datetime):
        out["date"] = dt.strftime("%Y-%m-%d %H:%M")
    else:
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
    """Tiny pub/sub over asyncio queues for WebSocket fan-out (single-user).

    ``publish`` may be called from worker THREADS (background recovery/promotion,
    threadpool routes) — asyncio queues are not thread-safe, so off-loop publishes
    hop onto the serving loop via ``call_soon_threadsafe`` (loop attached at startup)."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None  # attached in app lifespan

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, message: dict) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is None and self.loop is not None:
            self.loop.call_soon_threadsafe(self._publish_on_loop, message)
            return
        self._publish_on_loop(message)

    def _publish_on_loop(self, message: dict) -> None:
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
        # A REAL order failed (rejected/unfillable) or the broker book mismatched — halts
        # decisions until the owner acknowledges (POST /live/{id}/ack-order-error).
        self.order_error: str | None = None
        # Last self-heal retry of a stuck (quote_error'd) zerodha run — throttles the loop's
        # rebuild-and-repoll to ~once a minute so it doesn't hammer a rate-limited/dead token.
        self._last_quote_retry: datetime | None = None
        # Throttle for OFF-HOURS mark refreshes (post-market re-pricing so unrealized P&L stays
        # correct after the close). None → re-price on the next tick (set after a login/self-heal).
        self._last_offhours_refresh: datetime | None = None
        self.last_decision_day = None
        self.status = "running"
        # Greeks history is sampled (~1/min), not every refresh tick → keep a day's
        # forward-test to a few hundred rows.
        self._last_greeks_at: datetime | None = None
        # Real Zerodha basket margin, refreshed ~1/min (overrides the model estimate).
        self._margin: float | None = None
        self._last_margin_at: datetime | None = None
        self._wire_quote_source()

    def _wire_quote_source(self) -> None:
        """Point the options market view at the current quote source: live marks (quote_fn) AND,
        when a Zerodha adapter is present, the live broker chain for strike/expiry selection (so a
        live deployment doesn't depend on the stale bhavcopy cache). Re-called after a re-login
        rebuilds the adapter. No-op for equity views."""
        market = getattr(self.session, "market", None)
        if market is None:
            return
        if hasattr(market, "set_quote_fn"):
            market.set_quote_fn(lambda syms: self.quote_source.get_quotes(syms))
        if hasattr(market, "set_chain_fn"):
            # Live full-chain lookup for ANY underlying (donchian's 30Δ flip strike selection).
            adapter = getattr(self.quote_source, "adapter", None)
            market.set_chain_fn(
                (lambda u, e: adapter.live_option_chain(u, e)) if adapter is not None else None
            )
        if hasattr(market, "set_chain_adapter"):
            adapter = getattr(self.quote_source, "adapter", None)
            market.set_chain_adapter(
                adapter, self.config.underlying, self.config.params.get("contract_specs")
            )
        # Intraday strategies (momentum_theta) warm their self-built candles from the
        # broker's historical bars — strategy-side idempotent, so the re-login path that
        # re-runs this wiring doesn't double-seed. Cache-source runs cold-start instead.
        strategy = getattr(self.session, "strategy", None)
        bars_hook = getattr(strategy, "set_daily_bars_fn", None)
        if bars_hook is not None:
            # 21_ema_momentum: daily OHLC series INCLUDING today's forming bar (chart-at-
            # 15:20 semantics) — cache through yesterday + today's intraday H/L from the
            # broker (fallback: H=L=C=LTP when no session; bands read slightly tight).
            from skas_algo.data.options_provider import INDEX_SYMBOL
            from skas_algo.data.provider import get_price_loader

            cache_loader = get_price_loader()

            def _daily_bars_live(u: str, start, end):
                import pandas as _pd

                sym = INDEX_SYMBOL.get(u.upper()) or u.upper()
                df = cache_loader(sym, start, end)
                today = date.today()
                if end < today or (df is not None and len(df) and
                                   _pd.to_datetime(df["date"]).dt.date.max() >= today):
                    return df
                row = None
                bars_fn = getattr(getattr(self.quote_source, "adapter", None),
                                  "intraday_bars", None)
                if bars_fn is not None:
                    try:
                        intra = [b for b in bars_fn(u, 1) or []
                                 if str(b["start"])[:10] == today.isoformat()]
                    except Exception:  # pragma: no cover - fall to the LTP stub
                        intra = []
                    if intra:
                        row = {"date": today,
                               "high": max(b["high"] for b in intra),
                               "low": min(b["low"] for b in intra),
                               "close": intra[-1]["close"]}
                if row is None:
                    ltp_fn = getattr(self.session.market, "index_spot", None)
                    ltp = ltp_fn(u.upper()) if ltp_fn else None
                    if ltp:
                        row = {"date": today, "high": ltp, "low": ltp, "close": ltp}
                if row is None:
                    return df
                add = _pd.DataFrame([row])
                return add if df is None or not len(df) else _pd.concat(
                    [df, add], ignore_index=True)

            bars_hook(_daily_bars_live)
        ohlc_fn = getattr(strategy, "set_daily_ohlc_fn", None)
        if ohlc_fn is not None:
            from skas_algo.data.options_provider import INDEX_SYMBOL
            from skas_algo.data.provider import get_price_loader

            loader = get_price_loader()

            def _prior_day_ohlc(u: str, today):
                from datetime import timedelta as _td

                sym = INDEX_SYMBOL.get(u.upper()) or u.upper()
                df = loader(sym, today - _td(days=14), today - _td(days=1))
                if df is None or len(df) == 0:
                    return None  # e.g. SENSEX — no cached series → bar-derived fallback
                row = df.iloc[-1]
                return {"high": float(row["high"]), "low": float(row["low"]),
                        "close": float(row["close"])}

            ohlc_fn(_prior_day_ohlc)
        seed_fn = getattr(strategy, "seed_intraday_bars", None)
        bars_fn = getattr(getattr(self.quote_source, "adapter", None), "intraday_bars", None)
        if seed_fn is not None and bars_fn is not None:
            try:
                seed_fn(bars_fn)
            except Exception:  # pragma: no cover - warmup must never block a deploy
                logger.exception("intraday warmup seed failed for run %s", self.run_id)

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
        # Underlyings whose live spot we feed → set_index_spot(name, price). Usually just the
        # deployment's own underlying; a basket strategy (e.g. donchian_strangle_monthly) exposes
        # spot_symbols() so every name's spot drives its breach checks + sizing. Each maps to its
        # index series (NIFTY → "NIFTY 50") or, for a stock F&O underlying, the stock itself.
        spot_keys: dict[str, str] = {}
        if self.config.instrument_class.upper() == "DERIV":
            from skas_algo.data.options_provider import INDEX_SYMBOL
            names: set[str] = set()
            if self.config.underlying:
                names.add(self.config.underlying.upper())
            spot_fn = getattr(getattr(self.session, "strategy", None), "spot_symbols", None)
            if spot_fn is not None:
                try:
                    names.update(str(n).upper() for n in spot_fn())
                except Exception:  # pragma: no cover - never break the loop on a strategy quirk
                    pass
            spot_keys = {n: (INDEX_SYMBOL.get(n) or n) for n in names}
            symbols = symbols + list(spot_keys.values())
        try:
            quotes = self.quote_source.get_quotes(symbols) if symbols else {}
            self.quote_error = None
        except Exception as exc:  # e.g. a rejected Zerodha token — don't 500 / crash the loop
            self.quote_error = _quote_error_message(exc)
            logger.warning("quote fetch failed for run %s: %s", self.run_id, exc)
            quotes = {}
        if spot_keys and hasattr(self.session.market, "set_index_spot"):
            for name, key in spot_keys.items():
                if key in quotes:
                    self.session.market.set_index_spot(name, quotes.pop(key))
        self.session.update_quotes(quotes)
        self._maybe_refresh_margin()
        self._maybe_reconcile()
        snap = self.snapshot()
        with session_scope() as db:
            sync_positions(db, self.algo_id, snap)
            self._maybe_record_greeks(db, snap)
        self.broadcaster.publish({"type": "snapshot", "run_id": self.run_id, **snap})
        self._persist_state()
        return snap

    def _maybe_reconcile(self) -> None:
        """Real-order runs only: hourly, compare the broker's net book with the aggregate
        of ALL live-order runs on this account; mismatch → order_error halt (owner acks
        after reviewing). Covers manual trades in the account, missed fills, and drift."""
        from skas_algo.brokers.live_broker import LiveBroker

        if not isinstance(getattr(self.session, "broker", None), LiveBroker):
            return
        now = datetime.now(IST)
        last = getattr(self, "_last_reconcile_at", None)
        if last is not None and (now - last).total_seconds() < 3600:
            return
        self._last_reconcile_at = now
        adapter = getattr(self.quote_source, "adapter", None)
        if adapter is None or self.config.broker_account_id is None:
            return
        try:
            problem = manager.reconcile_account_book(self.config.broker_account_id, adapter)
        except Exception:  # pragma: no cover - reconciliation must never kill the loop
            logger.exception("reconciliation failed for run %s", self.run_id)
            return
        if problem and not self.order_error:
            self.order_error = f"book mismatch: {problem}"
            logger.error("run %s halted on reconciliation: %s", self.run_id, problem)
            try:
                from skas_algo.notify import Alert, AlertLevel, build_notifier

                build_notifier().send(Alert(
                    f"BOOK MISMATCH: {self.config.name}", problem, AlertLevel.ERROR))
            except Exception:  # pragma: no cover
                pass

    def _maybe_refresh_margin(self) -> None:
        """Throttled (~1/min) real Zerodha basket margin, built from our own legs. Falls
        back silently to the model estimate (in the session snapshot) when unavailable."""
        if self.config.instrument_class.upper() != "DERIV" or not is_broker_source(
                self.config.quote_source):
            return
        symbols = self.session.portfolio.lot_symbols()
        if not symbols:
            self._margin = None
            self._margin_symbols = []
            return
        now = datetime.now(IST)
        # The 1/min throttle yields to a CHANGED book (entry/roll/hedge just filled):
        # broker-margin-tracked strategies freeze their thresholds off this number, so a
        # structural change should re-base within a tick, not up to a minute later.
        same_book = sorted(symbols) == getattr(self, "_margin_symbols", [])
        if same_book and self._last_margin_at and (now - self._last_margin_at).total_seconds() < 60:
            return
        self._last_margin_at = now
        self._margin_symbols = sorted(symbols)
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
            # Broker-margin-tracked strategies (delta_neutral, cp_ratio_expiry) freeze
            # their rupee thresholds off THIS number — push it (owner rule: broker margin
            # only, never the model).
            push = getattr(getattr(self.session, "strategy", None), "set_broker_margin", None)
            if push is not None:
                try:
                    push(float(m))
                except Exception:  # pragma: no cover - never break the loop
                    logger.exception("set_broker_margin failed for run %s", self.run_id)

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

    def _tag_underlying_spot(self, events: list[dict]) -> None:
        """Stamp each option trade event with the underlying's live spot at execution, so the
        analysis page can mark the TRUE entry/exit spot per cycle even when the cached index
        series lags (e.g. a fresh forward-test whose dates aren't in the bhavcopy cache yet)."""
        market = getattr(self.session, "market", None)
        spot_fn = getattr(market, "index_spot", None)
        if spot_fn is None:
            return
        from skas_algo.engine.options.instrument import parse as parse_option
        for ev in events:
            if ev.get("underlying_spot") is not None:
                continue
            inst = parse_option(str(ev.get("ticker", "")))
            if inst is None:
                continue
            spot = spot_fn(inst.underlying)
            if spot is not None:
                ev["underlying_spot"] = float(spot)

    def run_decision(self, ts: datetime | None = None) -> list[dict]:
        """Make today's entry/exit decision; persist trades + positions; broadcast."""
        ts = ts or datetime.now(IST)
        self._refresh_supertrend()
        from skas_algo.brokers.live_broker import OrderExecutionError

        try:
            events = self.session.run_decision(ts)
        except OrderExecutionError as exc:
            # A real order failed mid-decision. Whatever DID fill is already in the book
            # (each leg books its own Fill); halt further decisions until acknowledged.
            self.order_error = str(exc)
            logger.error("run %s halted on order failure: %s", self.run_id, exc)
            try:
                from skas_algo.notify import Alert, AlertLevel, build_notifier

                build_notifier().send(Alert(
                    f"ORDERS HALTED: {self.config.name}", str(exc), AlertLevel.ERROR))
            except Exception:  # pragma: no cover
                pass
            events = []
        self._tag_underlying_spot(events)
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
        self._tag_underlying_spot(events)
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
            "order_error": self.order_error,
            "supports_force_entry": hasattr(
                getattr(self.session, "strategy", None), "request_force_entry"),
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
            snap["margin_source"] = self.config.quote_source  # which broker's basket margin
        # Multi-underlying basket strategies (donchian) expose a per-name breakdown + aggregate payoff.
        basket_fn = getattr(getattr(self.session, "strategy", None), "basket_status", None)
        if basket_fn is not None:
            try:
                # Pass the best-known basket margin so the basket's stop/target amounts use the same
                # margin base the decision does (real broker margin when known, else the model estimate).
                snap["basket"] = basket_fn(
                    self.session.market, self.session.portfolio, margin=snap.get("margin_used")
                )
            except Exception:  # pragma: no cover - never break the snapshot on a monitoring quirk
                logger.exception("basket_status failed for run %s", self.run_id)
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
        # DEDICATED tick pool: every run's per-tick body (broker/cache I/O, order polling)
        # runs here via run_in_executor, isolated from the default loop executor that
        # FastAPI/anyio use for request-side work. Sized for 20+ concurrent runs (the
        # default 14 workers on this box starved ticks — 2026-07-07). Idle threads cost
        # nothing; the ceiling just caps how many ticks run truly in parallel.
        self._tick_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="tick")
        self._maint_task: asyncio.Task | None = None
        self._last_backup_day: date | None = None

    def _maybe_inject_live_broker(self, session, config: "LiveConfig", quote_source) -> None:
        """THE real-order gate. Replace the session's PaperBroker with a LiveBroker ONLY
        when every key turns: mode LIVE, account armed, SKAS_LIVE_TRADING_ENABLED, and the
        quote source's adapter exposes the full order surface. Any other combination —
        including a disarmed account on a LIVE run — keeps simulated fills (CLAUDE.md §1)."""
        if config.mode.upper() != "LIVE":
            return
        from skas_algo.config import get_settings

        settings = get_settings()
        if not settings.live_trading_enabled:
            return
        adapter = getattr(quote_source, "adapter", None)
        if adapter is None or not getattr(adapter, "armed", False):
            return
        from skas_algo.brokers.live_broker import LiveBroker, adapter_can_execute

        if not adapter_can_execute(adapter):
            return

        market = getattr(session, "market", None)

        def touch(symbol: str, side) -> float | None:
            """LIMIT price at the touch: SELL→bid / BUY→ask from the live chain book."""
            ba_fn = getattr(market, "_bid_ask", None)
            ba = ba_fn(symbol) if ba_fn is not None else None
            if not ba:
                return None
            bid, ask = ba
            from skas_algo.db.enums import OrderSide as _OS

            px = bid if side is _OS.SELL else ask
            return float(px) if px and px > 0 else None

        session.broker = LiveBroker(
            adapter,
            account_id=config.broker_account_id,
            run_name=config.name,
            touch_fn=touch,
            max_order_notional=settings.live_max_order_notional,
            max_orders_per_day=settings.live_max_orders_per_day,
            order_timeout_s=settings.live_order_timeout_s,
        )
        logger.warning("REAL-ORDER broker injected for %s (account %s)",
                       config.name, config.broker_account_id)

    def reconcile_account_book(self, account_id: int, adapter) -> str | None:
        """Compare the broker's NET positions against the AGGREGATE book of all LIVE-mode
        real-order runs on this account (the broker nets per contract across runs — a
        per-run comparison would false-alarm whenever two strategies share a strike).
        Returns a human mismatch description, or None when consistent."""
        from skas_algo.brokers.live_broker import LiveBroker
        from skas_algo.engine.options.instrument import parse

        ours: dict[str, float] = {}
        for run in self.runs.values():
            if run.config.mode.upper() != "LIVE":
                continue
            if run.config.broker_account_id != account_id:
                continue
            if not isinstance(getattr(run.session, "broker", None), LiveBroker):
                continue
            for sym in run.session.portfolio.lot_symbols():
                for lot in run.session.portfolio.lots(sym):
                    inst = parse(sym)
                    ts = None
                    if inst is not None:
                        ts = adapter._option_tradingsymbol(inst)
                    ours[ts or sym] = ours.get(ts or sym, 0.0) + lot.direction * lot.units
        try:
            broker_net = {p["tradingsymbol"]: float(p.get("quantity") or 0)
                          for p in adapter.positions()}
        except Exception as exc:  # pragma: no cover - can't reconcile → say so
            return f"positions fetch failed: {exc}"
        problems = []
        for ts, qty in ours.items():
            b = broker_net.get(ts, 0.0)
            if abs(b - qty) > 1e-6:
                problems.append(f"{ts}: platform {qty:+.0f} vs broker {b:+.0f}")
        return "; ".join(problems) if problems else None

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
            raw_lots = getattr(strategy, "lots", 1)
            # Multi-underlying strategies (momentum_theta) carry lots as a PER-NAME dict —
            # the guard's notion of "lot-sets" is the total across names.
            if isinstance(raw_lots, dict):
                raw_lots = sum(int(v or 0) for v in raw_lots.values())
            lots = int(raw_lots or 1)
            # Auto-sizing (sizing="margin") fits its lot count INTO the capital at each
            # entry, so the ``lots`` param is only a fallback — require just one lot-set.
            if getattr(strategy, "sizing", "fixed") == "margin":
                lots = 1
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
        self._maybe_inject_live_broker(session, config, quote_source)

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
        # Manual lots are a FIXED-sizing control: under sizing="margin" the strategy
        # recomputes self.lots from equity at the next entry, overwriting this value.
        # Per-underlying dict lots (momentum_theta) are NOT scalar-editable here — a
        # scalar overwrite would break the strategy's per-name lookups.
        if lots is not None and hasattr(live.session.strategy, "lots") \
                and not isinstance(live.session.strategy.lots, dict):
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
                if lots is not None and hasattr(live.session.strategy, "lots") \
                        and not isinstance(live.session.strategy.lots, dict):
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
        live._wire_quote_source()  # repoint marks + live chain at the rebuilt adapter
        live._last_offhours_refresh = None  # re-price immediately (next tick) — even off-hours
        return True

    def promote_quote_source(self, run_id: int, db, adapter=None) -> bool:
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
        # A shared ``adapter`` (one per account) avoids re-downloading the NFO/BFO
        # instruments dumps once per promoted run — the 2026-07-07 login hang.
        live.quote_source = ZerodhaQuoteSource(adapter or broker_svc.make_adapter(account))
        live.on_cache_fallback = False
        live.quote_error = None
        live._wire_quote_source()  # repoint marks + live chain at the rebuilt adapter
        live._last_offhours_refresh = None  # the loop re-prices on the next tick — even off-hours
        self.broadcaster.publish({"type": "snapshot", "run_id": run_id, **live.snapshot()})
        return True

    def promote_account_runs(self, account_id: int, db) -> list[int]:
        """Promote every cache-fallback run on this account (called after a login).
        ONE adapter is built for the account and shared by every promoted run."""
        from skas_algo.db.models import BrokerAccount
        from skas_algo.services import broker as broker_svc

        account = db.get(BrokerAccount, account_id)
        if account is None or not broker_svc.has_valid_session(account):
            return []
        adapter = broker_svc.make_adapter(account)
        return [rid for rid, live in self.runs.items()
                if (live.on_cache_fallback or live.quote_error)
                and live.config.broker_account_id == account_id
                and self.promote_quote_source(rid, db, adapter=adapter)]

    def promote_account_runs_async(self, account_id: int) -> None:
        """Fire-and-forget promotion on a daemon thread with its OWN db session.
        The login route must return the moment the token is saved — promoting ~20 runs
        does minutes of serial broker I/O (instruments dumps, per-leg quote warmups) and
        hanging the request bricked the Brokers page twice (2026-07-07). The runs' own
        1/min self-heal would recover them anyway; this just makes it immediate."""
        import threading

        def _job() -> None:
            try:
                with session_scope() as db:
                    promoted = self.promote_account_runs(account_id, db)
                if promoted:
                    logger.info("promoted %d runs after login on account %s",
                                len(promoted), account_id)
            except Exception:  # pragma: no cover - background best-effort
                logger.exception("background promotion failed for account %s", account_id)

        threading.Thread(target=_job, daemon=True).start()

    @staticmethod
    def _due_offhours_refresh(live: "LiveRun", now: datetime) -> bool:
        """Throttle OFF-HOURS mark refreshes to ~once / 5 min (None → fire now, e.g. right after a
        login or self-heal). Post-market prices are static, so this keeps unrealized P&L correct
        without hammering the broker overnight."""
        last = live._last_offhours_refresh
        if last is not None and (now - last).total_seconds() < 300:
            return False
        live._last_offhours_refresh = now
        return True

    # ----------------------------------------------------- async driver
    def start_loop(self, run_id: int) -> None:
        """Kick off the background refresh/decision loop. Thread-safe: called from the
        event loop it schedules directly; from a worker thread (background recovery)
        it hops onto the serving loop attached to the broadcaster."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = self.broadcaster.loop
            if loop is None:  # pragma: no cover - startup ordering bug
                logger.error("start_loop(%s) called with no event loop attached", run_id)
                return
            loop.call_soon_threadsafe(self._start_loop_on_loop, run_id)
            return
        self._start_loop_on_loop(run_id)

    def _start_loop_on_loop(self, run_id: int) -> None:
        live = self.runs.get(run_id)
        if live is None:  # pragma: no cover - stopped between schedule and fire
            return
        self._tasks[run_id] = asyncio.create_task(self._loop(live))

    # ------------------------------------------------- maintenance (singleton)
    def start_maintenance(self) -> None:
        """Start the singleton watchdog + daily-backup task on the running loop.
        Idempotent; called from the app lifespan after the loop is attached."""
        if self._maint_task is not None and not self._maint_task.done():
            return
        self._maint_task = asyncio.create_task(self._maintenance_loop())

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(300)
                self._watchdog_scan()
                await self._maybe_daily_backup()
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:  # pragma: no cover - maintenance must never die
                logger.exception("maintenance loop iteration failed")

    def _watchdog_scan(self) -> None:
        """Restart any AUTO run whose loop task has silently died — a dead task means no
        decisions, no stop enforcement, with nothing surfacing it. Exceptions inside a
        tick are caught and keep the loop alive; this catches the rarer case of the whole
        loop/task ending (config parse, cancellation, unexpected raise outside the guard)."""
        for run_id, live in list(self.runs.items()):
            if not live.config.auto:
                continue
            task = self._tasks.get(run_id)
            if task is None or task.done():
                logger.error("watchdog: run %s (%s) loop is dead — restarting",
                             run_id, live.config.name)
                self._notify_watchdog(live)
                self._start_loop_on_loop(run_id)

    def _notify_watchdog(self, live: "LiveRun") -> None:
        try:
            from skas_algo.notify import Alert, AlertLevel, build_notifier

            build_notifier().send(Alert(
                "Run loop restarted",
                f"[{live.config.name}] background loop had died and was restarted by the "
                f"watchdog — check for a repeating crash.",
                AlertLevel.WARNING))
        except Exception:  # pragma: no cover - alert is best-effort
            logger.exception("watchdog notification failed")

    async def _maybe_daily_backup(self) -> None:
        """One DB snapshot per day, after the session + settlement (~16:30 IST)."""
        now = datetime.now(IST)
        if now.time() < time(16, 30) or self._last_backup_day == now.date():
            return
        self._last_backup_day = now.date()
        from skas_algo.services.backup import backup_db

        await asyncio.to_thread(backup_db)

    def _tick(self, live: LiveRun, tick_driven: bool, decide_at: time) -> None:
        """One synchronous pricing/decision tick — always called via asyncio.to_thread."""
        live.refresh()
        now = datetime.now(IST)
        mkt = is_market_open()
        if mkt and not live.quote_error and not live.order_error:
            # Decisions / orders ONLY during market hours.
            if tick_driven:
                # Decide EVERY tick — the strategy's own gates decide what fires
                # (options exit cadences; an equity trade's trigger/stop/trailing).
                live.run_decision(now)
                if now.time() >= time(15, 30) and live.last_decision_day != now.date():
                    live.end_day()
                    live.last_decision_day = now.date()
            elif now.time() >= decide_at and live.last_decision_day != now.date():
                live.run_decision(now)
                live.end_day()
                live.last_decision_day = now.date()

    async def _loop(self, live: LiveRun) -> None:
        try:
            loop = asyncio.get_running_loop()
            is_deriv = live.config.instrument_class.upper() == "DERIV"
            # Tick-driven runs decide every refresh (options, and custom equity trades whose
            # GTT trigger / stop / trailing must react intraday). Plain equity decides once a day.
            strategy = getattr(live.session, "strategy", None)
            tick_driven = is_deriv or getattr(strategy, "intraday", False)
            decide_at = time.fromisoformat(live.config.decision_time)
            while True:
                now = datetime.now(IST)
                mkt = is_market_open()
                is_zerodha = is_broker_source(live.config.quote_source)  # any real-broker feed

                # Self-heal a stuck zerodha run as soon as a VALID session exists — even off-hours,
                # so a re-login recovers the "expired" badge + P&L without waiting for market open.
                # (Throttled ~1/min; honest expiry makes it a no-op once the token is truly dead, so
                # it never hammers.) On success it resets the off-hours throttle → re-prices below.
                if is_zerodha and live.quote_error:
                    self._retry_quotes(live)

                # Pull marks this tick when:
                #   • the market is open (this also drives decisions), OR
                #   • it's a zerodha run with a working session, off-hours — re-price on a slow
                #     cadence so post-market unrealized P&L reflects last-traded prices (read-only;
                #     we NEVER decide / place orders off-hours), OR
                #   • it's an off-hours CACHE run with ignore_market_hours (offline testing).
                should_price = (
                    mkt
                    or (is_zerodha and not live.quote_error and self._due_offhours_refresh(live, now))
                    or (live.config.ignore_market_hours and not is_zerodha)
                )
                if should_price:
                    try:
                        # The WHOLE tick runs in a DEDICATED worker pool: refresh/decisions do
                        # real I/O (broker calls, or per-symbol DuckDB reads on cache fallback —
                        # a 50-name basket is ~50 queries) and 20+ runs ticking these
                        # synchronously on the ONE event loop starved the entire API
                        # (2026-07-07 morning). publish/start_loop are thread-safe now.
                        await loop.run_in_executor(
                            self._tick_pool, self._tick, live, tick_driven, decide_at)
                    except Exception:  # pragma: no cover - keep the loop alive
                        logger.exception("live loop tick failed for run %s", live.run_id)
                await asyncio.sleep(live.config.refresh_seconds)
        except asyncio.CancelledError:  # pragma: no cover
            pass


# Process-wide singleton.
manager = LiveRunManager()
