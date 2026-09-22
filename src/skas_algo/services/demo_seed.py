"""`skas-algo demo-seed` — synthetic NIFTY option days for a broker-free demo (2026-09-22).

A visitor (or a screen recording) needs the console, the replay backtest and a paper
deployment to have something to chew on without a Zerodha login, the real 1-min capture
or the skas-data cache. This writes a handful of plausible days into the option store:
a convex premium surface (time value decaying geometrically with distance, intrinsic
carried), spot drifting on a deterministic random walk minute by minute, both rights on
every 100-strike from spot ±2,000, one print a minute — enough for the console's ladder,
payoff, IV solve and the replay harness to behave as they do on a captured day.

It REFUSES to write into the real store: `SKAS_OPTION_INTRADAY_DIR` must point somewhere
else (scripts/demo.sh sets it), because a synthetic day beside the real capture would be
indistinguishable from data and would poison every backtest that touched it.
"""

from __future__ import annotations

import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from skas_algo.data import option_intraday_store as store

_REAL_STORE = Path.home() / ".skas_data" / "option_intraday" / "1min"


def _last_tuesday_on_or_after(d: date) -> date:
    """NIFTY monthlies expire on the last Tuesday of the month (2026 calendar)."""
    y, m = d.year, d.month
    for _ in range(3):
        last = date(y, m, 1) + timedelta(days=32)
        last = last.replace(day=1) - timedelta(days=1)
        while last.weekday() != 1:
            last -= timedelta(days=1)
        if last >= d:
            return last
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return d


def synthetic_day(day: date, spot_open: float, expiry: date, *,
                  seed: int) -> tuple[pd.DataFrame, float]:
    """One store day. Returns the frame and the day's closing spot (the next day's open)."""
    rng = random.Random(seed)
    dte = max(1, (expiry - day).days)
    atm_tv = 18.0 * (dte ** 0.5)               # ATM time value ≈ 18 × √DTE (≈ 14% vol)
    minutes = []
    t = datetime(day.year, day.month, day.day, 9, 15)
    while t.time() < datetime(2000, 1, 1, 15, 30).time():
        minutes.append(t)
        t += timedelta(minutes=1)
    spot = spot_open
    lo = int(round(spot_open / 100.0) * 100) - 2000
    strikes = list(range(lo, lo + 4100, 100))
    rows = []
    n = len(minutes)
    for i, m in enumerate(minutes):
        spot += rng.gauss(0.0, 4.5)             # ~₹4.5 a minute ≈ 90 pts a day, one sigma
        decay = 1.0 - 0.25 * (i / n)            # a quarter of the day's time value melts
        for k in strikes:
            dist = abs(k - spot) / 100.0
            tv = atm_tv * decay * (0.84 ** dist)
            ce = max(0.05, round(max(spot - k, 0.0) + tv, 2))
            pe = max(0.05, round(max(k - spot, 0.0) + tv, 2))
            for right, px in (("CE", ce), ("PE", pe)):
                rows.append({
                    "symbol": f"NIFTY|{expiry.isoformat()}|{k}|{right}",
                    "start": m, "open": px, "high": px, "low": px, "close": px,
                    "volume": float(rng.randint(50, 900)), "oi": float(5000 + 40 * (20 - dist)),
                })
    return pd.DataFrame(rows, columns=store.COLUMNS), round(spot, 2)


def seed(*, days: int = 5, end: str | None = None) -> list[str]:
    """Write ``days`` synthetic trading days ending on ``end`` (default: last Friday)."""
    target = Path(os.environ.get("SKAS_OPTION_INTRADAY_DIR", "")).expanduser()
    if (not os.environ.get("SKAS_OPTION_INTRADAY_DIR")
            or target.resolve() == _REAL_STORE.resolve()):
        raise SystemExit(
            "refusing to seed the REAL option store — set SKAS_OPTION_INTRADAY_DIR to a "
            "throwaway directory first (scripts/demo.sh does)"
        )
    if store.OPTION_INTRADAY_DIR.resolve() != target.resolve():  # pragma: no cover
        raise SystemExit("the store module was imported before SKAS_OPTION_INTRADAY_DIR was set")
    last = date.fromisoformat(end) if end else date.today() - timedelta(days=1)
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    todo: list[date] = []
    d = last
    while len(todo) < max(1, int(days)):
        if d.weekday() < 5:
            todo.append(d)
        d -= timedelta(days=1)
    todo.reverse()
    expiry = _last_tuesday_on_or_after(todo[-1] + timedelta(days=7))
    spot = 24000.0
    out = []
    for i, day in enumerate(todo):
        df, spot = synthetic_day(day, spot, expiry, seed=1000 + i)
        store.write_day(day, df)
        out.append(f"{day.isoformat()}  {len(df):>6} rows  close ≈ {spot:,.0f}  expiry {expiry}")
    out.append(f"store: {target}")
    return out
