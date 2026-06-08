"""Market data access for the engine.

`MarketView` holds per-symbol price history and a movable cursor ("today"). It
exposes today's close and rolling Donchian-style levels (prior-N window, excluding
today) computed once via ``rolling(n).shift(1)`` — identical to SST's
``df.iloc[loc - n : loc]`` window.

`HistoricalReplayFeed` (BACKTEST) drives the cursor across the unified trading
calendar. In PAPER/LIVE the same MarketView is fed by a live feed (Phase 4) — the
strategy code that reads it does not change.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

# A loader returns an OHLC DataFrame (with a 'date' column + 'close') for a symbol.
PriceLoader = Callable[[str, date, date], "pd.DataFrame | None"]


@runtime_checkable
class MarketLike(Protocol):
    """Market interface a strategy/engine reads, satisfied by both the backtest
    MarketView and the LiveMarketView — so the same strategy code runs in any mode."""

    def present_symbols(self) -> list[str]: ...
    def close(self, symbol: str) -> float: ...
    def rolling_high(self, symbol: str) -> float: ...
    def rolling_low(self, symbol: str) -> float: ...
    def closes_today(self) -> dict[str, float]: ...
    def mark_prices(self) -> dict[str, float]: ...


class MarketView:
    """Per-symbol price series with a movable 'current date' cursor."""

    def __init__(self, lookback: int):
        self.lookback = lookback
        self.unified_dates: list[pd.Timestamp] = []
        self._current: pd.Timestamp | None = None
        # symbol -> {date: (close, high_Nd, low_Nd)}  (levels may be NaN early)
        self._series: dict[str, dict[pd.Timestamp, tuple[float, float, float]]] = {}
        self._universe_order: list[str] = []
        # Most recent close seen per symbol, forward-filled as the cursor advances.
        self._last_close: dict[str, float] = {}

    # ------------------------------------------------------------- building
    def add_symbol(self, symbol: str, df: pd.DataFrame) -> None:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
        close = df["close"]
        high = close.rolling(self.lookback).max().shift(1)
        low = close.rolling(self.lookback).min().shift(1)
        self._series[symbol] = {
            ts: (close.loc[ts], high.loc[ts], low.loc[ts]) for ts in close.index
        }
        self._universe_order.append(symbol)

    def finalize(self) -> None:
        all_dates: set[pd.Timestamp] = set()
        for series in self._series.values():
            all_dates.update(series.keys())
        self.unified_dates = sorted(all_dates)

    # -------------------------------------------------------------- cursor
    def set_date(self, ts: pd.Timestamp) -> None:
        self._current = ts
        # Forward-fill the last known close for any symbol printing today.
        for symbol, series in self._series.items():
            row = series.get(ts)
            if row is not None:
                self._last_close[symbol] = row[0]

    @property
    def current_date(self) -> pd.Timestamp:
        assert self._current is not None, "cursor not positioned"
        return self._current

    # --------------------------------------------------------------- query
    def _row(self, symbol: str) -> tuple[float, float, float] | None:
        row = self._series.get(symbol, {}).get(self._current)
        if row is None:
            return None
        _close, high, low = row
        if pd.isna(high) or pd.isna(low):
            return None  # insufficient history (loc < lookback)
        return row

    def present_symbols(self) -> list[str]:
        """Symbols printing today with valid rolling levels, in universe order."""
        return [s for s in self._universe_order if self._row(s) is not None]

    def close(self, symbol: str) -> float:
        row = self._row(symbol)
        if row is None:
            raise KeyError(f"{symbol} not present on {self._current}")
        return row[0]

    def rolling_high(self, symbol: str) -> float:
        return self._row(symbol)[1]  # type: ignore[index]

    def rolling_low(self, symbol: str) -> float:
        return self._row(symbol)[2]  # type: ignore[index]

    def closes_today(self) -> dict[str, float]:
        """Prices actually printed today (for stop evaluation / fills)."""
        out: dict[str, float] = {}
        for s in self._universe_order:
            row = self._row(s)
            if row is not None:
                out[s] = row[0]
        return out

    def mark_prices(self) -> dict[str, float]:
        """Last known close per symbol (forward-filled) for marking-to-market.

        Unlike closes_today(), this never drops a held position to zero on a day it
        doesn't print (e.g. a Muhurat/special session) — it carries the prior close.
        """
        return dict(self._last_close)


class HistoricalReplayFeed:
    """Loads history for a universe and replays the unified calendar (BACKTEST)."""

    def __init__(self, loader: PriceLoader, lookback: int):
        self.loader = loader
        self.lookback = lookback

    def build(
        self, universe: list[str], start_date: date, end_date: date, verbose: bool = False
    ) -> MarketView:
        view = MarketView(self.lookback)
        for symbol in universe:
            df = self.loader(symbol, start_date, end_date)
            if df is not None and not df.empty:
                view.add_symbol(symbol, df)
            elif verbose:
                print(f"Warning: No data for {symbol}")
        view.finalize()
        return view

    @staticmethod
    def dates(view: MarketView) -> Iterator[pd.Timestamp]:
        yield from view.unified_dates
