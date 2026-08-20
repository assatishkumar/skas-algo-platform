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
    def rolling_mean(self, symbol: str) -> float: ...
    def closes_today(self) -> dict[str, float]: ...
    def mark_prices(self) -> dict[str, float]: ...

    # Optional capabilities — read through AlgoContext with getattr, so a view that does
    # not implement them degrades to None instead of raising (the options views implement
    # none of them, and OptionMarketView does not even have rolling_mean):
    #   supertrend_dir(symbol) -> float | None
    #   indicator(symbol, name) -> float | None
    #   prev_close(symbol) -> float | None


class MarketView:
    """Per-symbol price series with a movable 'current date' cursor."""

    def __init__(
        self, lookback: int, supertrend: dict | None = None, indicators: dict | None = None
    ):
        self.lookback = lookback
        self.unified_dates: list[pd.Timestamp] = []
        self._current: pd.Timestamp | None = None
        # symbol -> {date: (close, high_Nd, low_Nd, mean_Nd)}  (levels may be NaN early)
        self._series: dict[str, dict[pd.Timestamp, tuple[float, float, float, float]]] = {}
        self._universe_order: list[str] = []
        # Most recent close seen per symbol, forward-filled as the cursor advances.
        self._last_close: dict[str, float] = {}
        # symbol -> {date: the symbol's PREVIOUS PRINTED close}. Precomputed with shift(1)
        # because _series is an unordered dict — a "most recent date before the cursor" scan
        # would be O(days) per symbol per slice. Written for every run, read only by
        # prev_close(), so every existing path stays byte-identical.
        self._prev_close: dict[str, dict[pd.Timestamp, float]] = {}
        # Optional SuperTrend precompute: {"period","multiplier","timeframe"} → per-symbol
        # daily-mapped direction (+1/−1). Only computed when requested (equity supertrend).
        self._st_cfg = supertrend
        self._supertrend: dict[str, dict[pd.Timestamp, float]] = {}
        # Generic per-date indicator precompute (the OPEN-price plumbing, 2026-07-28):
        # {"ema": {"span": 21}, "rsi": {"period": 10}, "gap_pct": {}} → per-symbol
        # {date: {name: value}}. add_symbol has the RAW OHLC frame (incl. ``open``, which
        # the ctx otherwise never sees) — same precompute-then-scalar shape as SuperTrend.
        # None (the default) computes nothing → every existing path stays byte-identical.
        self._ind_cfg = indicators
        self._indicators: dict[str, dict[pd.Timestamp, dict[str, float]]] = {}

    # ------------------------------------------------------------- building
    def add_symbol(self, symbol: str, df: pd.DataFrame) -> None:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
        close = df["close"]
        high = close.rolling(self.lookback).max().shift(1)
        low = close.rolling(self.lookback).min().shift(1)
        mean = close.rolling(self.lookback).mean().shift(1)  # the N-day moving average (DMA)
        self._series[symbol] = {
            ts: (close.loc[ts], high.loc[ts], low.loc[ts], mean.loc[ts]) for ts in close.index
        }
        self._prev_close[symbol] = {
            ts: float(v) for ts, v in close.shift(1).items() if pd.notna(v)
        }
        if self._st_cfg is not None:
            from skas_algo.engine.indicators.supertrend import supertrend_direction

            sd = supertrend_direction(
                df.reset_index(),  # supertrend_direction reads OHLC (has high/low here)
                period=int(self._st_cfg.get("period", 10)),
                multiplier=float(self._st_cfg.get("multiplier", 3.0)),
                timeframe=str(self._st_cfg.get("timeframe", "daily")),
            )
            self._supertrend[symbol] = {ts: float(v) for ts, v in sd.items() if pd.notna(v)}
        if self._ind_cfg:
            self._indicators[symbol] = self._compute_indicators(df)
        self._universe_order.append(symbol)

    def _compute_indicators(self, df: pd.DataFrame) -> dict[pd.Timestamp, dict[str, float]]:
        """Per-date named indicator values from the RAW (date-indexed) OHLC frame.
        Supported: ``ema`` ({"span"}), ``rsi`` ({"period"}), ``gap_pct`` (today's open vs the
        previous close, as a FRACTION — the only reader of ``open`` in the equity engine).
        NaNs (warmup / missing open) are simply absent → ctx.indicator() returns None and
        strategies fail closed."""
        from skas_algo.engine.indicators import ema, rsi

        close = df["close"]
        series: dict[str, pd.Series] = {}
        if "ema" in self._ind_cfg:
            series["ema"] = ema(close, int(self._ind_cfg["ema"].get("span", 21)))
        if "rsi" in self._ind_cfg:
            cfg = self._ind_cfg["rsi"]
            # source "ema": RSI of the EMA(source_span) series (the TradingView "RSI 10
            # EMA:EMA" setup — gap_reversal's true spec). The smoothed source SATURATES
            # near 0/100 through trends (every EMA delta shares a sign), which is what
            # makes the <10 band fire routinely; raw-close RSI there is rare.
            src = close
            if str(cfg.get("source", "close")) == "ema":
                src = ema(close, int(cfg.get("source_span", 21)))
            series["rsi"] = rsi(src, int(cfg.get("period", 10)))
        if "gap_pct" in self._ind_cfg and "open" in df.columns:
            series["gap_pct"] = df["open"] / close.shift(1) - 1.0
        # Named SuperTrend series ({"kind": "supertrend", period, multiplier, timeframe}) —
        # any number of them, so a strategy can run TWINS (fast entry / slow exit) or more.
        # Reuses the same resample+ffill engine as the single-ST precompute: a weekly flip
        # becomes visible on the first trading day after the bar closes, no lookahead.
        for name, cfg in self._ind_cfg.items():
            if isinstance(cfg, dict) and cfg.get("kind") == "supertrend":
                from skas_algo.engine.indicators.supertrend import supertrend_direction

                series[name] = supertrend_direction(
                    df.reset_index(),
                    period=int(cfg.get("period", 10)),
                    multiplier=float(cfg.get("multiplier", 3.0)),
                    timeframe=str(cfg.get("timeframe", "daily")),
                )
        out: dict[pd.Timestamp, dict[str, float]] = {}
        for name, s in series.items():
            for ts, v in s.items():
                if pd.notna(v):
                    out.setdefault(ts, {})[name] = float(v)
        return out

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
    def _row(self, symbol: str) -> tuple[float, float, float, float] | None:
        row = self._series.get(symbol, {}).get(self._current)
        if row is None:
            return None
        _close, high, low, _mean = row
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

    def rolling_mean(self, symbol: str) -> float:
        """The prior-N moving average (DMA), excluding today — same window as the levels."""
        return self._row(symbol)[3]  # type: ignore[index]

    def supertrend_dir(self, symbol: str) -> float | None:
        """SuperTrend direction (+1 green / −1 red) at the cursor date, or None if not computed
        / not yet warmed up. Present only when the view was built with supertrend params."""
        return self._supertrend.get(symbol, {}).get(self._current)

    def indicator(self, symbol: str, name: str) -> float | None:
        """Named precomputed indicator value at the cursor date (ema / rsi / gap_pct), or
        None when the view wasn't built with indicators / the value isn't warmed up yet."""
        return self._indicators.get(symbol, {}).get(self._current, {}).get(name)

    def prev_close(self, symbol: str) -> float | None:
        """The symbol's PREVIOUS PRINTED close at the cursor date — the base for a day's
        change % — or None on its first bar. Deliberately NOT behind _row()'s rolling-warmup
        gate: that gate is about the Donchian window, while LiveMarketView.prev_close is
        available from the second close onward. Gating one side and not the other would make
        the two modes disagree, and present_symbols() applies the gate upstream anyway."""
        return self._prev_close.get(symbol, {}).get(self._current)

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

    def __init__(
        self,
        loader: PriceLoader,
        lookback: int,
        supertrend: dict | None = None,
        indicators: dict | None = None,
    ):
        self.loader = loader
        self.lookback = lookback
        self.supertrend = supertrend
        self.indicators = indicators

    def build(
        self, universe: list[str], start_date: date, end_date: date, verbose: bool = False
    ) -> MarketView:
        view = MarketView(self.lookback, supertrend=self.supertrend, indicators=self.indicators)
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
