"""Live/paper run control: start, manual refresh/decision, stop, list + WebSocket.

Async endpoints call the manager directly (loop thread) so broadcasts to WebSocket
subscribers are thread-safe. The "cache" quote source works offline; "zerodha" needs
a logged-in account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import (
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
from skas_algo.live.quotes import BrokerQuoteSource, CacheQuoteSource, is_broker_source
from skas_algo.services import broker as broker_svc
from skas_algo.services.runs import delete_algo_cascade

router = APIRouter(tags=["live"], prefix="/live")


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
        return BrokerQuoteSource(broker_svc.make_adapter(account))
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
            decision_time=req.decision_time,
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
        if live is not None and st == "active":
            snap = live.snapshot()
            tile["on_cache_fallback"] = snap.get("on_cache_fallback", False)
            tile["quote_error"] = snap.get("quote_error")
            tile["order_error"] = snap.get("order_error")
            tile["underlying_spot"] = snap.get("underlying_spot")  # live spot for the tile subline
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
    """Home dashboard aggregates across ACTIVE PAPER deployments: win rate (booked round-trips), a
    daily equity series for the last ~30 days + its annualized Sharpe. The series/Sharpe build from
    the runs' daily history, so they fill in as history accumulates (and are null until ≥ 2 days)."""
    import math
    import statistics
    from datetime import date as _date
    from datetime import timedelta

    lives = [lr for lr in manager.list() if str(lr.config.mode).upper() == "PAPER"]
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
        "win_rate": win_rate,
        "total_trades": total,
        "equity_series": series,
        "equity_change_pct_30d": change,
        "sharpe_30d": sharpe,
    }


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
    profit-booking / stop-loss that an auto-refresh would trigger fires now too."""
    live = _get(run_id)
    live.refresh()
    if decide:
        live.run_decision()
    return live.snapshot()


@router.post("/{run_id}/run-decision")
async def run_decision(run_id: int) -> dict:
    live = _get(run_id)
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
    points = [
        {
            "ts": iso_utc(r.ts),
            "spot": r.spot,
            "net_delta": r.net_delta,
            "net_iv": r.net_iv,
            "pnl": r.pnl,
            "legs": r.legs,
        }
        for r in reversed(rows)  # oldest → newest for charting
    ]
    return {"run_id": run_id, "points": points}


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
        d = o.created_at.date().isoformat() if o.created_at else None
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
    open_syms = live.session.portfolio.lot_symbols()
    if open_syms:
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


@router.websocket("/ws")
async def live_ws(ws: WebSocket) -> None:
    await ws.accept()
    queue = manager.broadcaster.subscribe()
    try:
        while True:
            message = await queue.get()
            await ws.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        manager.broadcaster.unsubscribe(queue)
