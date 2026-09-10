"""A daily ATM implied-vol history per underlying, built from the 1-min option store — the
series a TRUE IV rank needs (the strip carried a VIX rank as a stand-in until 2026-09-10).

One row per captured day: the ATM CE's (else PE's) implied vol at the 15:20 sample minute
on the expiry nearest 30 DTE (20–45 days; else the nearest ≥ 7 — a 30-day measure, so it
compares with the VIX rather than with the front weekly, whose IV balloons on its last
days). Solved with the same Black–Scholes and the same parity spot the console ladder
uses. Stored as `~/.skas_data/console/atm_iv_<U>.csv` (`SKAS_CONSOLE_DIR`), appended
incrementally — a day already in the file is never recomputed, so the maintenance loop
can top it up after the daily capture (~0.35 s a day; the first NIFTY build is ~7 min).

`iv_rank(underlying, day, iv_now)` answers the two rank flavours against the trailing
252 rows STRICTLY before ``day`` (no lookahead in a replay): the percentile of ``iv_now``
among them and the classic IVR = (now − low) / (high − low). Fewer than 60 rows → None.
"""

from __future__ import annotations

import bisect
import logging
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from skas_algo.data.option_intraday_store import captured_days, load_day
from skas_algo.engine.options import black_scholes as bs
from skas_algo.services.options_console.session import (
    _YEAR_S,
    EXPIRY_TIME,
    RISK_FREE,
    T_FLOOR_S,
)
from skas_algo.services.options_console.session import (
    pick_iv30_expiry as pick_expiry,
)
from skas_algo.services.options_console.store import console_dir
from skas_algo.services.replay_market import ReplayMarket

logger = logging.getLogger(__name__)

SAMPLE_AT = time(15, 20)
COLUMNS = ["day", "expiry", "dte", "spot", "atm", "iv"]
_MIN_ROWS = 60
_WINDOW = 252


def store_path(underlying: str) -> Path:
    return console_dir() / f"atm_iv_{underlying.upper()}.csv"


def load(underlying: str) -> pd.DataFrame:
    p = store_path(underlying)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(p)
    return df.sort_values("day").reset_index(drop=True)


def _t_years(expiry_iso: str, now: datetime) -> float:
    exp = datetime.combine(date.fromisoformat(expiry_iso), EXPIRY_TIME)
    return max(T_FLOOR_S, (exp - now).total_seconds()) / _YEAR_S


def sample_day(underlying: str, day: date, *, at: time = SAMPLE_AT) -> dict | None:
    """One row for ``day``: replay the tape to the sample minute, pick the ~30-DTE expiry,
    snap the parity spot to that expiry's strike grid and solve the ATM IV."""
    u = underlying.upper()
    df = load_day(day, underlying=u, columns=["symbol", "start", "close", "oi"])
    if df.empty:
        return None
    df = df.sort_values("start")
    key = datetime.combine(day, at)
    df = df[df["start"] <= key]
    if df.empty:
        return None
    symbols = df["symbol"].unique().tolist()
    expiries = sorted({s.split("|")[1] for s in symbols})
    expiry = pick_expiry(expiries, day)
    if expiry is None:
        return None
    m = ReplayMarket(u)
    m.start_day(day, symbols)
    last = None
    for sym, close, oi, start in zip(df["symbol"], df["close"], df["oi"], df["start"],
                                     strict=False):
        m.now = start.to_pydatetime() if hasattr(start, "to_pydatetime") else start
        m.feed(sym, float(close), float(oi) if oi == oi else 0.0)
        last = m.now
    if last is None:
        return None
    m._spot_dirty = True
    m.now = last
    spot = m.index_spot(u)
    if not spot:
        return None
    strikes = sorted({float(s.split("|")[2]) for s in symbols if s.split("|")[1] == expiry})
    if not strikes:
        return None
    atm = min(strikes, key=lambda k: abs(k - spot))
    t = _t_years(expiry, last)
    iv = None
    for right in ("CE", "PE"):
        sym = f"{u}|{expiry}|{int(atm)}|{right}"
        if sym in m.quotes:
            px = m.quotes[sym][0]
            iv = bs.implied_vol(px, spot, atm, t, RISK_FREE, right)
            if iv:
                break
    if not iv:
        return None
    return {"day": day.isoformat(), "expiry": expiry,
            "dte": (date.fromisoformat(expiry) - day).days,
            "spot": round(float(spot), 2), "atm": atm, "iv": round(float(iv) * 100.0, 2)}


def build(underlying: str, *, limit: int | None = None, progress=None) -> dict:
    """Append every captured day the store lacks. ``limit`` caps the days per call (the
    maintenance loop's budget); ``progress(done, total)`` is optional."""
    u = underlying.upper()
    have = load(u)
    done_days = set(have["day"].astype(str)) if len(have) else set()
    todo = [d for d in captured_days() if d not in done_days]
    if limit:
        todo = todo[-int(limit):] if len(todo) > int(limit) else todo
    rows = []
    for i, d in enumerate(todo, 1):
        try:
            row = sample_day(u, date.fromisoformat(d))
        except Exception:
            logger.exception("atm iv history: %s %s failed", u, d)
            row = None
        if row:
            rows.append(row)
        if progress:
            progress(i, len(todo))
    if rows:
        out = pd.concat([have, pd.DataFrame(rows, columns=COLUMNS)], ignore_index=True)
        out = out.drop_duplicates("day").sort_values("day")
        console_dir().mkdir(parents=True, exist_ok=True)
        out.to_csv(store_path(u), index=False)
    return {"underlying": u, "added": len(rows), "skipped": len(todo) - len(rows),
            "total": len(done_days) + len(rows)}


_CACHE: dict[str, tuple[float, list[str], list[float]]] = {}


def _series(underlying: str) -> tuple[list[str], list[float]]:
    p = store_path(underlying)
    if not p.exists():
        return [], []
    mtime = p.stat().st_mtime
    hit = _CACHE.get(underlying.upper())
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    df = load(underlying)
    days, ivs = df["day"].astype(str).tolist(), df["iv"].astype(float).tolist()
    _CACHE[underlying.upper()] = (mtime, days, ivs)
    return days, ivs


def iv_rank(underlying: str, day: date, iv_now: float | None) -> dict | None:
    """{"rank": percentile of iv_now in the trailing year, "ivr": (now−lo)/(hi−lo)·100,
    "n": rows used, "low", "high"} over rows STRICTLY before ``day``; None when the
    history is too short or there is no current IV."""
    if iv_now is None or iv_now <= 0:
        return None
    days, ivs = _series(underlying)
    i = bisect.bisect_left(days, day.isoformat())
    window = ivs[max(0, i - _WINDOW):i]
    if len(window) < _MIN_ROWS:
        return None
    lo, hi = min(window), max(window)
    below = sum(1 for v in window if v < iv_now)
    ivr = (iv_now - lo) / (hi - lo) * 100.0 if hi > lo else None
    return {"rank": round(100.0 * below / len(window), 1),
            "ivr": round(ivr, 1) if ivr is not None else None,
            "n": len(window), "low": lo, "high": hi}


def daily_iv(underlying: str, day: date) -> float | None:
    """The stored ~30-DTE ATM IV for ``day`` itself (the same rule the history uses)."""
    days, ivs = _series(underlying)
    i = bisect.bisect_left(days, day.isoformat())
    return ivs[i] if i < len(days) and days[i] == day.isoformat() else None
