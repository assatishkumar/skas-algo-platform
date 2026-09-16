"""US daily bars — the second market's price history (2026-09-16), one ``csv.gz`` per
symbol under ``~/.skas_data/us_daily/`` (``SKAS_US_DAILY_DIR``; tests get a tmp dir).

The Indian equity backtest reads the sibling skas-data DuckDB cache (Kite-fed, NSE
symbols). Nothing in the engine cares where bars come from — `BacktestRunner` /
`MarketView` take a `PriceLoader` (`engine/market.py`), the calendar is the union of the
data's dates, the equity multiplier is 1 — so a US run only needs a store and a loader
with the same contract. This is that store: the `data/intraday_bars.py` shape (plain
csv.gz, no DuckDB, never deletes) fed from Yahoo's chart endpoint, the one
`data/global_quotes.py` already uses for the portfolio's US quotes.

Facts that shaped it:
- Yahoo serves ``range=10y&interval=1d`` per symbol in ONE call (2,513 bars for AAPL on
  2026-09-16); the multi-symbol endpoint 401s, so it is one request per symbol, throttled
  — ~600 names in ~6 minutes, once, then tail top-ups.
- The OHLC Yahoo returns is SPLIT-adjusted and NOT dividend-adjusted. That is what a
  trend strategy wants (the price levels that traded); ``adjclose`` is stored beside it
  for anyone who needs total return, never used by the loader.
- A dotted class ticker (``BRK.B``) is ``BRK-B`` at Yahoo; the store keeps the index's
  spelling and maps on the wire.
- A symbol Yahoo cannot serve keeps whatever file it had; a fetch never shrinks a store.

Loader contract (quoted from `engine/market.py`): a DataFrame with a ``date`` column +
``open/high/low/close``, ascending, inclusive of both bounds, EMPTY (not None) on a miss.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger("skas_algo.data")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; skas-algo/1.0)"}   # a bare client gets 401
_TIMEOUT = 20
_THROTTLE_S = 0.25
COLUMNS = ["date", "open", "high", "low", "close", "volume", "adjclose"]


def store_dir() -> Path:
    return Path(os.environ.get("SKAS_US_DAILY_DIR", "~/.skas_data/us_daily")).expanduser()


def _path(symbol: str) -> Path:
    return store_dir() / f"{symbol.upper().replace('/', '-')}.csv.gz"


def yahoo_symbol(symbol: str) -> str:
    """The index's spelling → Yahoo's: ``BRK.B`` → ``BRK-B``, ``BF.B`` → ``BF-B``."""
    return symbol.upper().replace(".", "-")


# ------------------------------------------------------------------ fetch
def parse_chart(body: dict, symbol: str) -> pd.DataFrame:
    """Yahoo's chart JSON → a COLUMNS frame, ascending, null rows dropped."""
    result = (body.get("chart") or {}).get("result") or []
    if not result:
        err = ((body.get("chart") or {}).get("error") or {}).get("description", "no result")
        raise ValueError(f"{symbol}: {err}")
    node = result[0]
    ts = node.get("timestamp") or []
    q = ((node.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((node.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    for i, t in enumerate(ts):
        try:
            o, h = q["open"][i], q["high"][i]
            lo, c = q["low"][i], q["close"][i]
        except (KeyError, IndexError, TypeError):
            continue
        if c is None or o is None or h is None or lo is None:
            continue
        v = (q.get("volume") or [None] * len(ts))[i]
        a = adj[i] if i < len(adj) else None
        rows.append({"date": datetime.utcfromtimestamp(int(t)).date().isoformat(),
                     "open": float(o), "high": float(h), "low": float(lo), "close": float(c),
                     "volume": float(v) if v is not None else 0.0,
                     "adjclose": float(a) if a is not None else float(c)})
    df = pd.DataFrame(rows, columns=COLUMNS)
    # one row per date (Yahoo occasionally repeats the live bar), ascending
    return df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def fetch_history(symbol: str, *, rng: str = "10y", http=None) -> pd.DataFrame:
    """``range`` ∈ 1mo/3mo/6mo/1y/2y/5y/10y/max. ``http`` (a callable returning the
    parsed JSON) is injectable for tests."""
    if http is None:
        def http(sym: str, r: str) -> dict:
            resp = requests.get(CHART_URL.format(symbol=yahoo_symbol(sym)),
                                params={"range": r, "interval": "1d", "events": "div,splits"},
                                headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
    return parse_chart(http(symbol, rng), symbol)


def _range_for_gap(last: date | None, today: date) -> str:
    """The smallest Yahoo range that covers the days since the last stored bar."""
    if last is None:
        return "10y"
    gap = (today - last).days
    if gap <= 20:
        return "1mo"
    if gap <= 80:
        return "3mo"
    if gap <= 170:
        return "6mo"
    if gap <= 350:
        return "1y"
    if gap <= 700:
        return "2y"
    return "10y"


# ------------------------------------------------------------------ store
def _read(symbol: str) -> pd.DataFrame:
    p = _path(symbol)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(p)
    return df[[c for c in COLUMNS if c in df.columns]]


def last_date(symbol: str) -> date | None:
    df = _read(symbol)
    return date.fromisoformat(str(df["date"].max())) if len(df) else None


def cached_symbols() -> set[str]:
    d = store_dir()
    if not d.is_dir():
        return set()
    return {p.name[:-len(".csv.gz")] for p in d.glob("*.csv.gz")}


def refresh(symbols: list[str], *, http=None, throttle: float = _THROTTLE_S,
            today: date | None = None, progress=None) -> dict[str, dict]:
    """Fetch or top up each symbol. Existing files fetch only the tail (the smallest range
    that covers the gap) and merge — existing rows are REPLACED by fresher ones for the
    same date (a split re-bases history; Yahoo's tail is already re-based, and a 1-month
    tail never re-bases a 10-year file: a symbol with a split needs a full refetch, which
    ``refresh(..., full=…)`` callers get by deleting the file). Never deletes; a failed
    symbol keeps its file and is reported, not raised."""
    today = today or date.today()
    out: dict[str, dict] = {}
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i, sym in enumerate(symbols):
        sym = sym.upper()
        try:
            have = _read(sym)
            last = date.fromisoformat(str(have["date"].max())) if len(have) else None
            got = fetch_history(sym, rng=_range_for_gap(last, today), http=http)
            if got.empty:
                raise ValueError("no bars")
            merged = (pd.concat([have, got], ignore_index=True)
                      .drop_duplicates("date", keep="last").sort_values("date")
                      .reset_index(drop=True))
            merged.to_csv(_path(sym), index=False)
            out[sym] = {"ok": True, "rows": int(len(merged)), "added": int(len(merged) - len(have)),
                        "first": str(merged["date"].iloc[0]), "last": str(merged["date"].iloc[-1])}
        except Exception as exc:
            out[sym] = {"ok": False, "error": str(exc)[:200]}
            logger.warning("us_daily %s: %s", sym, exc)
        if progress is not None:
            progress(i + 1, len(symbols), sym, out[sym])
        if throttle and i + 1 < len(symbols):
            time.sleep(throttle)
    return out


def load(symbol: str, start: date, end: date) -> pd.DataFrame:
    """The `PriceLoader` contract: date/open/high/low/close, ascending, inclusive; an
    EMPTY frame for an unknown symbol."""
    df = _read(symbol)
    if df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    m = (df["date"] >= start.isoformat()) & (df["date"] <= end.isoformat())
    out = df.loc[m, ["date", "open", "high", "low", "close", "volume"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    return out.reset_index(drop=True)


def coverage(stale_days: int = 5) -> dict:
    """The Data page's card: how many symbols, the common first/last bar, the stale ones."""
    syms = sorted(cached_symbols())
    firsts, lasts, stale = [], [], []
    cutoff = date.today() - timedelta(days=stale_days)
    for s in syms:
        df = _read(s)
        if df.empty:
            continue
        f, la = str(df["date"].iloc[0]), str(df["date"].iloc[-1])
        firsts.append(f)
        lasts.append(la)
        if date.fromisoformat(la) < cutoff:
            stale.append(s)
    return {"symbols": len(syms), "first": min(firsts) if firsts else None,
            "last": max(lasts) if lasts else None, "oldest_last": min(lasts) if lasts else None,
            "stale": stale[:50], "stale_count": len(stale), "dir": str(store_dir())}


# ------------------------------------------------------------------ background refresh
_LOCK = threading.Lock()
_JOB: dict = {"running": False, "done": 0, "total": 0, "symbol": None, "errors": 0,
              "started_at": None, "finished_at": None, "result": None}


def job_snapshot() -> dict:
    with _LOCK:
        return dict(_JOB)


def start_refresh_job(symbols: list[str], *, http=None) -> bool:
    """Run `refresh` on a thread; False when one is already running."""
    with _LOCK:
        if _JOB["running"]:
            return False
        _JOB.update({"running": True, "done": 0, "total": len(symbols), "symbol": None,
                     "errors": 0, "started_at": datetime.now().isoformat(timespec="seconds"),
                     "finished_at": None, "result": None})

    def _prog(done, total, sym, res):
        with _LOCK:
            _JOB.update({"done": done, "total": total, "symbol": sym,
                         "errors": _JOB["errors"] + (0 if res.get("ok") else 1)})

    def _run():
        try:
            res = refresh(symbols, http=http, progress=_prog)
            with _LOCK:
                _JOB["result"] = {"ok": sum(1 for r in res.values() if r.get("ok")),
                                  "failed": [s for s, r in res.items() if not r.get("ok")][:50]}
        except Exception as exc:  # pragma: no cover - a job must always release the flag
            logger.exception("us_daily refresh job failed")
            with _LOCK:
                _JOB["result"] = {"error": str(exc)[:200]}
        finally:
            with _LOCK:
                _JOB["running"] = False
                _JOB["finished_at"] = datetime.now().isoformat(timespec="seconds")

    threading.Thread(target=_run, name="us-daily-refresh", daemon=True).start()
    return True
