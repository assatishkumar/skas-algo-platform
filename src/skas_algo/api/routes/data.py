"""Read-only views over the skas-data historical cache (the 'Data' screen).

Uses the cache_only skas-data instance — no broker session needed. Refreshing the
cache (writing) lives under /brokers and runs on the shared Kite session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from skas_algo.data.provider import get_data_cache

router = APIRouter(tags=["data"], prefix="/data")

STALE_DAYS = 5  # lenient (covers weekends/holidays)


def _iso(d) -> str | None:
    if d is None:
        return None
    return d.date().isoformat() if hasattr(d, "date") and not isinstance(d, str) else str(d)


@router.get("/summary")
def summary(cache=Depends(get_data_cache)) -> dict:
    symbols = cache.list_cached_symbols()
    db_path = getattr(getattr(cache, "storage", None), "db_path", None)
    return {"symbol_count": len(symbols), "db_path": str(db_path) if db_path else None}


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
