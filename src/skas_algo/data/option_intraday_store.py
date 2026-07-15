"""Self-captured 1-minute option-contract bars — the platform's own GFD replacement.

GlobalDataFeeds 1-min NFO data is paid and ends "yesterday"; going forward the platform
builds its OWN dataset: every trading day after close, one Kite historical request per
in-universe option contract captures the day's 1-min OHLC + volume + OI (exact exchange
bars — NOT tick-built). The store is one **Parquet** file per trading day under
``~/.skas_data/option_intraday/1min/`` written/read via **DuckDB** (the venv has no
pyarrow — duckdb 1.5 ships Parquet natively; in-memory connections per call, so none of
the persistent-DuckDB single-writer locking applies). ~1-2 MB/day at zstd.

Schema: ``symbol,start,open,high,low,close,volume,oi`` — ``symbol`` is the platform's
internal option form (``NIFTY|2026-07-21|24000|CE``), ``start`` a naive-IST minute-START
datetime. Sparse like GFD: only minutes that traded appear. The store keeps ALL listed
strikes in-window (incl. NIFTY 50s) — the NIFTY-100s rule is a TRADING rule, not a data
rule (CLAUDE.md §8).

Capture-day criticality: an expired weekly vanishes from Kite's instruments dump, so its
expiry-day bars are unrecoverable if the capture misses that day — hence the manager's
same-day EOD task plus a small missing-days sweep (live/manager.py).
"""

from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, time
from pathlib import Path

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

OPTION_INTRADAY_DIR = Path.home() / ".skas_data" / "option_intraday" / "1min"
_THROTTLE_S = 0.35      # ~3 historical requests/sec allowed (intraday_bars.py precedent)
_SESSION_OPEN = time(9, 15)
_SESSION_CLOSE = time(15, 30)
COLUMNS = ["symbol", "start", "open", "high", "low", "close", "volume", "oi"]


def _fmt_strike(strike: float) -> str:
    # Mirrors engine/options/instrument._fmt_strike so symbols match the platform's form.
    return str(int(strike)) if float(strike).is_integer() else str(strike)


def option_symbol(underlying: str, expiry_iso: str, strike: float, right: str) -> str:
    """The internal option symbol (``NIFTY|2026-07-21|24000|CE``) without needing a lot size."""
    return f"{underlying.upper()}|{expiry_iso}|{_fmt_strike(strike)}|{right.upper()}"


def day_path(day: date | str) -> Path:
    d = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    return OPTION_INTRADAY_DIR / f"{d}.parquet"


def captured_days() -> list[str]:
    """ISO dates that have a day-file, sorted."""
    if not OPTION_INTRADAY_DIR.exists():
        return []
    return sorted(p.stem for p in OPTION_INTRADAY_DIR.glob("*.parquet"))


def write_day(day: date | str, df: pd.DataFrame) -> None:
    """Atomically write one day's bars as Parquet (zstd) via duckdb: tmp → rename, so a
    crash mid-write never leaves a half day-file that would block re-capture."""
    path = day_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    out = df[COLUMNS].copy()
    out["start"] = pd.to_datetime(out["start"])
    con = duckdb.connect()
    try:
        con.register("bars", out)
        dest = str(tmp).replace("'", "''")
        con.execute(
            f"COPY (SELECT * FROM bars ORDER BY symbol, start) "
            f"TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()
    tmp.rename(path)


def load_day(day: date | str) -> pd.DataFrame:
    """All bars of one day (all contracts), or an empty frame if the day isn't captured."""
    path = day_path(day)
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    con = duckdb.connect()
    try:
        df = con.execute(
            "SELECT * FROM read_parquet(?) ORDER BY symbol, start", [str(path)]
        ).df()
    finally:
        con.close()
    return df


def load_contract_bars(
    underlying: str,
    expiry: date | str,
    strike: float,
    right: str,
    start_day: date | str,
    end_day: date | str,
    minutes: int = 1,
) -> pd.DataFrame:
    """One contract's bars across [start_day, end_day], aggregated to ``minutes``.
    Columns start/open/high/low/close/volume/oi, sorted. DuckDB pushes the symbol
    predicate into the Parquet scan, so this stays fast as the store grows."""
    exp_iso = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)[:10]
    sym = option_symbol(underlying, exp_iso, strike, right)
    lo = start_day.isoformat() if hasattr(start_day, "isoformat") else str(start_day)[:10]
    hi = end_day.isoformat() if hasattr(end_day, "isoformat") else str(end_day)[:10]
    files = [str(day_path(d)) for d in captured_days() if lo <= d <= hi]
    if not files:
        return pd.DataFrame(columns=["start", "open", "high", "low", "close", "volume", "oi"])
    con = duckdb.connect()
    try:
        df = con.execute(
            "SELECT start, open, high, low, close, volume, oi FROM read_parquet(?) "
            "WHERE symbol = ? ORDER BY start",
            [files, sym],
        ).df()
    finally:
        con.close()
    return resample_bars(df, minutes)


