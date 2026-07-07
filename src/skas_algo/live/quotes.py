"""Live quote sources for the real-time engine.

A QuoteSource returns the current price per symbol. Two implementations:
- CacheQuoteSource: latest cached close from skas-data — works offline (markets
  closed / no broker), so the whole live pipeline can be exercised without a session.
- ZerodhaQuoteSource: real-time LTP via a logged-in ZerodhaAdapter (used for actual
  forward-testing during market hours).

Warmup history (the rolling-Donchian seed) always comes from the skas-data cache.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from skas_algo.engine.market import PriceLoader

IST = ZoneInfo("Asia/Kolkata")


@runtime_checkable
class QuoteSource(Protocol):
    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        """Return {symbol: current price} for the symbols that have a price."""
        ...


class CacheQuoteSource:
    """Latest cached close per symbol (offline-friendly)."""

    def __init__(self, loader: PriceLoader):
        self.loader = loader

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        end = date.today()
        start = end - timedelta(days=30)
        for s in symbols:
            df = self.loader(s, start, end)
            if df is not None and not df.empty:
                out[s] = float(df.iloc[-1]["close"])
        return out


# quote_source values that mean "a real broker adapter feeds live LTPs" (vs "cache").
# The value doubles as the required account.broker, so a dhan source can't ride a
# zerodha account and vice-versa.
BROKER_QUOTE_SOURCES = ("zerodha", "dhan")


def is_broker_source(quote_source: str | None) -> bool:
    return (quote_source or "") in BROKER_QUOTE_SOURCES


class ZerodhaQuoteSource:
    """Real-time LTP via a logged-in broker adapter (name is historical — it wraps ANY
    adapter exposing ``get_quote``; Dhan uses the same class via ``BrokerQuoteSource``)."""

    def __init__(self, adapter):
        self.adapter = adapter

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        return self.adapter.get_quote(symbols)


BrokerQuoteSource = ZerodhaQuoteSource  # the honest name for new call sites


def warmup_history(
    loader: PriceLoader, symbols: list[str], lookback: int, as_of: date | None = None
) -> dict[str, list[float]]:
    """Closes up to (and including) the day before ``as_of`` for each symbol.

    Pulls a generous window (~4x lookback calendar-adjusted) so the last `lookback`
    *trading* closes are available to seed the rolling levels.
    """
    as_of = as_of or date.today()
    start = as_of - timedelta(days=max(lookback * 4, 40))
    out: dict[str, list[float]] = {}
    for s in symbols:
        df = loader(s, start, as_of - timedelta(days=1))
        out[s] = [float(c) for c in df["close"].tolist()] if df is not None and not df.empty else []
    return out


def is_market_open(now: datetime | None = None) -> bool:
    """NSE regular session: Mon-Fri, 09:15-15:30 IST, excluding trading holidays.

    Holidays make this return False so the loop treats them like a weekend — marks may
    re-price off-hours (read-only) but NO decisions/orders fire. See live/holidays.py.
    """
    from .holidays import is_nse_holiday

    now = now or datetime.now(IST)
    if now.weekday() >= 5 or is_nse_holiday(now.date()):
        return False
    return time(9, 15) <= now.timetz().replace(tzinfo=None) <= time(15, 30)
