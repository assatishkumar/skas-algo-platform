"""GlobalDataFeeds 1-min CSV → the option-intraday Parquet store.

The owner's purchased GFD history ("until yesterday") and the platform's self-captured
days (from now on) become ONE continuous dataset: this importer converts GFD's per-day
NFO files into the same per-day Parquet layout ``option_intraday_store`` writes.

GFD format (profiled from GFDLNFO_NIFTY_BANKNIFTY_01072025.csv):
``Ticker,Date,Time,Open,High,Low,Close,Volume,Open Interest`` — 1-min bars, only traded
minutes; tickers ``NIFTY03JUL2522800CE.NFO`` (DDMMMYY expiry); continuous futures rows
(``NIFTY-I.NFO`` …) which v1 SKIPS (counted, logged); dates DD/MM/YYYY; times stamp the
minute END (``10:58:59`` = the 10:58 bar) — converted to minute-START to match Kite.

Idempotent: rows merge into any existing day-file deduped on (symbol, start), with the
EXISTING rows winning (a self-captured day is never overwritten by an import).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from .option_intraday_store import COLUMNS, load_day, option_symbol, write_day

logger = logging.getLogger(__name__)

# NIFTY03JUL2522800CE.NFO → (NIFTY, 03JUL25, 22800, CE). Futures (NIFTY-I.NFO) don't match.
_TICKER_RE = re.compile(r"^([A-Z]+?)(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)\.NFO$")


def _ticker_to_symbol(ticker: str) -> str | None:
    m = _TICKER_RE.match(ticker.strip())
    if not m:
        return None
    u, exp_s, strike_s, right = m.groups()
    try:
        expiry = datetime.strptime(exp_s, "%d%b%y").date()  # %b is case-insensitive
    except ValueError:
        return None
    return option_symbol(u, expiry.isoformat(), float(strike_s), right)


def import_gfd_file(path: Path) -> dict:
    """Import one GFD CSV into the store. Returns {file, days:{iso:rows}, rows,
    skipped_tickers}."""
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    mapping = {t: _ticker_to_symbol(str(t)) for t in df["Ticker"].unique()}
    skipped = sorted(t for t, s in mapping.items() if s is None)
    if skipped:
        logger.info("import-gfd %s: skipping %d non-option tickers (futures etc.): %s%s",
                    path.name, len(skipped), ", ".join(skipped[:6]),
                    "…" if len(skipped) > 6 else "")
    df["symbol"] = df["Ticker"].map(mapping)
    df = df[df["symbol"].notna()].copy()
    if df.empty:
        return {"file": str(path), "days": {}, "rows": 0, "skipped_tickers": len(skipped)}
    # GFD stamps the minute END (…:59) — floor to the minute START (Kite convention).
    df["start"] = pd.to_datetime(
        df["Date"].astype(str) + " " + df["Time"].astype(str),
        format="%d/%m/%Y %H:%M:%S",
    ).dt.floor("min")
    out = pd.DataFrame({
        "symbol": df["symbol"],
        "start": df["start"],
        "open": df["Open"].astype(float),
        "high": df["High"].astype(float),
        "low": df["Low"].astype(float),
        "close": df["Close"].astype(float),
        "volume": df["Volume"].astype(float),
        "oi": df["Open Interest"].astype(float),
    })

    days: dict[str, int] = {}
    for day, chunk in out.groupby(out["start"].dt.date):
        existing = load_day(day)
        if existing.empty:
            merged = chunk[COLUMNS]
        else:
            existing["start"] = pd.to_datetime(existing["start"])
            merged = pd.concat([existing[COLUMNS], chunk[COLUMNS]], ignore_index=True)
            # Existing (self-captured) rows win on conflict — keep="first".
            merged = merged.drop_duplicates(subset=["symbol", "start"], keep="first")
        write_day(day, merged)
        days[day.isoformat()] = int(len(merged))
    return {"file": str(path), "days": days, "rows": int(len(out)),
            "skipped_tickers": len(skipped)}


def import_gfd(paths: list[str]) -> dict:
    """Import files and/or directories (``*.csv`` inside dirs). Returns aggregate totals."""
    files: list[Path] = []
    for p in paths:
        pt = Path(p).expanduser()
        if pt.is_dir():
            files.extend(sorted(pt.glob("*.csv")))
        elif pt.exists():
            files.append(pt)
        else:
            logger.warning("import-gfd: %s not found — skipped", p)
    results = [import_gfd_file(f) for f in files]
    days: dict[str, int] = {}
    for r in results:
        days.update(r["days"])
    return {"files": len(results), "days": days,
            "rows": sum(r["rows"] for r in results),
            "skipped_tickers": sum(r["skipped_tickers"] for r in results)}
