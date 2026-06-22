"""Read-only views over the skas-data historical cache (the 'Data' screen).

Uses the cache_only skas-data instance — no broker session needed. Refreshing the
cache (writing) lives under /brokers and runs on the shared Kite session.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.data.options_provider import make_spot_provider
from skas_algo.data.provider import get_data_cache
from skas_algo.db.models import BrokerAccount
from skas_algo.services import broker as broker_svc
from skas_algo.data.synthetic_options import (
    SYNTHETIC_UNDERLYINGS,
    synthetic_chain_for_view,
    synthetic_expiries,
)
from skas_algo.engine.options.black_scholes import greeks, implied_vol

router = APIRouter(tags=["data"], prefix="/data")

STALE_DAYS = 5  # lenient (covers weekends/holidays)

# Underlyings the options/futures pipeline can populate today (NSE F&O bhavcopy).
# GOLDM (MCX) is intentionally excluded — it needs a separate data source.
SUPPORTED_UNDERLYINGS = ["NIFTY", "BANKNIFTY"]
DEFAULT_RISK_FREE = 0.065  # annualized, for IV/greeks
MAX_REFRESH_DAYS = 120


def _iso(d) -> str | None:
    if d is None:
        return None
    return d.date().isoformat() if hasattr(d, "date") and not isinstance(d, str) else str(d)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def _num(v) -> float | None:
    """Coerce a possibly-NaN cell to a float or None (JSON-safe)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _int(v) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


@router.get("/summary")
def summary(cache=Depends(get_data_cache)) -> dict:
    symbols = cache.list_cached_symbols()
    db_path = getattr(getattr(cache, "storage", None), "db_path", None)
    return {"symbol_count": len(symbols), "db_path": str(db_path) if db_path else None}


@router.get("/coverage")
def coverage(
    instrument_class: str = "STOCK",
    underlying: str | None = None,
    cache=Depends(get_data_cache),
) -> dict:
    """Available cached date range, used to pre-fill the backtest date pickers.

    DERIV → the options DB range for the underlying (NIFTY/BANKNIFTY).
    STOCK → the NIFTY 50 index EOD span (always cached, covers full equity history).
    """
    if instrument_class.upper() == "DERIV":
        u = (underlying or "NIFTY").upper()
        try:
            # GOLD (synthetic) spans its cached futures series; others span the options DB.
            cov = (cache.get_coverage_stats(u) if u in SYNTHETIC_UNDERLYINGS
                   else cache.options_coverage(u)) or {}
        except Exception:
            cov = {}
        return {
            "instrument_class": "DERIV",
            "underlying": u,
            "start_date": _iso(cov.get("start_date")),
            "end_date": _iso(cov.get("end_date")),
        }
    try:
        cov = cache.get_coverage_stats("NIFTY 50") or {}
    except Exception:
        cov = {}
    return {
        "instrument_class": "STOCK",
        "underlying": None,
        "start_date": _iso(cov.get("start_date")),
        "end_date": _iso(cov.get("end_date")),
    }


@router.get("/symbols")
def list_symbols(cache=Depends(get_data_cache)) -> list[dict]:
    """Each cached symbol with its latest date + staleness (cheap MAX query per symbol)."""
    today = datetime.now(UTC).date()
    out: list[dict] = []
    for sym in sorted(cache.list_cached_symbols()):
        last = cache.storage.get_latest_date(sym)
        last_date = last.isoformat() if last else None
        stale_days = (today - last).days if last else None
        out.append(
            {
                "symbol": sym,
                "last_date": last_date,
                "stale_days": stale_days,
                "stale": stale_days is None or stale_days > STALE_DAYS,
            }
        )
    return out


@router.get("/symbols/{symbol}")
def symbol_detail(symbol: str, cache=Depends(get_data_cache)) -> dict:
    try:
        cov = cache.get_coverage_stats(symbol)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"no cached data for {symbol!r}: {exc}") from exc
    if not cov or not cov.get("total_records"):
        raise HTTPException(status_code=404, detail=f"no cached data for {symbol!r}")

    yearly = [
        {"year": y["year"], "count": y["count"]}
        for y in sorted(cov.get("yearly_stats", []), key=lambda x: x["year"])
    ]

    # Recent closes for a sparkline.
    end = datetime.now(UTC).date()
    start = end - timedelta(days=120)
    recent: list[dict] = []
    try:
        df = cache.get_prices(symbol, start_date=start, end_date=end)
        if df is not None and len(df):
            for _, row in df.tail(60).iterrows():
                d = row["date"]
                recent.append(
                    {
                        "date": d.date().isoformat() if hasattr(d, "date") else str(d),
                        "close": float(row["close"]),
                    }
                )
    except Exception:  # sparkline is best-effort
        recent = []

    return {
        "symbol": symbol,
        "start_date": _iso(cov.get("start_date")),
        "end_date": _iso(cov.get("end_date")),
        "total_records": cov.get("total_records", 0),
        "yearly": yearly,
        "recent": recent,
    }


