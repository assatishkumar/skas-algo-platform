"""Prices for the sleeves no Indian broker quotes: US equities and crypto.

Zerodha and Dhan cover NSE/BSE, and AMFI covers mutual funds — which leaves US stocks and
Bitcoin with no feed at all, so they sat frozen at whatever was typed. This closes that gap
using Yahoo's public chart endpoint: no key, no auth, and it serves equities, crypto and FX
through one shape.

**Everything on the /portfolio screen is INR**, so a USD quote is converted here rather than
at the call site. The conversion is done at BOTH ends of the day change:

    today's INR   = price      x rate today
    prior INR     = prevClose  x rate at ITS previous close

Using one rate for both would report only the share's move and quietly drop the currency's —
but an INR investor's position really did change by both, and on a day the rupee moves 1% that
is the larger half. The FX quote carries its own previousClose, so the honest figure is
available for free.

Batching is not possible: Yahoo's multi-symbol quote endpoint now 401s, so this is one request
per symbol, throttled. That is fine for a once-a-day portfolio sync and nowhere near a trading
path — nothing here is ever used for an order.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FX_SYMBOL = "USDINR=X"
_TIMEOUT = 20
_THROTTLE = 0.25  # seconds between requests — courteous, and irrelevant at this volume
# Yahoo returns 401 to an unadorned client.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; skas-algo/1.0)"}


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    prev_close: float | None
    currency: str
    # ``(ex_date, numerator, denominator)`` for every split Yahoo reports in the fetch
    # window — 3:1 is (…, 3.0, 1.0). The sync compares them with the ledger: the quote is
    # split-adjusted the morning it happens, and a ledger still holding PRE-split units
    # then reads a two-thirds loss (SOXX 3:1, 2026-11-02: +38% shown as −54%).
    splits: tuple[tuple[date, float, float], ...] = ()

    @property
    def day_move(self) -> float | None:
        return None if self.prev_close is None else self.price - self.prev_close


class QuoteUnavailable(RuntimeError):
    """The feed answered, but not with a price for this symbol."""


def _previous_close(meta: dict, closes: list, price: float) -> float | None:
    """Yesterday's close, taken from the CANDLE SERIES — never from ``chartPreviousClose``.

    That field is the close before the requested WINDOW opens, so on a 5-day range it is about
    a week old. Using it reported MSFT at +5.65% on a day it actually fell 1.22% (513.53 ->
    507.29): not merely the wrong magnitude, the wrong SIGN. The series is unambiguous — the
    last candle is the current session (or the most recent close when the market is shut), so
    the prior session is the one before it.
    """
    series = [c for c in closes if c is not None]
    if len(series) >= 2:
        # When the final candle already carries the current price, step back one.
        same = abs(series[-1] - price) <= max(0.01, abs(price) * 1e-6)
        return float(series[-2] if same else series[-1])
    if len(series) == 1 and abs(series[0] - price) > max(0.01, abs(price) * 1e-6):
        return float(series[0])
    # Only as a last resort, and only the field that means the PRIOR SESSION.
    prev = meta.get("previousClose")
    return float(prev) if prev is not None else None


def _local_date(ts: int | float, meta: dict) -> date:
    """A candle/event timestamp as the EXCHANGE's calendar date. Yahoo stamps a USDINR=X
    daily candle at 23:00 UTC the evening BEFORE (London midnight in summer), so a plain UTC
    date reads one day early; ``gmtoffset`` is the exchange's offset in seconds."""
    off = int(meta.get("gmtoffset") or 0)
    return datetime.fromtimestamp(int(ts) + off, tz=UTC).date()


def _splits(node: dict, meta: dict) -> tuple[tuple[date, float, float], ...]:
    out = []
    for ev in ((node.get("events") or {}).get("splits") or {}).values():
        try:
            num, den = float(ev["numerator"]), float(ev["denominator"])
            if num > 0 and den > 0:
                out.append((_local_date(ev["date"], meta), num, den))
        except (KeyError, TypeError, ValueError):  # a malformed event is skipped, not fatal
            continue
    return tuple(sorted(out))


