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


SESSION_OPEN = time(9, 15)
_SESSION_CLOSE_FALLBACK = {"EQUITY": time(15, 30), "DERIV": time(15, 40)}


def session_close(segment: str = "EQUITY") -> time:
    """Last minute of continuous trading for ``segment`` ("EQUITY" | "DERIV").

    Two numbers since SEBI's Closing Auction Session (2026-08-03): index F&O runs to 15:40,
    equity cash still stops at 15:30. Read from settings so the owner can correct a future
    exchange change without a deploy; a malformed override falls back to the literal default
    rather than widening the window — this gates REAL orders.
    """
    from skas_algo.config.settings import get_settings

    key = "DERIV" if str(segment).upper() == "DERIV" else "EQUITY"
    raw = (get_settings().session_close_deriv if key == "DERIV"
           else get_settings().session_close_equity)
    try:
        hh, mm = str(raw).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return _SESSION_CLOSE_FALLBACK[key]


# Cash CONTINUOUS trading ends here for F&O-LISTED stocks since SEBI's Closing Auction
# Session (2026-08-03) — the auction then runs to ~15:35 and `session_close("EQUITY")`
# (15:30) is the segment boundary, NOT the last minute you can actually trade one of these
# names. Non-F&O names keep trading continuously to the segment close, which is why this is
# a WARNING input and never a hard rail: a watchlist of only non-F&O stocks is fine at 15:20.
FNO_CASH_CONTINUOUS_CLOSE = time(15, 15)


def in_closing_auction(t: time) -> bool:
    """Is ``t`` past continuous trading for an F&O-LISTED cash stock?

    An equity strategy deciding after this lands in the auction: a limit order rests
    unfilled, the escalation cannot help (there is no continuous book to cross), and the
    order path then cancels and HALTS the run — every single day. The platform's default
    decision time was 15:20 and no equity strategy was live-capable until value_investing,
    so this had never bitten.
    """
    return t >= FNO_CASH_CONTINUOUS_CLOSE


def auction_warning(decision_time: str, *, segment: str = "EQUITY") -> str | None:
    """A human warning when a daily EQUITY decision falls inside the auction, else None."""
    if str(segment).upper() == "DERIV":
        return None
    try:
        hh, mm = str(decision_time).split(":")
        t = time(int(hh), int(mm))
    except Exception:
        return None
    if not in_closing_auction(t):
        return None
    return (
        f"decision time {decision_time} is past {FNO_CASH_CONTINUOUS_CLOSE.strftime('%H:%M')}, "
        "when continuous cash trading ends for F&O-LISTED stocks (SEBI's closing auction runs "
        "to ~15:35). Orders in F&O names would rest unfilled and the run would halt daily. "
        "Non-F&O-only watchlists are unaffected."
    )


def is_market_open(now: datetime | None = None, *, segment: str = "EQUITY") -> bool:
    """NSE regular session: Mon-Fri, 09:15 → the segment's close, excluding holidays.

    ``segment`` defaults to EQUITY (15:30) — i.e. the pre-CAS behaviour — so any caller that
    doesn't opt in is unchanged; DERIV callers pass segment="DERIV" for the 15:40 close.

    Holidays make this return False so the loop treats them like a weekend — marks may
    re-price off-hours (read-only) but NO decisions/orders fire. See live/holidays.py.
    """
    from .holidays import is_nse_holiday

    now = now or datetime.now(IST)
    if now.weekday() >= 5 or is_nse_holiday(now.date()):
        return False
    return SESSION_OPEN <= now.timetz().replace(tzinfo=None) <= session_close(segment)
