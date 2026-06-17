"""Index-benchmark series for the equity-curve overlay.

Turns a cached index price series (e.g. ``NIFTY 50``) into a buy-and-hold curve
of the run's initial capital, aligned to the run's equity-curve dates.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from skas_algo.engine.market import PriceLoader

# Index symbols that exist in the skas-data cache (load exactly like stocks).
BENCHMARK_INDICES: list[str] = ["NIFTY 50", "NIFTY 100", "NIFTY 200", "NIFTY 500"]


def benchmark_series(
    loader: PriceLoader, index: str, equity_dates: list[str], initial_capital: float
) -> list[dict]:
    """Buy-and-hold of ``initial_capital`` in ``index``, aligned to ``equity_dates``.

    ``equity_dates`` are ``YYYY-MM-DD`` strings (the run's equity curve). Returns
    ``[{date, value}]`` with value normalized so it starts at ``initial_capital`` on
    the first date. Raises ``ValueError`` if the index has no usable cached data.
    """
    if not equity_dates:
        return []
    edates = pd.to_datetime(equity_dates)
    start: date = edates.min().date()
    end: date = edates.max().date()

    df = loader(index, start, end)
    if df is None or len(df) == 0:
        raise ValueError(f"no cached data for index {index!r}")
    df = df[["date", "close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    # As-of join: each equity date takes the latest index close on/before it.
    merged = pd.merge_asof(
        pd.DataFrame({"date": edates}).sort_values("date"), df, on="date", direction="backward"
    )
    closes = merged["close"].dropna()
    if closes.empty:
        raise ValueError(f"index {index!r} has no data covering the run's dates")
    base = float(closes.iloc[0])

    out: list[dict] = []
    for d, c in zip(merged["date"], merged["close"], strict=True):
        if pd.isna(c):
            continue
        ts: datetime = d.to_pydatetime()
        out.append({"date": ts.strftime("%Y-%m-%d"), "value": float(initial_capital * c / base)})
    return out
