"""Trade — deploy ad-hoc, user-built positions (custom option structures + managed equity trades).

Thin endpoints that validate the structured Trade-UI input, translate it into a LiveStartRequest,
and reuse the exact same deployment path as POST /live/start (``start_deployment``). "Save" deploys
immediately; the result is a normal paper/live deployment that shows on the Live page with the usual
tiles/metrics/reports. Real-money LIVE stays gated behind ``armed`` + ``SKAS_LIVE_TRADING_ENABLED``.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import (
    CpRatioExpiryDeploy,
    DonchianAnalyzeRequest,
    DonchianDeploy,
    DonchianPortfolioRequest,
    EquityTradeDeploy,
    LiveStartRequest,
    MomentumThetaDeploy,
    OptionsTradeDeploy,
    OptionTradeLeg,
)
from skas_algo.api.routes.data import _live_adapter
from skas_algo.api.routes.live import start_deployment
from skas_algo.data.options_provider import make_spot_provider
from skas_algo.data.provider import get_available_symbols, get_data_cache, get_price_loader
from skas_algo.db.models import BrokerAccount
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.services import broker as broker_svc
from skas_algo.services.donchian_strangle import (
    DonchianParams,
    analyze_name,
    beta_from_frames,
    portfolio_panel,
    resolve_cycle,
)
from skas_algo.services.fibret import FibParams, analyze_symbol

router = APIRouter(prefix="/trade", tags=["trade"])


class OptionMarginRequest(BaseModel):
    underlying: str
    expiry: str
    lot_size: int = 0
    legs: list[OptionTradeLeg]
    broker_account_id: int | None = None


def _per_lot(underlying: str, expiry: date, lot_size: int) -> int:
    if lot_size:
        return lot_size
    try:
        return lot_size_for(underlying, expiry)
    except KeyError:
        return 0


@router.post("/options/margin")
def option_trade_margin(
    body: OptionMarginRequest,
    db: Session = Depends(get_db),
    cache=Depends(get_data_cache),
) -> dict:
    """Margin the basket would block. Prefers Zerodha's real basket margin (live session,
    with spread benefit); falls back to a SPAN+exposure model estimate on the short legs."""
    exp = date.fromisoformat(body.expiry[:10])
    per_lot = _per_lot(body.underlying.upper(), exp, body.lot_size)
    if per_lot <= 0 or not body.legs:
        return {"margin": None, "source": None}
    sized = [
        {
            "symbol": make(body.underlying.upper(), exp, float(leg.strike), leg.right.upper(),
                           lot_size=per_lot).symbol,
            "direction": -1 if leg.side.lower() == "sell" else 1,
            "units": int(leg.lots) * per_lot,
            "right": leg.right.upper(), "strike": float(leg.strike),
        }
        for leg in body.legs
    ]
    # Live basket margin (accurate, spread-netted) when a session is available.
    if body.broker_account_id is not None:
        account = db.get(BrokerAccount, body.broker_account_id)
        if account is not None and broker_svc.has_valid_session(account):
            try:
                m = broker_svc.make_adapter(account).basket_margin(sized)
            except Exception:  # pragma: no cover - API hiccup → fall through to model
                m = None
            if m is not None:
                return {"margin": float(m), "source": "zerodha"}
    # Model estimate: SPAN+exposure on each short leg (longs are debit-paid, no margin).
    spot = make_spot_provider(cache)(body.underlying.upper(), date.today())
    if spot is None:
        return {"margin": None, "source": None}
    p = MarginParams()
    total = sum(short_option_margin(spot, leg["units"], 1, p) for leg in sized if leg["direction"] < 0)
    return {"margin": float(total), "source": "model"}


def _frac(v: float | None) -> float | None:
    """A whole-number percent from the UI → a fraction the strategy uses (50 → 0.5)."""
    return None if v is None else v / 100.0


@router.post("/options/deploy")
async def deploy_option_trade(
    body: OptionsTradeDeploy,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    if not body.legs:
        raise HTTPException(status_code=422, detail="at least one leg is required")
    params = {
        "expiry": body.expiry,
        "legs": [leg.model_dump() for leg in body.legs],
        "lot_size": body.lot_size,
        "spot_upper": body.spot_upper,
        "spot_lower": body.spot_lower,
        "target_pct": _frac(body.target_pct),
        "stop_pct": _frac(body.stop_pct),
        "leg_targets": {int(k): _frac(v) for k, v in (body.leg_targets or {}).items()},
        "leg_stops": {int(k): _frac(v) for k, v in (body.leg_stops or {}).items()},
    }
    req = LiveStartRequest(
        strategy_id="custom_options",
        name=body.name,
        notes=body.notes,
        instrument_class="DERIV",
        underlying=body.underlying.upper(),
        capital=body.capital,
        params=params,
        mode=body.mode,
        quote_source=body.quote_source,
        broker_account_id=body.broker_account_id,
        ignore_market_hours=body.ignore_market_hours,
        auto=body.auto,
    )
    return start_deployment(req, db, loader, avail).snapshot()


class FibRetRequest(BaseModel):
    broker_account_id: int
    symbols: list[str]
    expiry: str | None = None        # ISO; default = nearest listed expiry with DTE ≥ min_dte
    swing_lookback: int = 20         # trading days for the swing high/low (recent swing)
    entry_fib: float = 1.618         # short-strike extension level
    stop_fib: float = 0.786          # spot stop level
    target_pct: float = 90.0         # whole percent (UI) → fraction at deploy
    min_oi: int = 0                  # liquidity floor for the ⚑ flag
    lots: int = 1
    min_dte: int = 7


def _pick_expiry(adapter, symbol: str, requested: str | None, on_date: date, min_dte: int) -> date | None:
    if requested:
        return date.fromisoformat(requested[:10])
    try:
        exps = adapter.option_expiries(symbol) or []
    except Exception:  # pragma: no cover - network hiccup
        return None
    parsed = sorted({date.fromisoformat(str(e)[:10]) for e in exps})
    future = [e for e in parsed if (e - on_date).days >= min_dte]
    return future[0] if future else (parsed[-1] if parsed else None)


@router.post("/options/fibret/analyze")
def fibret_analyze(
    body: FibRetRequest,
    db: Session = Depends(get_db),
    cache=Depends(get_data_cache),
) -> dict:
    """Screen a watchlist for the FibRet setup using LIVE chains (needs a broker session). One row
    per symbol: suggested short leg, fib levels, live premium/OI/liquidity, R:R and margin. Errors
    are reported per-row (illiquid strike, no swing, no chain) so the table shows what failed."""
    adapter = _live_adapter(body.broker_account_id, db)  # 4xx if no valid session
    on_date = date.today()
    params = FibParams(
        swing_lookback=body.swing_lookback, entry_fib=body.entry_fib, stop_fib=body.stop_fib,
        target_pct=(body.target_pct / 100.0), lots=max(1, body.lots), min_oi=body.min_oi,
    )
    rows: list[dict] = []
    for raw in body.symbols:
        sym = raw.strip().upper()
        if not sym:
            continue
        try:
            df = cache.get_prices(sym, start_date=on_date - timedelta(days=400), end_date=on_date)
        except Exception:
            df = None
        if df is None or len(df) == 0:
            rows.append({"symbol": sym, "error": "no cached price history"})
            continue
        exp = _pick_expiry(adapter, sym, body.expiry, on_date, body.min_dte)
        if exp is None:
            rows.append({"symbol": sym, "error": "no listed option expiries"})
            continue
        try:
            chain = adapter.live_option_chain(sym, exp.isoformat())
        except Exception as exc:  # pragma: no cover - network hiccup
            rows.append({"symbol": sym, "error": f"live chain failed: {exc}"})
            continue
        if not chain:
            rows.append({"symbol": sym, "error": f"no listed options for {exp.isoformat()}"})
            continue
        rows.append(analyze_symbol(symbol=sym, df=df, chain=chain, expiry=exp, on_date=on_date, params=params))
    from skas_algo.services.vault_export import journal_safe
    setups = sum(1 for r in rows if not r.get("error") and r.get("premium"))
    journal_safe("screen", f"FibRet screen: {len(rows)} symbols, {setups} setups", strategy="fibret")
    return {
        "as_of": on_date.isoformat(),
        "target_pct": body.target_pct,
        "entry_fib": body.entry_fib,
        "stop_fib": body.stop_fib,
        "rows": rows,
    }


# ───────────────────────────────────── Donchian Strangle Monthly (basket) ─────────────────────────

def _d(s: str | None) -> date | None:
    return date.fromisoformat(s[:10]) if s else None


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


@router.post("/options/donchian/analyze")
def donchian_analyze(
    body: DonchianAnalyzeRequest,
    db: Session = Depends(get_db),
    cache=Depends(get_data_cache),
) -> dict:
    """Screen the basket for the Donchian-strangle setup using LIVE chains. Resolves the monthly
    cycle (range window + sell expiry), then one row per name: CE@high / PE@low strikes + premiums,
    skip-leg flags, per-name margin, and status (strangle / CE-only / PE-only / excluded:event /
    excluded:filter). Per-row errors are reported so the table shows what failed."""
    adapter = _live_adapter(body.broker_account_id, db)  # 4xx if no valid session
    today = date.today()
    params = DonchianParams(
        ivp_min=body.ivp_min, require_iv_gt_hv=body.require_iv_gt_hv, hv_window=body.hv_window,
        skip_leg_min_premium_pct=body.skip_leg_min_premium_pct, round_out=body.round_out,
        breakout_atm=body.breakout_atm, lots_per_name=max(1, body.lots_per_name),
        min_hv_ratio=body.min_hv_ratio, min_channel_width_pct=body.min_channel_width_pct,
    )
    # Live India VIX for the market-stress advisory (the backtest's VIX rule can't act
    # mechanically live — lots are the owner's call — so the UI warns instead).
    try:
        live_vix = adapter.underlying_ltp("INDIA VIX")
    except Exception:  # pragma: no cover - network hiccup
        live_vix = None
    # NIFTY daily series (once) → the index trading calendar (holiday-adjusted anchors) + per-name beta.
    try:
        nifty_df = cache.get_prices("NIFTY 50", start_date=today - timedelta(days=400), end_date=today)
    except Exception:
        nifty_df = None
    trading_days = (
        {(d.date() if hasattr(d, "date") else d) for d in nifty_df["date"].tolist()}
        if nifty_df is not None and len(nifty_df) else None
    )
    # Cycle anchor: listed monthly expiries from a representative name (stocks list monthlies only).
    listed: list[date] = []
    for n in body.names:
        try:
            exps = adapter.option_expiries(n.symbol.strip().upper()) or []
        except Exception:  # pragma: no cover - network hiccup
            exps = []
        if exps:
            listed = sorted({date.fromisoformat(str(e)[:10]) for e in exps})
            break
    cyc = resolve_cycle(today, listed, trading_days=trading_days,
                        range_start=_d(body.range_start), range_end=_d(body.range_end),
                        entry_date=_d(body.entry_date), sell_expiry=_d(body.sell_expiry),
                        min_dte=body.min_dte)
    sell, rstart, rend = cyc["sell_expiry"], cyc["range_start"], cyc["range_end"]
    dates = {k: _iso(v) for k, v in cyc.items()}
    if not (sell and rstart and rend):
        return {"as_of": today.isoformat(), "dates": dates, "rows": [],
                "error": "could not resolve cycle dates — set them manually"}

    rows: list[dict] = []
    for n in body.names:
        sym = n.symbol.strip().upper()
        if not sym:
            continue
        try:
            df = cache.get_prices(sym, start_date=today - timedelta(days=400), end_date=today)
        except Exception:
            df = None
        if df is None or len(df) == 0:
            rows.append({"symbol": sym, "status": "error", "error": "no cached price history"})
            continue
        try:
            chain = adapter.live_option_chain(sym, sell.isoformat())
        except Exception as exc:  # pragma: no cover - network hiccup
            rows.append({"symbol": sym, "status": "error", "error": f"live chain failed: {exc}"})
            continue
        if not chain:
            rows.append({"symbol": sym, "status": "error", "error": f"no listed options for {sell.isoformat()}"})
            continue
        row = analyze_name(
            symbol=sym, df=df, chain=chain, sell_expiry=sell, range_start=rstart, range_end=rend,
            entry_date=cyc["entry_date"], atm_iv=n.atm_iv, ivp=n.ivp, event=n.event, params=params,
        )
        row["beta"] = beta_from_frames(df, nifty_df)
        rows.append(row)
    from skas_algo.services.vault_export import journal_safe
    tradeable = sum(1 for r in rows if r.get("status") in ("strangle", "CE-only", "PE-only"))
    journal_safe("screen", f"Donchian screen: {len(rows)} names, {tradeable} tradeable",
                 strategy="donchian_strangle_monthly", detail=f"sell expiry {sell.isoformat()}")
    return {"as_of": today.isoformat(), "dates": dates, "rows": rows, "vix": live_vix}


@router.post("/options/donchian/portfolio")
def donchian_portfolio(
    body: DonchianPortfolioRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Portfolio panel for the selected rows: aggregate notional, premium collected, the
    notional-matched NIFTY hedge (lots/strikes/cost + cap flag), the −2% stop, and the COMBINED
    basket margin (shorts + hedge) from the broker."""
    adapter = _live_adapter(body.broker_account_id, db)
    sell = date.fromisoformat(body.sell_expiry[:10])
    params = DonchianParams(
        hedge_otm_pct=body.hedge_otm_pct, hedge_cost_cap_pct=body.hedge_cost_cap_pct,
        hedge_beta_weight=body.hedge_beta_weight,
        portfolio_sl_pct=body.portfolio_sl_pct, portfolio_target_enabled=body.portfolio_target_enabled,
        portfolio_target_pct=body.portfolio_target_pct,
    )
    try:
        nifty_chain = adapter.live_option_chain("NIFTY", sell.isoformat())
    except Exception:  # pragma: no cover - network hiccup
        nifty_chain = None
    nifty_spot = (nifty_chain or {}).get("spot")
    nifty_lot = int((nifty_chain or {}).get("lot_size") or 0)
    panel = portfolio_panel(body.selected, nifty_spot=nifty_spot, nifty_lot_size=nifty_lot,
                            nifty_chain=nifty_chain, params=params)

    # Combined basket margin: every selected short leg + the hedge longs.
    legs: list[dict] = []
    for r in body.selected:
        units = int((r.get("lot_size") or 0) * (r.get("lots") or 1))
        if units <= 0:
            continue
        for leg, right in ((r.get("ce"), "CE"), (r.get("pe"), "PE")):
            if leg and leg.get("premium") is not None and not leg.get("skip"):
                sym = make(r["symbol"].upper(), sell, float(leg["strike"]), right, lot_size=1).symbol
                legs.append({"symbol": sym, "direction": -1, "units": units})
    hedge = panel.get("hedge") or {}
    h_units = int((hedge.get("nifty_lot_size") or 0) * (hedge.get("nifty_lots") or 0))
    if h_units > 0:
        for k, right in ((hedge.get("ce_strike"), "CE"), (hedge.get("pe_strike"), "PE")):
            if k:
                sym = make("NIFTY", sell, float(k), right, lot_size=1).symbol
                legs.append({"symbol": sym, "direction": 1, "units": h_units})
    if legs:
        try:
            panel["basket_margin"] = adapter.basket_margin(legs)
        except Exception:  # pragma: no cover - API hiccup → leave None
            panel["basket_margin"] = None
    # Margin basis: recompute the stop/target amounts as % of the live basket margin (portfolio_panel
    # computed the notional/premium fallback before the margin was known).
    if body.portfolio_basis == "margin" and panel.get("basket_margin"):
        bm = panel["basket_margin"]
        panel["portfolio_sl_amount"] = body.portfolio_sl_pct / 100.0 * bm
        panel["portfolio_target_amount"] = (
            body.portfolio_target_pct / 100.0 * bm if body.portfolio_target_enabled else None
        )
    return panel