@router.get("/stocks/{symbol}/series")
def stock_series(
    symbol: str,
    start: date | None = None,
    end: date | None = None,
    st_period: int | None = None,
    st_multiplier: float | None = None,
    st_timeframe: str = "daily",
    cache=Depends(get_data_cache),
) -> dict:
    """Daily OHLC for an equity symbol, optionally with a SuperTrend line + direction overlaid
    (when ``st_period`` & ``st_multiplier`` are given). Powers the trade-analysis charts."""
    end = end or datetime.now(UTC).date()
    start = start or (end - timedelta(days=400))
    # SuperTrend cold-starts (ATR needs ~period bars) — fetch a warmup buffer BEFORE start so the
    # overlay's direction converges (matching the engine/TradingView), then display only [start, end].
    st_on = bool(st_period and st_multiplier)
    buffer_days = {"daily": 400, "weekly": 1500, "monthly": 3000}.get(st_timeframe.lower(), 400) if st_on else 0
    fetch_start = start - timedelta(days=buffer_days)
    try:
        df = cache.get_prices(symbol, start_date=fetch_start, end_date=end)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"no cached data for {symbol!r}: {exc}") from exc
    if df is None or len(df) == 0:
        raise HTTPException(status_code=404, detail=f"no cached data for {symbol!r}")
    df = df.copy()

    st_by_date: dict[str, tuple] = {}
    if st_on:
        from skas_algo.engine.indicators.supertrend import supertrend_bands

        try:
            st = supertrend_bands(df, period=int(st_period), multiplier=float(st_multiplier),
                                  timeframe=st_timeframe)
            for ts, row in st.iterrows():
                key = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                st_by_date[key] = (row["supertrend"], row["direction"])
        except Exception:  # overlay is best-effort; price still renders
            st_by_date = {}

    def _num(v):
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

    points: list[dict] = []
    for _, row in df.iterrows():
        d = row["date"]
        dd = d.date() if hasattr(d, "date") else d
        if isinstance(dd, date) and dd < start:
            continue  # warmup buffer — feeds the indicator only, not displayed
        ds = dd.isoformat() if hasattr(dd, "isoformat") else str(dd)[:10]
        pt = {
            "date": ds,
            "open": _num(row.get("open", row["close"])),
            "high": _num(row["high"]),
            "low": _num(row["low"]),
            "close": _num(row["close"]),
        }
        if ds in st_by_date:
            line, dirn = st_by_date[ds]
            if _num(line) is not None:
                pt["supertrend"] = _num(line)
            if _num(dirn) is not None:
                pt["direction"] = _num(dirn)
        points.append(pt)
    return {"symbol": symbol, "points": points}


# ====================================================================== options
# These read the NSE options DuckDB (no Kite session). Refresh downloads the public
# F&O bhavcopy, so it also works without a broker login — unlike equity refresh which
# lives under /brokers and needs the Kite session.


def _coverage_payload(cov: dict) -> dict:
    return {
        "symbol": cov.get("symbol"),
        "start_date": _iso(cov.get("start_date")),
        "end_date": _iso(cov.get("end_date")),
        "total_records": cov.get("total_records", 0),
        "trading_days": cov.get("trading_days", 0),
    }


@router.get("/options/underlyings")
def options_underlyings(cache=Depends(get_data_cache)) -> dict:
    try:
        available = list(cache.list_option_underlyings())
    except Exception:
        available = []
    # GOLD is synthetic — "available" once its futures spot series is cached.
    for u in SYNTHETIC_UNDERLYINGS:
        try:
            if (cache.get_coverage_stats(u) or {}).get("total_records"):
                available.append(u)
        except Exception:
            pass
    return {"supported": SUPPORTED_UNDERLYINGS + SYNTHETIC_UNDERLYINGS, "available": available}


