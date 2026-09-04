"""Live/paper run control: start, manual refresh/decision, stop, list + WebSocket.

Async endpoints call the manager directly (loop thread) so broadcasts to WebSocket
subscribers are thread-safe. The "cache" quote source works offline; "zerodha" needs
a logged-in account.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import (
    AdoptBrokerCloseInput,
    SetHoldingInput,
    DeploymentUpdate,
    GoLiveRequest,
    LiveControlsInput,
    LiveStartRequest,
    ManualOrderInput,
    OverrideInput,
    QuoteSourceInput,
    iso_utc,
)
from skas_algo.config import get_settings
from skas_algo.data import universes
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.db.enums import TradingMode
from skas_algo.db.models import Algo, AlgoRun, BrokerAccount, GreeksSnapshot, Order
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.overrides import OverrideRule
from skas_algo.live.manager import LiveConfig, manager
from skas_algo.live.quotes import CacheQuoteSource, is_broker_source
from skas_algo.services import broker as broker_svc
from skas_algo.services.runs import delete_algo_cascade

logger = logging.getLogger("skas_algo.live")

router = APIRouter(tags=["live"], prefix="/live")


_FALLBACK_DECISION_TIME = "15:20"


def _resolve_decision_time(strategy_id: str, requested: str | None) -> str:
    """Explicit wins; else the STRATEGY's own default; else the platform's 15:20.

    Strategies declare `default_decision_time` when the platform default is wrong for them —
    value_investing uses 15:05 because 15:20 falls inside the closing auction for F&O-listed
    cash stocks. Resolved HERE rather than in the model so an API caller gets it too, not
    just the deploy form.
    """
    if requested:
        return requested
    from skas_algo.strategies.registry import get_strategy

    try:
        cls = get_strategy(strategy_id)
    except Exception:
        return _FALLBACK_DECISION_TIME
    return str(getattr(cls, "default_decision_time", _FALLBACK_DECISION_TIME))
# The WebSocket lives on its OWN router registered WITHOUT the require_auth dependency: a
# browser can't attach an Authorization header to a WS, so router-level auth would reject
# every connection. It's gated inline instead (token via ?token=). See app.py.
ws_router = APIRouter(prefix="/live")


def _build_quote_source(quote_source: str, broker_account_id, loader: PriceLoader, db: Session):
    if quote_source == "cache":
        return CacheQuoteSource(loader)
    if is_broker_source(quote_source):
        if broker_account_id is None:
            raise HTTPException(
                status_code=400, detail=f"broker_account_id required for {quote_source} quotes"
            )
        account = db.get(BrokerAccount, broker_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="broker account not found")
        # The source names the broker — a "dhan" run must ride a dhan account (and vice
        # versa), or the adapter would silently speak the wrong API.
        if (account.broker or "zerodha").lower() != quote_source:
            raise HTTPException(
                status_code=400,
                detail=f"quote_source '{quote_source}' needs a {quote_source} account; "
                       f"'{account.label}' is {account.broker}",
            )
        if not broker_svc.has_valid_session(account):
            raise HTTPException(
                status_code=400,
                detail="broker account has no valid session — log in (paste token) first",
            )
        from skas_algo.live.pricefeed import build_quote_source

        return build_quote_source(account, broker_svc.make_adapter(account))
    raise HTTPException(status_code=400, detail=f"unknown quote_source '{quote_source}'")


def _quote_source(req: LiveStartRequest, loader: PriceLoader, db: Session):
    return _build_quote_source(req.quote_source, req.broker_account_id, loader, db)


def start_deployment(req: LiveStartRequest, db: Session, loader: PriceLoader, avail: set[str]):
    """Resolve symbols, build a LiveConfig, start (and optionally loop) a deployment. Shared by
    POST /live/start and the /trade/* deploy endpoints. Raises HTTPException on bad input."""
    is_deriv = req.instrument_class.upper() == "DERIV"
    underlying = (req.underlying or (req.symbols[0] if req.symbols else None))
    if is_deriv:
        if not underlying:
            raise HTTPException(status_code=422, detail="underlying required for a DERIV deployment")
        symbols = [underlying.upper()]
    else:
        symbols = universes.resolve(req.universe, avail) if req.universe else list(req.symbols)
        if not symbols:
            raise HTTPException(status_code=422, detail="symbols or a valid universe required")
        # Same union the backtest route applies: price what the strategy will trade.
        symbols = universes.with_helper_symbols(symbols, req.params or {})
    try:
        quote_source = _quote_source(req, loader, db)
        config = LiveConfig(
            name=req.name or f"{req.strategy_id} {req.mode.lower()}",
            strategy_id=req.strategy_id,
            notes=req.notes,
            symbols=symbols,
            instrument_class=req.instrument_class,
            underlying=underlying.upper() if is_deriv else None,
            capital=req.capital,
            params=req.params,
            tax_rate=req.tax_rate,
            withdrawal_rate=req.withdrawal_rate,
            lookback=req.lookback,
            overrides=[
                OverrideRule(scope=o.scope, target=o.target, rule=o.rule) for o in req.overrides
            ],
            mode=req.mode,
            quote_source=req.quote_source,
            broker_account_id=req.broker_account_id if is_broker_source(req.quote_source) else None,
            refresh_seconds=req.refresh_seconds,
            decision_time=_resolve_decision_time(req.strategy_id, req.decision_time),
            ignore_market_hours=req.ignore_market_hours,
            auto=req.auto,
            warm_from_date=req.warm_from_date,
        )
        live = manager.start(config, loader, quote_source)
    except KeyError as exc:  # unknown strategy
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:  # bad warm_from_date / missing option-chain data to seed
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if req.auto:
        manager.start_loop(live.run_id)
    # An EQUITY decision inside the closing auction cannot fill in an F&O-listed name, and
    # the order path halts the run when an entry does not fill — so it would halt every day.
    # A WARNING, not a 422: a watchlist of only non-F&O names is perfectly fine at 15:20.
    # Raised here (not in the form) so an API caller hears it too.
    if not is_deriv:
        from skas_algo.live.quotes import auction_warning
        from skas_algo.notify import Alert, AlertLevel, build_notifier

        if (warn := auction_warning(config.decision_time)):
            build_notifier().send(Alert(
                f"Deployed in the closing auction: {config.name}", warn, AlertLevel.WARNING))
    # Trading-brain capture: a run-card + a "deploy" journal entry (no-op without a vault).
    from skas_algo.db.models import Algo, AlgoRun
    from skas_algo.services.vault_export import export_run_safe, journal_safe
    run = db.get(AlgoRun, live.run_id)
    algo = db.get(Algo, run.algo_id) if run else None
    if run and algo:
        export_run_safe(run, algo)
        journal_safe("deploy", f"{algo.name} ({algo.strategy_id}, {req.mode})",
                     strategy=algo.strategy_id, run_id=run.id, detail=f"capital ₹{algo.capital:,.0f}")
    return live


@router.post("/start")
async def start_live(
    req: LiveStartRequest,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    return start_deployment(req, db, loader, avail).snapshot()


def _get(run_id: int):
    live = manager.get(run_id)
    if live is None:
        raise HTTPException(status_code=404, detail="live run not found")
    return live


@router.get("")
async def list_live() -> list[dict]:
    return [live.snapshot() for live in manager.list()]


def _deployment_status(run: AlgoRun) -> str:
    if run.archived:
        return "archived"
    return "active" if run.id in manager.runs else "stopped"


@router.get("/deployments")
async def list_deployments(status: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    """All paper/live deployments as tiles, optionally filtered by status."""
    rows = db.execute(
        select(AlgoRun, Algo)
        .join(Algo, AlgoRun.algo_id == Algo.id)
        .where(AlgoRun.mode != TradingMode.BACKTEST)
        .order_by(AlgoRun.id.desc())
    ).all()
    out: list[dict] = []
    for run, algo in rows:
        st = _deployment_status(run)
        if status and st != status:
            continue
        tile = {
            "run_id": run.id,
            "algo_id": algo.id,
            "name": algo.name,
            "notes": algo.notes,
            "strategy_id": algo.strategy_id,
            "mode": run.mode.value,
            "status": st,
            "quote_source": (run.params_snapshot or {}).get("quote_source", "cache"),
            "instrument_class": (run.params_snapshot or {}).get("instrument_class"),
            "underlying": (run.params_snapshot or {}).get("underlying"),
            "started_at": iso_utc(run.started_at),
            "stopped_at": iso_utc(run.stopped_at),
        }
        # Broker connection: which account routes quotes/orders and whether its session
        # is currently valid. Lets the tile show a connected/disconnected indicator.
        account_id = (run.params_snapshot or {}).get("broker_account_id")
        tile["broker_account_id"] = account_id
        tile["broker_label"] = None
        tile["broker_connected"] = None
        if account_id is not None:
            account = db.get(BrokerAccount, account_id)
            if account is not None:
                tile["broker_label"] = account.label
                tile["broker_connected"] = broker_svc.has_valid_session(account)
        tile["on_cache_fallback"] = False
        tile["quote_error"] = None
        live = manager.get(run.id)
        snap = None
        if live is not None and st == "active":
            try:
                snap = live.snapshot()
            except Exception:  # ONE run's snapshot bug must never 500 the whole list → blank page
                logger.exception("snapshot failed for run %s — showing a degraded tile", run.id)
                tile["snapshot_error"] = True
        if snap is not None:
            tile["on_cache_fallback"] = snap.get("on_cache_fallback", False)
            tile["quote_error"] = snap.get("quote_error")
            tile["order_error"] = snap.get("order_error")
            # "paper" on a LIVE-mode run = restart demotion — the tile chips it loudly.
            tile["order_broker"] = snap.get("order_broker")
            tile["resume_orders_pending"] = snap.get("resume_orders_pending")
            tile["strategy_alert"] = snap.get("strategy_alert")
            tile["underlying_spot"] = snap.get("underlying_spot")  # live spot for the tile subline
            # The open cycle's entry stamp + the index level it was entered at, read off the
            # transaction log (services/live_cycles) — the tile shows "entered 57,515" next to
            # the live spot and draws the CYCLE's P&L, not the run's.
            try:
                from skas_algo.services.live_cycles import cycle_info

                tile["cycle"] = cycle_info(list(live.session.transactions), live.config.underlying)
            except Exception:
                logger.exception("cycle read failed for run %s", run.id)
                tile["cycle"] = None
            upnl = sum(p["unrealized_pnl"] for p in snap.get("positions", []))
            tile["metrics"] = {
                "equity": snap.get("equity"),
                "cash": snap.get("cash"),
                "invested": snap.get("invested", 0),
                "open_positions": snap.get("open_positions", 0),
                "open_lots": snap.get("open_lots", 0),
                "parts_total": snap.get("parts_total"),
                "unrealized_pnl": upnl,
                # Options tiles surface margin + net credit/debit instead of equity value.
                "margin_used": snap.get("margin_used"),
                "margin_source": snap.get("margin_source"),
                "net_credit": snap.get("net_credit"),
                "net_delta": snap.get("net_delta"),
                "realized_pnl": snap.get("realized_pnl"),
            }
        else:
            m = (run.metrics or {}).get("metrics", {})
            tile["metrics"] = {
                "equity": m.get("Final Equity"),
                "total_return_pct": m.get("Total Return %"),
                "total_trades": m.get("Total Trades"),
                "open_positions": 0,
            }
        out.append(tile)
    return out


@router.get("/summary")
def live_summary() -> dict:
    """Home dashboard aggregates: win rate (booked round-trips), a daily equity series for
    the last ~30 days + its annualized Sharpe. The series/Sharpe build from the runs' daily
    history, so they fill in as history accumulates (and are null until ≥ 2 days).

    Basis: REAL (LIVE-mode) deployments when any are running — the VPS Home page showed
    paper stats while real money traded (owner, 2026-07-31) — else the paper fleet. The
    chosen basis rides the response so the Home page can label the hero honestly."""
    import math
    import statistics
    from datetime import date as _date
    from datetime import timedelta

    real = [lr for lr in manager.list() if str(lr.config.mode).upper() == "LIVE"]
    lives = real or [lr for lr in manager.list() if str(lr.config.mode).upper() == "PAPER"]
    wins = total = 0
    per_run_day: list[dict[_date, float]] = []  # each run's last total_equity per calendar day
    for lr in lives:
        for t in lr.session.transactions:
            # Closed round-trips only (mirror compute_metrics): long sells, short covers, expiry
            # settlement — NOT entries (which carry no realized P&L).
            if t.get("action") not in ("SELL", "COVER", "SETTLE"):
                continue
            total += 1
            if (t.get("profit") or 0) > 0:
                wins += 1
        day_eq: dict[_date, float] = {}
        for row in lr.session.history:
            d = row.get("date")
            dd = d.date() if hasattr(d, "date") else d if isinstance(d, _date) else None
            te = row.get("total_equity")
            if dd is not None and te is not None:
                day_eq[dd] = float(te)  # last point of the day wins
        if day_eq:
            per_run_day.append(day_eq)

    win_rate = (wins / total * 100) if total else None

    # Aggregate equity across runs per day (each run forward-filled from its first day), last 30 days.
    all_days = sorted({d for r in per_run_day for d in r})
    series: list[float] = []
    if all_days:
        last: list[float | None] = [None] * len(per_run_day)
        agg: list[tuple[_date, float]] = []
        for d in all_days:
            tot = 0.0
            for i, r in enumerate(per_run_day):
                if d in r:
                    last[i] = r[d]
                if last[i] is not None:
                    tot += last[i]  # type: ignore[arg-type]
            agg.append((d, tot))
        cutoff = all_days[-1] - timedelta(days=30)
        series = [round(v, 2) for d, v in agg if d >= cutoff]

    change = sharpe = None
    if len(series) >= 2 and series[0] > 0:
        change = (series[-1] - series[0]) / series[0] * 100
        rets = [(series[i] - series[i - 1]) / series[i - 1]
                for i in range(1, len(series)) if series[i - 1] > 0]
        if len(rets) >= 2:
            sd = statistics.pstdev(rets)
            if sd > 0:
                sharpe = statistics.mean(rets) / sd * math.sqrt(252)  # annualized (daily)

    return {
        # Which fleet the numbers describe — "live" (real money) when any LIVE run exists.
        "basis": "live" if real else "paper",
        "win_rate": win_rate,
        "total_trades": total,
        "equity_series": series,
        "equity_change_pct_30d": change,
        "sharpe_30d": sharpe,
        # Last successful daily historical-cache refresh (for the quiet "Data ✓ HH:MM" chip).
        "last_cache_refresh": manager.last_cache_refresh,
        # Last daily option-bar capture (the self-built GFD store; SKAS_OPTION_BARS_*).
        "last_option_capture": manager.last_option_capture,
    }


# The Live page's index strip: NIFTY / BANKNIFTY / SENSEX with a day change. ONE Kite quote
# call per 10s no matter how many tabs poll (module cache), off any logged-in Zerodha account;
# with no session it falls back to the running deployments' own live spots (no day change).
# Declared ABOVE the /{run_id} routes — a path "indices" would otherwise 422 as a run id.
_INDEX_QUOTE_KEYS = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "SENSEX": "SENSEX"}
_INDEX_TTL_S = 10.0
_index_cache: dict = {"at": 0.0, "body": None}


def index_rows(quotes: dict) -> list[dict]:
    """Rows for the strip from a ``day_quotes`` answer keyed by Kite index name."""
    out: list[dict] = []
    for name, key in _INDEX_QUOTE_KEYS.items():
        row = quotes.get(key) or {}
        last = row.get("last")
        if not last:
            continue
        prev = row.get("prev_close")
        change = (float(last) - float(prev)) if prev else None
        out.append({
            "name": name,
            "last": float(last),
            "prev_close": float(prev) if prev else None,
            "change": round(change, 2) if change is not None else None,
            "change_pct": round(change / float(prev) * 100, 2) if change is not None else None,
        })
    return out


def _index_quotes(db: Session) -> dict:
    import time
    from datetime import datetime

    from skas_algo.services.live_cycles import IST

    now = time.monotonic()
    if _index_cache["body"] is not None and now - _index_cache["at"] < _INDEX_TTL_S:
        return _index_cache["body"]
    rows: list[dict] = []
    source = None
    accounts = [
        a for a in broker_svc.list_accounts(db)
        if "zerodha" in str(a.broker).lower() and broker_svc.has_valid_session(a)
    ]
    if accounts:
        try:
            quotes = broker_svc.make_adapter(accounts[0]).day_quotes(list(_INDEX_QUOTE_KEYS.values()))
            rows = index_rows(quotes)
            source = accounts[0].label
        except Exception:
            logger.warning("index quotes failed — falling back to run spots", exc_info=True)
    if not rows:
        seen: dict[str, float] = {}
        for lr in manager.list():
            u = str(lr.config.underlying or "").upper()
            if u in _INDEX_QUOTE_KEYS and u not in seen:
                try:
                    spot = lr._underlying_spot()
                except Exception:
                    spot = None
                if spot:
                    seen[u] = float(spot)
        rows = [{"name": u, "last": v, "prev_close": None, "change": None, "change_pct": None}
                for u, v in seen.items()]
        source = "runs" if rows else None
    body = {"indices": rows, "source": source, "as_of": datetime.now(IST).isoformat()}
    _index_cache.update(at=now, body=body)
    return body


@router.get("/indices")
def live_indices(db: Session = Depends(get_db)) -> dict:
    return _index_quotes(db)


@router.get("/{run_id}")
async def get_live(run_id: int) -> dict:
    return _get(run_id).snapshot()


@router.get("/{run_id}/watchlist")
async def watchlist(run_id: int) -> dict:
    """Per-symbol signal status (price, 20-day levels, tracking, holding)."""
    from skas_algo.engine.jsonutil import to_native

    return {"run_id": run_id, "rows": to_native(_get(run_id).session.watchlist())}


@router.post("/{run_id}/quote-source")
async def set_quote_source(
    run_id: int,
    body: QuoteSourceInput,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
) -> dict:
    """Swap a running run's quote source (e.g. cache -> Zerodha live) in place."""
    live = _get(run_id)
    account_id = body.broker_account_id if is_broker_source(body.quote_source) else None
    live.quote_source = _build_quote_source(body.quote_source, account_id, loader, db)
    live.config.quote_source = body.quote_source
    live.config.broker_account_id = account_id
    # Persist so a restart recovers with the new source.
    run = db.get(AlgoRun, run_id)
    if run is not None:
        params = dict(run.params_snapshot or {})
        params["quote_source"] = body.quote_source
        params["broker_account_id"] = account_id
        run.params_snapshot = params
    return live.snapshot()


@router.post("/{run_id}/reconnect-quotes")
async def reconnect_quotes(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Promote a cache-fallback run back to live Zerodha quotes (needs a valid session)."""
    live = _get(run_id)
    if not manager.promote_quote_source(run_id, db):
        raise HTTPException(
            status_code=400,
            detail="cannot reconnect — run isn't on cache fallback or no valid session",
        )
    return live.snapshot()


@router.post("/{run_id}/refresh")
async def refresh_live(run_id: int, decide: bool = False) -> dict:
    """Re-price all positions. With ``decide=true`` it then runs a decision so any
    profit-booking / stop-loss that an auto-refresh would trigger fires now too.

    ``decide`` IS A TRADING ACTION and is gated exactly like ``/run-decision``. It was not,
    and that gap was live: the Live tile's "Refresh" button passed decide=true, so pressing
    it to update prices ran a full decision — eight real orders on run 28 on 2026-08-31,
    hours before its 15:05 decision time — through a path that ALSO skipped the
    reconcile-pending check the explicit button honours. The UI no longer sends decide from
    Refresh; this guard is the backstop, so no future caller can trade through the cheap
    door either."""
    live = _get(run_id)
    if decide and getattr(live, "reconcile_pending", False):
        raise HTTPException(
            status_code=409,
            detail="run is reconciling its broker book — decisions are held until it clears",
        )
    live.refresh()
    if decide:
        live.run_decision()
    return live.snapshot()


@router.post("/{run_id}/run-decision")
async def run_decision(run_id: int) -> dict:
    live = _get(run_id)
    # A real-order run that hasn't reconciled its broker book yet must not decide (the
    # loop enforces this too; the manual trigger bypasses the tick's refresh→reconcile).
    if getattr(live, "reconcile_pending", False):
        raise HTTPException(
            status_code=409,
            detail="run is reconciling its broker book — decisions are held until it clears",
        )
    events = live.run_decision()
    return {
        "run_id": run_id,
        "trades": [
            {
                "ticker": e["ticker"],
                "action": e["action"],
                "units": e["units"],
                "price": e["price"],
                "tag": e["tag"],
            }
            for e in events
        ],
    }


@router.post("/{run_id}/controls")
async def set_controls(run_id: int, body: LiveControlsInput) -> dict:
    """Edit a running deployment's loop controls and exclusion list, in place.

    Any field left null is unchanged. ``excluded_symbols`` replaces the blocklist;
    excluded names get no new entries while open positions keep being managed.
    """
    _get(run_id)
    live = manager.update_controls(
        run_id,
        auto=body.auto,
        ignore_market_hours=body.ignore_market_hours,
        refresh_seconds=body.refresh_seconds,
        excluded_symbols=body.excluded_symbols,
        lots=body.lots,
        lot_sets=body.lot_sets,
    )
    if body.auto is not None:  # journal the meaningful pause/resume toggle
        from skas_algo.services.vault_export import journal_safe
        journal_safe("intervene", f"{'Resumed' if body.auto else 'Paused'} {live.config.name}",
                     strategy=live.config.strategy_id, run_id=run_id)
    return live.snapshot()


@router.get("/{run_id}/greeks-history")
async def greeks_history(run_id: int, limit: int = 1000, db: Session = Depends(get_db)) -> dict:
    """Sampled greeks time-series for an options deployment (net delta + IV + per-leg)."""
    rows = (
        db.execute(
            select(GreeksSnapshot)
            .where(GreeksSnapshot.algo_run_id == run_id)
            .order_by(GreeksSnapshot.ts.desc())
            .limit(max(1, min(limit, 5000)))
        )
        .scalars()
        .all()
    )
    ordered = list(reversed(rows))  # oldest → newest for charting
    points = [
        {
            "ts": iso_utc(r.ts),
            "spot": r.spot,
            "net_delta": r.net_delta,
            "net_iv": r.net_iv,
            "pnl": r.pnl,  # UNREALIZED at the sample (persistence.record_greeks)
            "legs": r.legs,
        }
        for r in ordered
    ]
    # Realized P&L booked up to each sample, so a chart can show OVERALL (pnl + realized_cum)
    # or one CYCLE (overall − the cycle's realized_before). Running deployments only — the
    # transaction log lives on the session.
    live = manager.get(run_id)
    if live is not None and points:
        from datetime import timezone as _tz

        from skas_algo.services.live_cycles import realized_cumulative

        stamps = [(r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=_tz.utc)) for r in ordered]
        for p, cum in zip(points, realized_cumulative(list(live.session.transactions), stamps)):
            p["realized_cum"] = cum
    return {"run_id": run_id, "points": points}


@router.get("/{run_id}/pnl-history")
def pnl_history(run_id: int, db: Session = Depends(get_db)) -> dict:
    """The deployment's OVERALL progress, one point per trading day since deploy — the
    expanded card's dated chart. Realized by day comes from the transaction log (gross, the
    same basis as the tile's Realized KPI); each day's closing unrealized is that day's LAST
    greeks sample (0 when the book was flat at the close); ``overall`` is the sum. The
    daily equity ``history`` was NOT used: it books charges and short premium differently
    from the KPIs, so its last point would disagree with the number beside the chart."""
    from datetime import timedelta, timezone as _tz

    from sqlalchemy import func

    from skas_algo.services.live_cycles import IST, daily_pnl

    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    live = manager.get(run_id)
    if live is not None:
        txns = list(live.session.transactions)
    else:
        txns = list((run.state or {}).get("transactions") or [])
    # The last sample of each IST day: ids are monotonic with ts, so max(id) per day is it.
    day_expr = func.date(GreeksSnapshot.ts, "+330 minutes")
    last_ids = (
        select(func.max(GreeksSnapshot.id))
        .where(GreeksSnapshot.algo_run_id == run_id)
        .group_by(day_expr)
    )
    eod: dict[str, float] = {}
    for ts, pnl in db.execute(
        select(GreeksSnapshot.ts, GreeksSnapshot.pnl).where(GreeksSnapshot.id.in_(last_ids))
    ):
        if ts is None:
            continue
        utc = ts if ts.tzinfo else ts.replace(tzinfo=_tz.utc)
        eod[(utc + timedelta(minutes=330)).date().isoformat()] = float(pnl or 0.0)
    del IST  # the offset above is the same +05:30; the name is imported for the reader
    return {
        "run_id": run_id,
        "started_at": iso_utc(run.started_at),
        "days": daily_pnl(txns, eod),
        "running": live is not None,
    }


def _orders_to_trades(orders: list[Order]) -> list[dict]:
    """Reconstruct trades (entry legs + exits with per-leg P&L) from the persisted Order rows —
    the durable audit trail — so a closed cycle survives restarts even before it's finalized.
    FIFO match per symbol; profit is directional (short entry SELL → cover BUY)."""
    from collections import defaultdict, deque

    open_lots: dict[str, deque] = defaultdict(deque)  # symbol -> [units, price, side]
    out: list[dict] = []
    for o in orders:
        side = o.side.value if hasattr(o.side, "value") else str(o.side)
        sym, units, px = o.symbol, int(o.quantity), float(o.price or 0.0)
        # Owner rule: dates ALWAYS carry the time. created_at is naive-UTC → IST.
        d = None
        if o.created_at:
            from datetime import timedelta as _td
            d = (o.created_at + _td(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M")
        q = open_lots[sym]
        closing = q and q[0][2] != side  # opposite side of the open position → an exit
        if closing:
            rem, profit = units, 0.0
            while rem > 0 and q:
                lot = q[0]
                take = min(rem, lot[0])
                profit += (lot[1] - px) * take if lot[2] == "SELL" else (px - lot[1]) * take
                lot[0] -= take
                rem -= take
                if lot[0] == 0:
                    q.popleft()
            out.append({"date": d, "ticker": sym, "action": "COVER" if side == "BUY" else "SELL",
                        "units": units, "price": px, "profit": profit, "pnl_pct": 0.0,
                        "lots": 1, "tag": o.tag or ""})
            if rem > 0:
                q.append([rem, px, side])
        else:
            q.append([units, px, side])
            out.append({"date": d, "ticker": sym, "action": "SHORT" if side == "SELL" else "BUY",
                        "units": units, "price": px, "profit": 0.0, "pnl_pct": 0.0,
                        "lots": 1, "tag": o.tag or ""})
    return out


@router.get("/{run_id}/trades")
async def live_trades(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Executed trades for a deployment — entry legs + exits with per-leg P&L, holding days and
    exit reason — so a CLOSED cycle still shows what was traded, when it exited, and the booked
    P&L. Prefers the running session's in-memory transactions (richest: exit_reason/holding_days),
    then the persisted trade log, then a reconstruction from the durable Order rows."""
    from skas_algo.live.manager import _serialize_event

    live = manager.get(run_id)
    if live is not None and live.session.transactions:
        trades = [_serialize_event(t) for t in live.session.transactions]
    else:
        run = db.get(AlgoRun, run_id)
        trades = (run.trade_log if run is not None else None) or []
        if not trades and run is not None:
            orders = db.execute(
                select(Order).where(Order.algo_id == run.algo_id).order_by(Order.id)
            ).scalars().all()
            trades = _orders_to_trades(orders)
    return {"run_id": run_id, "trades": trades}


@router.post("/{run_id}/flatten")
async def flatten(run_id: int) -> dict:
    """Exit-all: close every open position now, at live prices. The strategy adopts the
    now-flat book (it won't try to manage legs that no longer exist)."""
    live = _get(run_id)
    events = live.flatten()
    from skas_algo.services.vault_export import journal_safe
    journal_safe("intervene", f"Flattened {live.config.name}", strategy=live.config.strategy_id,
                 run_id=run_id, detail=f"closed {len(events)} legs at live prices")
    return {"run_id": run_id, "closed": len(events), "snapshot": live.snapshot()}


@router.post("/{run_id}/adopt-broker-close")
async def adopt_broker_close(run_id: int, body: AdoptBrokerCloseInput) -> dict:
    """Book legs the BROKER already closed, at the prices they settled at. NO orders.

    For the case flatten can't handle: the position is already gone at the broker (you
    squared off in Kite, or an MIS auto-square-off fired), so there is nothing left to
    trade — the platform just needs to stop believing it holds it. Without this the run
    carries a phantom leg and every reconciliation halts it.
    """
    live = _get(run_id)
    try:
        events = live.adopt_broker_close({leg.symbol: leg.price for leg in body.legs})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from skas_algo.services.vault_export import journal_safe
    detail = ", ".join(f"{leg.symbol} @ {leg.price}" for leg in body.legs)
    journal_safe("intervene", f"Adopted broker close on {live.config.name}",
                 strategy=live.config.strategy_id, run_id=run_id,
                 detail=f"booked {len(events)} leg(s) with no order: {detail}")
    return {"run_id": run_id, "closed": len(events), "snapshot": live.snapshot()}


@router.post("/{run_id}/set-holding")
async def set_holding(run_id: int, body: SetHoldingInput) -> dict:
    """Force the platform's unit count for one symbol. Places NO order.

    Adoption only ever ADDS — a broker showing fewer units is usually a real divergence, and
    deleting the platform's book on a bad read would be worse than halting. That leaves no
    way back from an OVER-count, which is where run 28 ended up on 2026-08-31: 778 LIQUIDCASE
    against a true 754, halting every reconciliation. Redeploying would have fixed the count
    and thrown away the settlement ledger and the pots with it.
    """
    live = _get(run_id)
    try:
        out = live.set_holding_units(body.symbol, body.units)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from skas_algo.services.vault_export import journal_safe
    journal_safe("intervene", f"Set {out['symbol']} units on {live.config.name}",
                 strategy=live.config.strategy_id, run_id=run_id,
                 detail=f"{out['before']:g} -> {out['after']:g} by hand, no order placed")
    return {"run_id": run_id, **out, "snapshot": live.snapshot()}


@router.post("/{run_id}/params")
async def update_run_params(run_id: int, body: dict) -> dict:
    """Hot-edit strategy params on a running deployment (profit target / SL / trail…).

    Accepts {"params": {...}} (or a bare dict). Infra keys (mode, symbols, capital,
    quote_source…) are rejected — those require stop + redeploy. The strategy is rebuilt
    recovery-style with its state carried over, and the merged params are persisted to
    Algo.params + AlgoRun.params_snapshot so a restart keeps the edit."""
    live = _get(run_id)
    changes = body.get("params") if isinstance(body.get("params"), dict) else body
    try:
        result = live.update_params(changes or {})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TypeError as exc:  # unknown kwarg never reaches the ctor (strategy_kwargs
        raise HTTPException(status_code=422, detail=str(exc)) from exc  # filters), but be safe
    from skas_algo.services.vault_export import journal_safe
    journal_safe("intervene", f"Edited params on {live.config.name}",
                 strategy=live.config.strategy_id, run_id=run_id,
                 detail=f"changed {', '.join(result['applied'])}")
    return {"run_id": run_id, **result, "snapshot": live.snapshot()}


@router.post("/{run_id}/manual-order")
async def manual_order(run_id: int, body: ManualOrderInput) -> dict:
    """Option-aware live intervention: close selected legs/lots and/or open new legs now.

    Executes immediately at live prices; afterwards the strategy adopts the resulting book.
    """
    live = _get(run_id)
    try:
        events = live.manual_order(
            closes=[c.model_dump() for c in body.closes],
            opens=[o.model_dump() for o in body.opens],
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from skas_algo.services.vault_export import journal_safe
    journal_safe("intervene", f"Manual order on {live.config.name}", strategy=live.config.strategy_id,
                 run_id=run_id, detail=f"closed {len(body.closes)} / opened {len(body.opens)}")
    return {"run_id": run_id, "executed": len(events), "snapshot": live.snapshot()}


@router.post("/{run_id}/overrides")
async def add_override(run_id: int, override: OverrideInput) -> dict:
    """Live intervention: inject an override rule into the running session.

    The resolver reads its mutable rule list on each decision, so this takes effect
    on the run's next decision (e.g. 'book 50% at 6%, trail the rest').
    """
    live = _get(run_id)
    live.session.resolver.overrides.append(
        OverrideRule(scope=override.scope, target=override.target, rule=override.rule)
    )
    return {"run_id": run_id, "overrides": len(live.session.resolver.overrides)}


@router.post("/{run_id}/stop")
async def stop_live(run_id: int) -> dict:
    """Stop the deployment (→ Stopped tab). Blocked while positions are open — exit them first."""
    live = _get(run_id)
    strategy = getattr(live.session, "strategy", None)
    open_syms = list(live.session.portfolio.lot_symbols())
    # An ACCUMULATION strategy never sells, so "exit them first" is an instruction it can
    # never satisfy — the guard made such a run permanently unstoppable (run 23, 2026-08-28).
    # Its holdings are delivery stock that simply stays in the broker account; stopping ends
    # the platform's management of it, it does not abandon anything that needs managing.
    if open_syms and not getattr(strategy, "never_sells", False):
        raise HTTPException(
            status_code=409,
            detail=f"Exit the {len(open_syms)} open position(s) before stopping — use Exit.",
        )
    from skas_algo.services.vault_export import journal_safe
    journal_safe("lifecycle", f"Stopped {live.config.name}", strategy=live.config.strategy_id, run_id=run_id)
    manager.stop(run_id)
    return {"stopped": run_id}


@router.post("/{run_id}/go-live")
async def go_live(
    run_id: int,
    body: GoLiveRequest,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    """Promote a PAPER deployment to a fresh LIVE one (re-enters per the strategy). Real orders
    require an armed account with a valid session + platform live-trading enabled."""
    paper = _get(run_id)
    if paper.config.mode.upper() != "PAPER":
        raise HTTPException(status_code=422, detail="only a PAPER deployment can be taken live")
    account = db.get(BrokerAccount, body.broker_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="broker account not found")
    if not broker_svc.has_valid_session(account):
        raise HTTPException(status_code=400, detail="broker account has no valid session — log in first")
    if not account.armed:
        raise HTTPException(status_code=400, detail="arm the broker account on the Brokers page first")
    if not get_settings().live_trading_enabled:
        raise HTTPException(status_code=400, detail="live trading is disabled (SKAS_LIVE_TRADING_ENABLED)")

    cfg = paper.config
    params = dict(cfg.params)
    if body.lots:
        params["lots"] = int(body.lots)
    req = LiveStartRequest(
        strategy_id=cfg.strategy_id,
        name=f"{cfg.name} [LIVE]",
        notes=cfg.notes,
        instrument_class=cfg.instrument_class,
        underlying=cfg.underlying,
        symbols=list(cfg.symbols),
        capital=body.capital or cfg.capital,
        params=params,
        tax_rate=cfg.tax_rate,
        withdrawal_rate=cfg.withdrawal_rate,
        lookback=cfg.lookback,
        mode="LIVE",
        quote_source=(account.broker or "zerodha").lower(),  # live quotes ride the chosen account's broker
        broker_account_id=body.broker_account_id,
        refresh_seconds=cfg.refresh_seconds,
        decision_time=cfg.decision_time,
        ignore_market_hours=cfg.ignore_market_hours,
        auto=True,
    )
    live = start_deployment(req, db, loader, avail)
    if not body.keep_paper_running:
        manager.stop(run_id)  # paper book is simulated — safe to drop
    return live.snapshot()


@router.post("/{run_id}/force-entry")
async def force_entry(run_id: int) -> dict:
    """Arm the strategy's force-entry: the next tick attempts entry, bypassing its
    schedule gates (entry day/window). Only strategies exposing request_force_entry
    support it; structural gates (credit windows, chain availability) still apply."""
    live = manager.get(run_id)
    if live is None:
        raise HTTPException(status_code=404, detail="run is not active")
    strategy = getattr(live.session, "strategy", None)
    fn = getattr(strategy, "request_force_entry", None)
    if fn is None:
        raise HTTPException(status_code=400,
                            detail="this strategy has no forced-entry semantics")
    note = fn()
    live._persist_state()  # the armed flag survives a restart
    return {"armed": True, "note": note}


@router.post("/{run_id}/ironfly-adjust")
async def ironfly_adjust(run_id: int, body: dict) -> dict:
    """Turn the post-iron-fly adjustment ON/OFF for a running deploy. Takes effect on the next
    tick and survives a restart (persisted via export_state). Only strategies exposing
    set_ironfly_adjust (delta_neutral_monthly / iron_fly_monthly) support it."""
    live = manager.get(run_id)
    if live is None:
        raise HTTPException(status_code=404, detail="run is not active")
    strategy = getattr(live.session, "strategy", None)
    fn = getattr(strategy, "set_ironfly_adjust", None)
    if fn is None:
        raise HTTPException(status_code=400,
                            detail="this strategy has no iron-fly adjustment")
    note = fn(bool(body.get("on", True)))
    live._persist_state()
    # Push the updated snapshot NOW so the live card's toggle reflects it immediately
    # (a plain refresh only re-broadcasts on the next decision tick).
    manager.broadcaster.publish({"type": "snapshot", "run_id": run_id, **live.snapshot()})
    return {"ironfly_adjust": strategy.ironfly_adjust, "note": note}


@router.post("/{run_id}/ack-order-error")
async def ack_order_error(run_id: int) -> dict:
    """Owner acknowledges a real-order failure: clears the halt so decisions resume.
    The book should be reviewed first — whatever filled before the failure is real."""
    live = manager.get(run_id)
    if live is None:
        raise HTTPException(status_code=404, detail="run is not active")
    prev = live.order_error
    live.order_error = None
    return {"cleared": prev}


@router.post("/{run_id}/activate")
async def activate(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Restart a stopped deployment (→ Active). Rebuilds from its saved config and resumes the loop."""
    if run_id in manager.runs:
        raise HTTPException(status_code=400, detail="deployment is already active")
    run = _get_run(db, run_id)
    if run.archived:
        raise HTTPException(status_code=400, detail="unarchive the deployment before activating")
    from skas_algo.live.recovery import reactivate

    try:
        reactivate(run_id)
    except Exception as exc:  # pragma: no cover - cache/strategy rebuild failure
        raise HTTPException(status_code=500, detail=f"activation failed: {exc}") from exc
    live = manager.get(run_id)
    return live.snapshot() if live is not None else {"run_id": run_id, "status": "active"}


def _get_run(db: Session, run_id: int) -> AlgoRun:
    run = db.get(AlgoRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    return run


@router.post("/{run_id}/archive")
async def archive(run_id: int, db: Session = Depends(get_db)) -> dict:
    if run_id in manager.runs:
        manager.stop(run_id)  # finalize before hiding
    _get_run(db, run_id).archived = True
    return {"run_id": run_id, "status": "archived"}


@router.post("/{run_id}/unarchive")
async def unarchive(run_id: int, db: Session = Depends(get_db)) -> dict:
    _get_run(db, run_id).archived = False
    return {"run_id": run_id, "status": "stopped"}


@router.patch("/{run_id}")
async def update_deployment(
    run_id: int, body: DeploymentUpdate, db: Session = Depends(get_db)
) -> dict:
    run = _get_run(db, run_id)
    algo = db.get(Algo, run.algo_id)
    if body.name is not None:
        algo.name = body.name
    if body.notes is not None:
        algo.notes = body.notes
    live = manager.get(run_id)
    if live is not None:  # keep the in-memory config in sync
        if body.name is not None:
            live.config.name = body.name
        if body.notes is not None:
            live.config.notes = body.notes
    return {"run_id": run_id, "name": algo.name, "notes": algo.notes}


@router.delete("/{run_id}")
async def delete_deployment(run_id: int, db: Session = Depends(get_db)) -> dict:
    """Permanently remove a deployment: its run, orders, fills, positions, and Algo."""
    if run_id in manager.runs:
        manager.stop(run_id)
    run = _get_run(db, run_id)
    delete_algo_cascade(db, run.algo_id)
    return {"deleted": run_id}


@ws_router.websocket("/ws")
async def live_ws(ws: WebSocket) -> None:
    # Auth gate BEFORE accept(): when configured, require a valid ?token= (a WS can't send an
    # Authorization header). Fail-open when auth is off. Close 1008 (policy violation) on a bad
    # token so the client sees a clean rejection.
    if get_settings().auth_enabled:
        from skas_algo.security import AuthError, decode_token

        token = ws.query_params.get("token", "")
        try:
            decode_token(token)
        except AuthError:
            await ws.close(code=1008)
            return
    await ws.accept()
    queue = manager.broadcaster.subscribe()
    # A reader, purely to learn when the client hangs up. This handler only ever SENDS, and
    # a send is not what raises WebSocketDisconnect — only receive() is. Without the reader
    # the disconnect surfaces only when the NEXT broadcast fails, so between broadcasts a
    # dead client keeps its subscription and its slot in the fan-out.
    watcher = asyncio.create_task(_watch_disconnect(ws, queue))
    try:
        while True:
            message = await queue.get()
            if message is _CLOSED:
                break
            await ws.send_json(message)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        # The client vanished mid-send. Starlette reports THAT as a bare RuntimeError off the
        # closed transport ("unable to perform operation on <TCPTransport closed=True …>; the
        # handler is closed"), never as WebSocketDisconnect — so the old `except
        # WebSocketDisconnect` never caught it and every page reload logged an
        # unhandled-exception traceback (owner spotted the noise, 2026-09-03). Losing the
        # client is the ordinary end of this handler, not an error.
        pass
    finally:
        watcher.cancel()
        manager.broadcaster.unsubscribe(queue)


_CLOSED = object()
"""Sentinel pushed by the disconnect watcher to wake the send loop and end the handler."""


async def _watch_disconnect(ws: WebSocket, queue: asyncio.Queue) -> None:
    """Drain the client's frames until it disconnects, then wake the sender.

    Nothing the browser sends is acted on — reading exists ONLY so the close frame is
    processed. A full queue means the sender is already behind and will hit the dead
    transport on its next send anyway, so dropping the sentinel there is safe."""
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
    except Exception:  # pragma: no cover - any read failure means the client is gone
        pass
    finally:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(_CLOSED)