def _basket_required_margin(legs: list[dict], db: Session, broker_account_id: int | None, sell: date) -> float:
    """Margin the basket needs: the broker's net basket margin (live session, hedge-benefited) when
    available, else a SPAN+exposure model estimate on the short legs."""
    sized: list[dict] = []
    model = 0.0
    p = MarginParams()
    for leg in legs:
        units = int((leg.get("lot_size") or 0) * (leg.get("lots") or 1))
        if units <= 0:
            continue
        sym = make(str(leg["underlying"]).upper(), sell, float(leg["strike"]), str(leg["right"]).upper(),
                   lot_size=1).symbol
        short = str(leg.get("side", "sell")).lower() == "sell"
        sized.append({"symbol": sym, "direction": -1 if short else 1, "units": units})
        if short and leg.get("spot"):
            model += short_option_margin(float(leg["spot"]), units, 1, p)
    if broker_account_id is not None:
        account = db.get(BrokerAccount, broker_account_id)
        if account is not None and broker_svc.has_valid_session(account):
            try:
                m = broker_svc.make_adapter(account).basket_margin(sized)
            except Exception:  # pragma: no cover - API hiccup → model estimate
                m = None
            if m is not None:
                return float(m)
    return model


@router.post("/options/donchian/deploy")
async def donchian_deploy(
    body: DonchianDeploy,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    """Deploy the resolved basket + NIFTY hedge as ONE multi-underlying DERIV deployment
    (strategy_id=donchian_strangle_monthly), reusing the standard live-start path."""
    if not body.legs:
        raise HTTPException(status_code=422, detail="at least one leg is required")
    # Pre-flight: the deployment capital must fund the basket margin, or the broker rejects it live
    # and the %-of-notional stop is meaningless.
    required = _basket_required_margin(body.legs, db, body.broker_account_id, date.fromisoformat(body.sell_expiry[:10]))
    if required and body.capital < required:
        import math

        suggested = int(math.ceil(required * 1.1 / 100_000) * 100_000)
        raise HTTPException(
            status_code=422,
            detail=(f"Capital ₹{body.capital:,.0f} is below the ~₹{required:,.0f} basket margin. "
                    f"Deploy with at least ₹{suggested:,.0f}, or reduce names / lots."),
        )
    params = {
        "expiry": body.sell_expiry,
        "legs": body.legs,
        "portfolio_sl_pct": body.portfolio_sl_pct,
        "portfolio_target_enabled": body.portfolio_target_enabled,
        "portfolio_target_pct": body.portfolio_target_pct,
        "portfolio_basis": body.portfolio_basis,
        "leg_target_enabled": body.leg_target_enabled,
        "leg_target_pct": body.leg_target_pct,
        "breach_basis": body.breach_basis,
        "breach_buffer_pct": body.breach_buffer_pct,
        "flip_delta": body.flip_delta,
        "max_flips": body.max_flips,
    }
    req = LiveStartRequest(
        strategy_id="donchian_strangle_monthly",
        name=body.name,
        notes=body.notes,
        instrument_class="DERIV",
        underlying="NIFTY",
        capital=body.capital,
        params=params,
        mode=body.mode,
        quote_source=body.quote_source,
        broker_account_id=body.broker_account_id,
        ignore_market_hours=body.ignore_market_hours,
        auto=body.auto,
    )
    return start_deployment(req, db, loader, avail).snapshot()


@router.post("/options/cp-ratio-expiry/deploy")
async def cp_ratio_expiry_deploy(
    body: CpRatioExpiryDeploy,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    """Deploy the expiry-day 1:3 premium-ratio seller as one DERIV deployment."""
    unders = [u.upper() for u in body.underlyings if u.strip()]
    if not unders:
        raise HTTPException(status_code=422, detail="pick at least one underlying")
    if body.quote_source == "cache":
        raise HTTPException(
            status_code=422,
            detail="this strategy picks strikes off the LIVE chain at 09:20 — "
                   "deploy with a broker quote source (zerodha)",
        )
    params = {
        "underlyings": unders,
        "sets": {u: int(body.sets.get(u, 1) or 1) for u in unders},
        "entry_start": body.entry_start,
        "entry_end": body.entry_end,
        "eod_exit": body.eod_exit,
        "profit_target_pct": body.profit_target_pct,
        "stop_loss_pct": body.stop_loss_pct,
        "ratio_tolerance_pct": body.ratio_tolerance_pct,
    }
    req = LiveStartRequest(
        strategy_id="call_put_ratio_expiry",
        name=body.name,
        notes=body.notes,
        instrument_class="DERIV",
        underlying=unders[0],
        capital=body.capital,
        params=params,
        mode=body.mode,
        quote_source=body.quote_source,
        broker_account_id=body.broker_account_id,
        refresh_seconds=max(5, int(body.refresh_seconds)),
        ignore_market_hours=body.ignore_market_hours,
        auto=body.auto,
    )
    return start_deployment(req, db, loader, avail).snapshot()


@router.post("/options/momentum-theta/deploy")
async def momentum_theta_deploy(
    body: MomentumThetaDeploy,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    """Deploy the intraday momentum-theta seller as one DERIV deployment
    (strategy_id=momentum_theta_gainer_intra), reusing the standard live-start path."""
    unders = [u.upper() for u in body.underlyings if u.strip()]
    if not unders:
        raise HTTPException(status_code=422, detail="pick at least one underlying")
    if "SENSEX" in unders and body.quote_source == "cache":
        raise HTTPException(
            status_code=422,
            detail="SENSEX has no cached data — deploy with a broker quote source (zerodha)",
        )
    params = {
        "underlyings": unders,
        "lots": {u: int(body.lots.get(u, 1) or 1) for u in unders},
        "st_period": body.st_period,
        "st_multiplier": body.st_multiplier,
        "candle_minutes": body.candle_minutes,
        "max_trades_per_day": body.max_trades_per_day,
        "eod_exit": body.eod_exit,
        "entry_cutoff": body.entry_cutoff,
        "min_dte": body.min_dte,
    }
    req = LiveStartRequest(
        strategy_id="momentum_theta_gainer_intra",
        name=body.name,
        notes=body.notes,
        instrument_class="DERIV",
        underlying=unders[0],
        capital=body.capital,
        params=params,
        mode=body.mode,
        quote_source=body.quote_source,
        broker_account_id=body.broker_account_id,
        refresh_seconds=max(5, int(body.refresh_seconds)),
        ignore_market_hours=body.ignore_market_hours,
        auto=body.auto,
    )
    return start_deployment(req, db, loader, avail).snapshot()


@router.post("/equity/deploy")
async def deploy_equity_trade(
    body: EquityTradeDeploy,
    db: Session = Depends(get_db),
    loader: PriceLoader = Depends(get_price_loader),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    if not body.symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    params = {
        "symbol": body.symbol.upper(),
        "qty": body.qty,
        "entry_mode": body.entry_mode,
        "trigger_price": body.trigger_price,
        "target_pct": _frac(body.target_pct),
        "stop_pct": _frac(body.stop_pct),
        "trailing": body.trailing,
        "trail_pct": _frac(body.trail_pct),
    }
    req = LiveStartRequest(
        strategy_id="custom_equity",
        name=body.name,
        notes=body.notes,
        instrument_class="STOCK",
        symbols=[body.symbol.upper()],
        capital=body.capital,
        params=params,
        mode=body.mode,
        quote_source=body.quote_source,
        broker_account_id=body.broker_account_id,
        ignore_market_hours=body.ignore_market_hours,
        auto=body.auto,
    )
    return start_deployment(req, db, loader, avail).snapshot()