@router.get("/options/{underlying}/coverage")
def options_coverage_route(underlying: str, cache=Depends(get_data_cache)) -> dict:
    u = underlying.upper()
    if u in SYNTHETIC_UNDERLYINGS:
        # Synthetic coverage = the span of the cached underlying (GOLD futures) series.
        cov = cache.get_coverage_stats(u) or {}
        if not cov.get("total_records"):
            raise HTTPException(
                status_code=404,
                detail=f"no cached {u} series yet — refresh GOLD futures from the Brokers screen",
            )
        n = cov.get("total_records", 0)
        return {"symbol": u, "start_date": _iso(cov.get("start_date")),
                "end_date": _iso(cov.get("end_date")), "total_records": n, "trading_days": n}
    cov = cache.options_coverage(u) or {}
    if not cov.get("total_records"):
        raise HTTPException(status_code=404, detail=f"no cached options for {underlying!r}")
    return _coverage_payload(cov)


@router.get("/options/{underlying}/expiries")
def options_expiries(underlying: str, date: str | None = None, cache=Depends(get_data_cache)) -> dict:
    u = underlying.upper()
    if u in SYNTHETIC_UNDERLYINGS:
        on = _parse_date(date) if date else datetime.now(UTC).date()
        return {"underlying": u, "date": date, "expiries": [_iso(e) for e in synthetic_expiries(u, on)]}
    on = _parse_date(date) if date else None
    exps = cache.list_option_expiries(u, on_date=on)
    return {"underlying": u, "date": date, "expiries": [_iso(e) for e in exps]}


def _pivot_chain(df, on: date, expiry: date, spot: float | None, with_greeks: bool, r: float,
                 q: float = 0.0) -> list[dict]:
    """Pivot a (strike × CE/PE) option chain into one row per strike.

    ``q=r`` switches the IV/greeks convention to Black-76 — used for synthetic
    futures-options chains (GOLD), which are generated under that model."""
    if df is None or len(df) == 0:
        return []
    t = max((expiry - on).days, 0) / 365.0
    rows: list[dict] = []
    for strike, grp in df.groupby("strike_price"):
        entry: dict = {"strike": float(strike), "ce": None, "pe": None}
        for _, row in grp.iterrows():
            ot = str(row.get("option_type") or "").upper()
            side = "ce" if ot == "CE" else "pe" if ot == "PE" else None
            if side is None:
                continue
            ltp, close = _num(row.get("ltp")), _num(row.get("close"))
            leg = {
                "ltp": ltp,
                "close": close,
                "oi": _int(row.get("open_interest")),
                "change_in_oi": _int(row.get("change_in_oi")),
            }
            if with_greeks and spot and t > 0:
                px = ltp or close
                iv = implied_vol(px, spot, float(strike), t, r, ot, q=q) if px else None
                if iv:
                    g = greeks(spot, float(strike), t, r, iv, ot, q=q)
                    leg.update({"iv": iv, "delta": g["delta"], "gamma": g["gamma"],
                                "theta": g["theta"], "vega": g["vega"]})
                else:
                    leg.update({"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None})
            entry[side] = leg
        rows.append(entry)
    rows.sort(key=lambda x: x["strike"])
    return rows


@router.get("/options/{underlying}/chain")
def options_chain(
    underlying: str,
    date: str,
    expiry: str,
    greeks: bool = False,  # noqa: A002 — query flag, not the bs.greeks fn
    r: float = DEFAULT_RISK_FREE,
    cache=Depends(get_data_cache),
) -> dict:
    u = underlying.upper()
    on, exp = _parse_date(date), _parse_date(expiry)
    synthetic = u in SYNTHETIC_UNDERLYINGS
    if synthetic:
        spot, _vol, df = synthetic_chain_for_view(cache, u, on, exp, r=r)
    else:
        df = cache.get_option_chain(u, on, expiry=exp)
        spot = make_spot_provider(cache)(u, on)
    rows = _pivot_chain(df, on, exp, spot, greeks, r, q=r if synthetic else 0.0)
    atm = None
    if spot is not None and rows:
        atm = min(rows, key=lambda x: abs(x["strike"] - spot))["strike"]
    return {
        "underlying": u, "date": date, "expiry": expiry,
        "spot": spot, "atm_strike": atm, "rows": rows, "synthetic": synthetic,
    }


# ------------------------------------------------ live option chain (Zerodha)
def _live_adapter(broker_account_id: int, db: Session):
    """Adapter for a logged-in account, for real-time option quotes. 4xx if no session."""
    account = db.get(BrokerAccount, broker_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="broker account not found")
    if not broker_svc.has_valid_session(account):
        raise HTTPException(
            status_code=400,
            detail="broker account has no valid session — log in (paste request token) first",
        )
    return broker_svc.make_adapter(account)


@router.get("/options/live/underlyings")
def options_live_underlyings(broker_account_id: int, db: Session = Depends(get_db)) -> dict:
    """Every F&O underlying Kite currently lists (indices + stocks), from the live session."""
    adapter = _live_adapter(broker_account_id, db)
    try:
        return {"underlyings": adapter.option_underlyings()}
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - network/API hiccup
        raise HTTPException(status_code=502, detail=f"live instruments fetch failed: {exc}") from exc


@router.get("/options/live/{underlying}/expiries")
def options_live_expiries(underlying: str, broker_account_id: int, db: Session = Depends(get_db)) -> dict:
    adapter = _live_adapter(broker_account_id, db)
    try:
        return {"underlying": underlying.upper(), "date": None, "expiries": adapter.option_expiries(underlying)}
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=502, detail=f"live expiries fetch failed: {exc}") from exc


@router.get("/options/live/{underlying}/chain")
def options_live_chain(
    underlying: str, expiry: str, broker_account_id: int, db: Session = Depends(get_db),
) -> dict:
    """Real-time chain (per-strike CE/PE LTP + OI, live spot, ATM, lot size) for one expiry."""
    adapter = _live_adapter(broker_account_id, db)
    try:
        ch = adapter.live_option_chain(underlying, expiry)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"live chain fetch failed: {exc}") from exc
    if ch is None:
        raise HTTPException(status_code=404, detail=f"no listed {underlying.upper()} options for {expiry}")
    return {"underlying": underlying.upper(), "date": datetime.now(UTC).date().isoformat(),
            "expiry": expiry, "live": True, **ch}