def _chart(symbol: str, params: dict, session: requests.Session | None) -> dict:
    http = session or requests
    resp = http.get(
        CHART_URL.format(symbol=symbol), params=params, headers=_HEADERS, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    result = (body.get("chart") or {}).get("result") or []
    if not result:
        err = ((body.get("chart") or {}).get("error") or {}).get("description", "no result")
        raise QuoteUnavailable(f"{symbol}: {err}")
    return result[0]


def _fetch(symbol: str, session: requests.Session | None = None) -> Quote:
    # A month, not five days, so a split stays visible through a week the box was off. The
    # price and the previous close read the series' TAIL, which the range does not change.
    node = _chart(symbol, {"range": "1mo", "interval": "1d", "events": "split"}, session)
    meta = node.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        raise QuoteUnavailable(f"{symbol}: no price in the response")
    price = float(price)
    quote_block = ((node.get("indicators") or {}).get("quote") or [{}])[0]
    return Quote(
        symbol=symbol,
        price=price,
        prev_close=_previous_close(meta, quote_block.get("close") or [], price),
        currency=str(meta.get("currency") or "").upper(),
        splits=_splits(node, meta),
    )


def usd_inr(session: requests.Session | None = None) -> Quote:
    """The USD/INR rate, with its own previous close so a day change can be computed in INR."""
    return _fetch(FX_SYMBOL, session)


def usd_inr_on(day: date, session: requests.Session | None = None) -> tuple[float, date]:
    """``(rate, rate_date)`` — the USD/INR close ON ``day``, or the last one before it.

    For entering a US trade in its own currency: the ledger is INR, and the honest rupee
    cost of a dollar purchase is at the rate of the day it was paid — today's rate would
    move the cost basis by every rupee the currency has moved since. A weekend or US holiday
    falls back to the prior close, and ``rate_date`` says which close was used."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(days=10)
    end = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=2)
    node = _chart(FX_SYMBOL, {
        "period1": int(start.timestamp()), "period2": int(end.timestamp()), "interval": "1d",
    }, session)
    meta = node.get("meta") or {}
    closes = (((node.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    best: tuple[date, float] | None = None
    for ts, c in zip(node.get("timestamp") or [], closes, strict=False):
        d = _local_date(ts, meta)
        if c is not None and d <= day and (best is None or d >= best[0]):
            best = (d, float(c))
    if best is None:
        raise QuoteUnavailable(f"no USD/INR close on or before {day.isoformat()}")
    return best[1], best[0]


def quotes(
    symbols: list[str], *, session: requests.Session | None = None
) -> tuple[dict[str, Quote], dict[str, str]]:
    """``({symbol: Quote}, {symbol: error})`` — one request per symbol.

    A symbol that fails lands in the error map and is simply absent from the quotes; nothing
    is substituted, because a stale price presented as current is worse than a gap."""
    out: dict[str, Quote] = {}
    errors: dict[str, str] = {}
    for i, symbol in enumerate(dict.fromkeys(s for s in symbols if s)):
        if i:
            time.sleep(_THROTTLE)
        try:
            out[symbol] = _fetch(symbol, session)
        except Exception as exc:  # network, HTTP, or a symbol the feed doesn't carry
            logger.warning("global quote failed for %s", symbol, exc_info=True)
            errors[symbol] = str(exc)
    return out, errors


def in_inr(quote: Quote, fx: Quote | None) -> tuple[float, float | None]:
    """``(price_inr, prev_close_inr)``.

    An INR-quoted symbol (Yahoo carries BTC-INR directly) needs no conversion. A USD one is
    converted at today's rate, and its previous close at the rate's OWN previous close — so
    the day change carries the currency move as well as the instrument's."""
    if quote.currency == "INR" or not quote.currency:
        return quote.price, quote.prev_close
    if fx is None:
        raise QuoteUnavailable(
            f"{quote.symbol} is quoted in {quote.currency} and the USD/INR rate is unavailable"
        )
    price = quote.price * fx.price
    prev = (
        quote.prev_close * (fx.prev_close or fx.price)
        if quote.prev_close is not None
        else None
    )
    return price, prev
