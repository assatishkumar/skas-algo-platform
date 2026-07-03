"""15-minute NIFTY spot bars for the momentum-theta backtest — Kite-fetched, locally cached.

No intraday data exists in skas-data (its DuckDB schemas are strictly one-row-per-day), so
this module owns its own store: a csv.gz per (symbol, interval) under
``~/.skas_data/intraday/`` (no parquet engine in the venv; ~26k rows loads in ms), topped
up incrementally from Kite historical data via an existing broker session. skas-algo-only —
no skas-data schema change.

Kite serves 15-minute candles years back but caps each request at ~200 days, so fetches are
chunked (and lightly throttled — historical API allows ~3 req/s). The Zerodha instrument
token is resolved from a live ``ltp()`` call, same trick as ``ZerodhaAdapter.intraday_bars``.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

INTRADAY_DIR = Path.home() / ".skas_data" / "intraday"
_CHUNK_DAYS = 190       # Kite 15-minute limit is ~200 days/request
_THROTTLE_S = 0.35      # ~3 historical requests/sec allowed


def _store_path(symbol: str, minutes: int) -> Path:
    safe = symbol.replace(" ", "").replace(":", "")
    return INTRADAY_DIR / f"{safe}_{minutes}min.csv.gz"


def cached_range(symbol: str = "NSE:NIFTY 50", minutes: int = 15) -> tuple[str, str] | None:
    """(first, last) bar-start ISO in the local store, or None when nothing is cached."""
    path = _store_path(symbol, minutes)
    if not path.exists():
        return None
    df = pd.read_csv(path, usecols=["start"])
    if df.empty:
        return None
    return str(df["start"].min()), str(df["start"].max())


def load_intraday_bars(
    start: date,
    end: date,
    *,
    adapter=None,
    symbol: str = "NSE:NIFTY 50",
    minutes: int = 15,
) -> pd.DataFrame:
    """15-min OHLC bars for [start, end], columns start/open/high/low/close (start =
    naive-IST datetime, sorted). Serves from the local store and, when an ``adapter``
    (a logged-in ZerodhaAdapter) is given, fetches + persists whatever is missing.
    Without an adapter it returns whatever the store already covers."""
    path = _store_path(symbol, minutes)
    have = pd.read_csv(path) if path.exists() else pd.DataFrame(
        columns=["start", "open", "high", "low", "close"])
    if not have.empty:
        have["start"] = pd.to_datetime(have["start"])

    if adapter is not None:
        missing = _missing_windows(have, start, end)
        fetched = [_fetch_window(adapter, symbol, minutes, lo, hi) for lo, hi in missing]
        fetched = [f for f in fetched if f is not None and not f.empty]
        if fetched:
            have = pd.concat([have, *fetched], ignore_index=True)
            have["start"] = pd.to_datetime(have["start"])  # concat can leave object dtype
            have = have.drop_duplicates(subset="start").sort_values("start")
            INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
            have.to_csv(path, index=False)

    if not have.empty:
        have["start"] = pd.to_datetime(have["start"])
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end) + pd.Timedelta(days=1)
    out = have[(have["start"] >= lo) & (have["start"] < hi)]
    return out.sort_values("start").reset_index(drop=True)


def _missing_windows(have: pd.DataFrame, start: date, end: date) -> list[tuple[date, date]]:
    """The (at most two) date windows the store doesn't cover: before its first bar and
    after its last. Interior gaps (holidays) are not re-fetched."""
    if have.empty:
        return [(start, end)]
    first, last = have["start"].min().date(), have["start"].max().date()
    windows = []
    if start < first:
        windows.append((start, first - timedelta(days=1)))
    if end > last:
        windows.append((last, end))  # refetch the last cached day too (it may be partial)
    return [(a, b) for a, b in windows if a <= b]


def _fetch_window(adapter, symbol: str, minutes: int, lo: date, hi: date) -> pd.DataFrame | None:
    kite = adapter._kite_client()
    try:
        token = kite.ltp([symbol]).get(symbol, {}).get("instrument_token")
    except Exception:
        return None
    if not token:
        return None
    rows: list[dict] = []
    cur = lo
    while cur <= hi:
        chunk_end = min(cur + timedelta(days=_CHUNK_DAYS), hi)
        try:
            bars = kite.historical_data(
                token,
                datetime(cur.year, cur.month, cur.day),
                datetime(chunk_end.year, chunk_end.month, chunk_end.day, 23, 59),
                f"{minutes}minute",
            )
        except Exception:  # pragma: no cover - one bad chunk shouldn't void the rest
            bars = []
        for b in bars:
            ts = b.get("date")
            start = ts.replace(tzinfo=None) if hasattr(ts, "replace") else pd.to_datetime(ts)
            rows.append({"start": start, "open": float(b["open"]), "high": float(b["high"]),
                         "low": float(b["low"]), "close": float(b["close"])})
        cur = chunk_end + timedelta(days=1)
        _time.sleep(_THROTTLE_S)
    return pd.DataFrame(rows) if rows else None