class _RefreshBody(BaseModel):
    underlyings: list[str]
    start_date: str
    end_date: str


def _validate_refresh(body: _RefreshBody) -> tuple[list[str], date, date]:
    unders = [u.upper() for u in body.underlyings]
    bad = [u for u in unders if u not in SUPPORTED_UNDERLYINGS]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported underlyings {bad}; supported: {SUPPORTED_UNDERLYINGS}",
        )
    start, end = _parse_date(body.start_date), _parse_date(body.end_date)
    if (end - start).days > MAX_REFRESH_DAYS:
        raise HTTPException(status_code=400, detail=f"range too large (max {MAX_REFRESH_DAYS} days/call)")
    return unders, start, end


@router.post("/options/refresh")
def options_refresh(body: _RefreshBody, cache=Depends(get_data_cache)) -> dict:
    unders, start, end = _validate_refresh(body)
    return cache.refresh_options(unders, start, end)


# ====================================================================== futures
@router.get("/futures/underlyings")
def futures_underlyings(cache=Depends(get_data_cache)) -> dict:
    try:
        available = cache.list_future_underlyings()
    except Exception:
        available = []
    return {"supported": SUPPORTED_UNDERLYINGS, "available": available}


@router.get("/futures/{underlying}/coverage")
def futures_coverage_route(underlying: str, cache=Depends(get_data_cache)) -> dict:
    cov = cache.futures_coverage(underlying.upper()) or {}
    if not cov.get("total_records"):
        raise HTTPException(status_code=404, detail=f"no cached futures for {underlying!r}")
    return _coverage_payload(cov)


@router.get("/futures/{underlying}/series")
def futures_series(
    underlying: str,
    expiry: str | None = None,
    start: str | None = None,
    end: str | None = None,
    cache=Depends(get_data_cache),
) -> dict:
    """Front-month continuous series by default; one contract if ``expiry`` is given."""
    df = cache.get_future_series(
        underlying.upper(),
        expiry=_parse_date(expiry) if expiry else None,
        start_date=_parse_date(start) if start else None,
        end_date=_parse_date(end) if end else None,
    )
    points: list[dict] = []
    if df is not None and len(df):
        for _, row in df.iterrows():
            points.append({
                "date": _iso(row.get("trade_date")),
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": _num(row.get("close")),
                "settle": _num(row.get("settle_price")),
                "oi": _int(row.get("open_interest")),
                "expiry": _iso(row.get("expiry_date")),
            })
    return {"underlying": underlying.upper(), "points": points}


@router.post("/futures/refresh")
def futures_refresh(body: _RefreshBody, cache=Depends(get_data_cache)) -> dict:
    unders, start, end = _validate_refresh(body)
    return cache.refresh_futures(unders, start, end)
