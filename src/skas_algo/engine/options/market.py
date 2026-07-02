"""Lazy options market view — loads a contract's price series on first reference.

Unlike the equity ``MarketView`` (a fixed universe loaded up front), an options
strategy picks strikes dynamically, so contracts are loaded on demand: the first time
the engine prices a contract symbol (a fill, a mark, an exit) the series is fetched
via the loader and cached. The trading calendar comes from the underlying index
series (``calendar``). Satisfies the ``MarketLike`` protocol so the same SliceExecutor
drives it; ``chain`` is exposed for the strategy via ``AlgoContext.option_chain()``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from .chain import OptionChainView
from .instrument import is_option_symbol


class OptionMarketView:
    def __init__(self, loader, chain: OptionChainView, calendar: list, lot_overrides: dict | None = None,
                 equity_loader=None, day_range_provider=None):
        # loader(symbol, start, end) -> DataFrame with 'date' + 'close' for one contract.
        # equity_loader (optional, same shape): fallback for PLAIN symbols (e.g. an ETF
        # held inside a covered-call options run) — the options loader returns None for
        # anything that isn't a contract symbol. None → behaviour unchanged.
        # day_range_provider (optional): (underlying, date) -> (high, low) | None — the
        # underlying's daily bar range, wired only by basket runs so a touch-basis breach
        # check can see intraday extremes on daily bars. None → day_range returns None.
        self._loader = loader
        self._equity_loader = equity_loader
        self._day_range = day_range_provider
        self.chain = chain
        self.lot_overrides = lot_overrides
        self.unified_dates: list[pd.Timestamp] = [pd.Timestamp(d) for d in calendar]
        self._start = self.unified_dates[0].date() if self.unified_dates else date(2000, 1, 1)
        self._end = self.unified_dates[-1].date() if self.unified_dates else date.today()
        self._current: pd.Timestamp | None = None
        self._series: dict[str, dict[pd.Timestamp, float]] = {}   # symbol -> {ts: close}
        self._last_close: dict[str, float] = {}

    # --------------------------------------------------------------- cursor
    def set_date(self, ts: pd.Timestamp) -> None:
        self._current = ts
        for symbol, series in self._series.items():
            px = series.get(ts)
            if px is not None:
                self._last_close[symbol] = px

    @property
    def current_date(self) -> pd.Timestamp:
        assert self._current is not None, "cursor not positioned"
        return self._current

    # --------------------------------------------------------------- loading
    def _ensure(self, symbol: str) -> None:
        if symbol in self._series:
            return
        df = self._loader(symbol, self._start, self._end)
        if ((df is None or df.empty) and self._equity_loader is not None
                and not is_option_symbol(symbol)):
            df = self._equity_loader(symbol, self._start, self._end)
        series: dict[pd.Timestamp, float] = {}
        if df is not None and not df.empty:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            for ts, close in zip(df["date"], df["close"]):
                series[pd.Timestamp(ts)] = float(close)
        self._series[symbol] = series
        # Seed last_close with the most recent print up to the cursor.
        if self._current is not None and series:
            past = [t for t in series if t <= self._current]
            if past:
                self._last_close[symbol] = series[max(past)]

    # --------------------------------------------------------------- query
    def close(self, symbol: str) -> float:
        """Contract close on the current date, forward-filling the last known print."""
        self._ensure(symbol)
        series = self._series.get(symbol, {})
        if self._current in series:
            return series[self._current]
        if symbol in self._last_close:
            return self._last_close[symbol]
        raise KeyError(f"{symbol} has no price on/before {self._current}")

    def has_print(self, symbol: str) -> bool:
        self._ensure(symbol)
        return self._current in self._series.get(symbol, {})

    def present_symbols(self) -> list[str]:
        return [s for s, series in self._series.items() if self._current in series]

    def closes_today(self) -> dict[str, float]:
        out = {}
        for s, series in self._series.items():
            if self._current in series:
                out[s] = series[self._current]
        return out

    def mark_prices(self) -> dict[str, float]:
        return dict(self._last_close)

    def index_spot(self, underlying: str) -> float | None:
        """Underlying spot on the current bar (close basis, forward-filled) — parity with
        the live view's ``index_spot`` so spot-driven strategies (donchian breach flips)
        run in backtests too. Same value ``chain.spot`` would give for the bar date."""
        if self._current is None:
            return None
        return self.chain.spot(underlying, self._current.date())

    def day_range(self, underlying: str) -> tuple[float, float] | None:
        """(high, low) of the underlying's daily bar on the current date, when the run
        builder wired a provider (basket backtests). None = no provider / no bar."""
        if self._day_range is None or self._current is None:
            return None
        return self._day_range(underlying, self._current.date())

    # Options strategies don't use Donchian levels; present for protocol completeness.
    def rolling_high(self, symbol: str) -> float:  # pragma: no cover - unused
        raise NotImplementedError("rolling levels are not defined for option contracts")

    def rolling_low(self, symbol: str) -> float:  # pragma: no cover - unused
        raise NotImplementedError("rolling levels are not defined for option contracts")