def resample_bars(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate 1-min bars to N-min: o=first/h=max/l=min/c=last/vol=sum/oi=last. Buckets
    with no traded minute are dropped (sparse in, sparse out — GFD semantics)."""
    if minutes <= 1 or df.empty:
        return df.reset_index(drop=True)
    out = df.copy()
    out["start"] = pd.to_datetime(out["start"])
    g = out.set_index("start").groupby(pd.Grouper(freq=f"{minutes}min"))
    agg = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"), oi=("oi", "last"))
    return agg.dropna(subset=["open"]).reset_index()


def mirror_store(dest_dir: str | Path) -> dict:
    """Mirror the store into ``dest_dir`` — COPY day-files that are missing or differ
    (size/mtime), NEVER delete (backup semantics: a local mistake must not propagate a
    deletion to the backup). Point dest at a Google Drive for Desktop folder and the Drive
    app ships it off-box. Copies via a tmp name + rename so the Drive uploader never sees
    a half-written parquet."""
    import shutil

    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for day in captured_days():
        src = day_path(day)
        dst = dest / src.name
        s = src.stat()
        if dst.exists():
            d = dst.stat()
            if d.st_size == s.st_size and d.st_mtime >= s.st_mtime:
                skipped += 1
                continue
        tmp = dest / (src.name + ".tmp")
        shutil.copy2(src, tmp)
        tmp.rename(dst)
        copied += 1
    return {"dir": str(dest), "copied": copied, "skipped": skipped}


def store_summary(days_limit: int = 30) -> dict:
    """Inventory of the store for the Data page: totals over ALL day-files (cheap — parquet
    row counts come from metadata, sizes from stat) + per-day detail for the most recent
    ``days_limit`` days (rows, distinct contracts, per-underlying contracts, first/last bar
    minute, file size)."""
    all_days = captured_days()
    if not all_days:
        return {"days_total": 0, "rows_total": 0, "bytes_total": 0,
                "first_day": None, "last_day": None, "days": []}
    all_files = [str(day_path(d)) for d in all_days]
    bytes_total = sum(day_path(d).stat().st_size for d in all_days)
    recent = all_days[-max(1, days_limit):]
    files = [str(day_path(d)) for d in recent]
    con = duckdb.connect()
    try:
        rows_total = con.execute(
            "SELECT count(*) FROM read_parquet(?)", [all_files]).fetchone()[0]
        # Per (day, underlying): row + contract counts and the day's bar window.
        detail = con.execute(
            "SELECT regexp_extract(filename, '(\\d{4}-\\d{2}-\\d{2})', 1) AS day, "
            "       split_part(symbol, '|', 1) AS u, "
            "       count(*) AS rows, count(DISTINCT symbol) AS contracts, "
            "       min(start) AS first_bar, max(start) AS last_bar "
            "FROM read_parquet(?, filename=true) GROUP BY 1, 2 ORDER BY 1, 2",
            [files]).fetchall()
    finally:
        con.close()
    by_day: dict[str, dict] = {}
    for day, u, rows, contracts, first_bar, last_bar in detail:
        d = by_day.setdefault(day, {"day": day, "rows": 0, "contracts": 0,
                                    "underlyings": {}, "first_bar": None, "last_bar": None,
                                    "bytes": day_path(day).stat().st_size})
        d["rows"] += int(rows)
        d["contracts"] += int(contracts)
        d["underlyings"][u] = int(contracts)
        fb, lb = str(first_bar), str(last_bar)
        d["first_bar"] = fb if d["first_bar"] is None else min(d["first_bar"], fb)
        d["last_bar"] = lb if d["last_bar"] is None else max(d["last_bar"], lb)
    return {"days_total": len(all_days), "rows_total": int(rows_total),
            "bytes_total": int(bytes_total),
            "first_day": all_days[0], "last_day": all_days[-1],
            "days": sorted(by_day.values(), key=lambda d: d["day"], reverse=True)}


# ------------------------------------------------------------------ capture
def capture_day(
    adapter,
    day: date,
    *,
    underlyings: list[str],
    expiry_days: int = 40,
    strike_pct: float = 10.0,
    minutes: int = 1,
    progress=None,
) -> dict:
    """Fetch + persist one trading day's option bars for the configured universe.

    Universe: for each underlying, listed expiries within ``expiry_days`` of ``day`` and
    strikes within ±``strike_pct``% of the live spot (fallback: the median listed strike).
    One ``kite.historical_data(..., oi=True)`` call per contract, throttled ~3/s; a single
    contract's failure is counted, never fatal (a partial day beats none). No-op when the
    day-file already exists. The file is only written when at least one bar came back —
    an all-errors day (dead subscription) leaves no file, so the sweep retries it.
    ``progress(done, total)`` (optional) is called per contract — the universe is
    enumerated UP FRONT so total is known from the first call (the Data-page indicator)."""
    path = day_path(day)
    if path.exists():
        return {"day": day.isoformat(), "skipped": "exists"}
    adapter._build_nfo()
    kite = adapter._kite_client()
    frm = datetime.combine(day, _SESSION_OPEN)
    to = datetime.combine(day, _SESSION_CLOSE)

    # Enumerate the whole universe first so progress has a denominator.
    todo: list[tuple[str, str, float, str, int]] = []  # (u, expiry_iso, strike, right, token)
    for u in [x.upper() for x in underlyings]:
        by_expiry = adapter._nfo_index.get(u, {})
        exps = sorted(e for e in by_expiry
                      if 0 <= (date.fromisoformat(e) - day).days <= expiry_days)
        if not exps:
            continue
        try:
            spot = adapter.underlying_ltp(u)
        except Exception:  # pragma: no cover - spot is only the window center
            spot = None
        for e in exps:
            strikes = sorted(by_expiry[e])
            center = float(spot) if spot else float(strikes[len(strikes) // 2])
            lo_k = center * (1 - strike_pct / 100.0)
            hi_k = center * (1 + strike_pct / 100.0)
            for k in strikes:
                if not lo_k <= k <= hi_k:
                    continue
                for right in ("CE", "PE"):
                    token = adapter._nfo_token.get((u, e, float(k), right))
                    if token:
                        todo.append((u, e, k, right, token))

    total = len(todo)
    if progress is not None:
        progress(0, total)
    # Kite's 1-min interval is named "minute" (NOT "1minute" — that string is rejected and
    # silently failed EVERY call on the first live run, 2026-07-15).
    interval = "minute" if minutes == 1 else f"{minutes}minute"
    rows: list[dict] = []
    with_data = errors = 0
    for i, (u, e, k, right, token) in enumerate(todo, 1):
        try:
            bars = kite.historical_data(token, frm, to, interval, oi=True)
        except Exception:
            errors += 1
            bars = []
        _time.sleep(_THROTTLE_S)
        if progress is not None:
            progress(i, total)
        if i % 250 == 0:
            logger.info("option-bar capture %s: %d/%d contracts (%d with data, %d errors)",
                        day, i, total, with_data, errors)
        if not bars:
            continue
        with_data += 1
        sym = option_symbol(u, e, k, right)
        for b in bars:
            ts = b.get("date")
            start = ts.replace(tzinfo=None) if hasattr(ts, "replace") else pd.to_datetime(ts)
            rows.append({
                "symbol": sym, "start": start,
                "open": float(b["open"]), "high": float(b["high"]),
                "low": float(b["low"]), "close": float(b["close"]),
                "volume": float(b.get("volume") or 0.0),
                "oi": float(b.get("oi") or 0.0),
            })
    summary = {"day": day.isoformat(), "contracts": total, "with_data": with_data,
               "rows": len(rows), "errors": errors}
    if rows:
        write_day(day, pd.DataFrame(rows))
    else:
        logger.warning("option-bar capture wrote nothing for %s (%s)", day, summary)
    return summary
