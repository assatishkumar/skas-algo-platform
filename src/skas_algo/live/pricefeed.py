"""WebSocket price feed — push-based marks for live runs, broker-agnostic surface.

Design principle: **prices PUSH into a shared cache; decisions stay loop-driven.** A single
KiteTicker WebSocket per account streams LTPs into a thread-safe last-tick cache; every run
on that account reads the cache instantly through ``FeedQuoteSource`` instead of each run
polling REST. Strategies are UNCHANGED — they still read marks through the ``QuoteSource``
contract, so backtest == paper == live parity holds (a raw per-tick callback INTO strategies
would fork live behavior away from backtest, which has no ticks — deliberately not done).

Reliability model: every cache read is staleness-checked. A dead/oscillating socket, an
auth failure after a token refresh, or a symbol the exchange isn't ticking simply reads as
"stale" → ``FeedQuoteSource`` transparently falls back to the batched REST quote. The feed
can only ever make marks *faster*, never wrong or missing.

Only Zerodha (KiteTicker) is implemented; the surface is broker-neutral so a Dhan feed can
slot in during the Dhan order phase. Everything is gated behind ``settings.ws_feed_enabled``.
"""

from __future__ import annotations

import logging
import threading
import time as _time
from collections.abc import Callable
from typing import Protocol

from skas_algo.config import get_settings

from .quotes import ZerodhaQuoteSource

logger = logging.getLogger("skas_algo.live")

# Kite allows up to 3000 instruments per connection; warn before we get close.
_SUB_WARN = 2500


class PriceFeed(Protocol):
    def ensure_subscribed(self, symbols: list[str]) -> None: ...
    def get(self, symbol: str) -> tuple[float, float] | None: ...  # (ltp, monotonic_ts)
    def stale(self, symbol: str, max_age_s: float) -> bool: ...
    def status(self) -> dict: ...
    def stop(self) -> None: ...


class KiteTickerFeed:
    """One KiteTicker WebSocket for a Zerodha account, feeding a last-tick cache.

    ``adapter`` is the account's ZerodhaAdapter — used to resolve our symbols to Kite
    instrument tokens (``instrument_tokens``, one REST call for unknowns) and for the
    api_key/access_token the socket authenticates with. ``ticker_factory`` is injectable
    so tests drive a fake ticker; the default builds a real ``KiteTicker`` lazily.
    """

    def __init__(self, adapter, *, ticker_factory: Callable[[str, str], object] | None = None):
        self.adapter = adapter
        self.api_key = getattr(getattr(adapter, "creds", None), "api_key", None)
        self.access_token = getattr(adapter, "access_token", None)
        self._ticker_factory = ticker_factory or _default_ticker_factory
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, float]] = {}   # symbol -> (ltp, monotonic ts)
        self._token_of: dict[str, int] = {}               # symbol -> instrument_token
        self._symbol_of: dict[int, str] = {}              # instrument_token -> symbol
        self._ticker = None
        self._connected = False
        self._degraded: str | None = None

    # ---------------------------------------------------------------- lifecycle
    def _ensure_ticker(self) -> bool:
        """Build + connect the ticker on first use. Returns False if it can't start
        (no creds / KiteTicker unavailable / connect error) → callers fall back to REST."""
        if self._ticker is not None:
            return True
        if not self.api_key or not self.access_token:
            self._degraded = "no api_key/access_token"
            return False
        try:
            ticker = self._ticker_factory(self.api_key, self.access_token)
            ticker.on_ticks = self._on_ticks
            ticker.on_connect = self._on_connect
            ticker.on_close = self._on_close
            ticker.on_error = self._on_error
            ticker.connect(threaded=True)
            self._ticker = ticker
            return True
        except Exception as exc:  # pragma: no cover - env/socket issues → REST fallback
            self._degraded = f"connect failed: {exc}"
            logger.warning("price feed connect failed (falling back to REST): %s", exc)
            return False

    def stop(self) -> None:
        t = self._ticker
        self._ticker = None
        self._connected = False
        if t is not None:
            try:
                t.close()
            except Exception:  # pragma: no cover
                pass

    # ---------------------------------------------------------------- subscribe
    def ensure_subscribed(self, symbols: list[str]) -> None:
        if not symbols or not self._ensure_ticker():
            return
        with self._lock:
            unknown = [s for s in symbols if s not in self._token_of]
        if unknown:
            try:
                tokens = self.adapter.instrument_tokens(unknown)
            except Exception as exc:  # pragma: no cover - REST resolve hiccup
                logger.debug("token resolution failed for %s: %s", unknown, exc)
                return
            with self._lock:
                for sym, tok in tokens.items():
                    self._token_of[sym] = tok
                    self._symbol_of[tok] = sym
                total = len(self._token_of)
            self._subscribe_tokens(list(tokens.values()))
            if total >= _SUB_WARN:
                logger.warning("price feed nearing the 3000-instrument cap: %d subscribed",
                               total)

    def _subscribe_tokens(self, tokens: list[int]) -> None:
        if not tokens or self._ticker is None:
            return
        try:
            self._ticker.subscribe(tokens)
            self._ticker.set_mode(self._ticker.MODE_LTP, tokens)
        except Exception as exc:  # pragma: no cover - resubscribed on next reconnect
            logger.debug("subscribe failed for %d tokens: %s", len(tokens), exc)

    # ---------------------------------------------------------------- reads
    def get(self, symbol: str) -> tuple[float, float] | None:
        with self._lock:
            return self._cache.get(symbol)

    def stale(self, symbol: str, max_age_s: float) -> bool:
        v = self.get(symbol)
        if v is None:
            return True
        return (_time.monotonic() - v[1]) > max_age_s

    def status(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "subscribed": len(self._token_of),
                "cached": len(self._cache),
                "degraded": self._degraded,
            }

    # ---------------------------------------------------------------- callbacks
    def _on_ticks(self, ws, ticks) -> None:  # noqa: ANN001 - kite signature
        now = _time.monotonic()
        with self._lock:
            for t in ticks:
                tok = t.get("instrument_token")
                sym = self._symbol_of.get(tok)
                if sym is not None and t.get("last_price") is not None:
                    self._cache[sym] = (float(t["last_price"]), now)

    def _on_connect(self, ws, response) -> None:  # noqa: ANN001
        self._connected = True
        self._degraded = None
        # Re-subscribe everything: a reconnect starts with an empty subscription set.
        with self._lock:
            tokens = list(self._token_of.values())
        self._subscribe_tokens(tokens)

    def _on_close(self, ws, code, reason) -> None:  # noqa: ANN001
        self._connected = False
        self._degraded = f"closed: {reason}"

    def _on_error(self, ws, code, reason) -> None:  # noqa: ANN001
        self._degraded = f"error: {reason}"


def _default_ticker_factory(api_key: str, access_token: str):
    from kiteconnect import KiteTicker

    return KiteTicker(api_key, access_token)


class FeedQuoteSource:
    """QuoteSource that serves fresh WS ticks from ``feed`` and falls back to REST.

    Exposes ``.adapter`` (the underlying real adapter) so every existing
    ``getattr(quote_source, "adapter", ...)`` call site — the live-order injection gate,
    reconciliation, margin, chain wiring — keeps working unchanged.
    """

    def __init__(self, feed: PriceFeed, rest: ZerodhaQuoteSource, stale_s: float):
        self.feed = feed
        self.rest = rest
        self.stale_s = float(stale_s)
        self.adapter = rest.adapter  # keep the injection gate + wiring working

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        self.feed.ensure_subscribed(symbols)
        out: dict[str, float] = {}
        missing: list[str] = []
        for s in symbols:
            v = self.feed.get(s)
            if v is not None and not self.feed.stale(s, self.stale_s):
                out[s] = v[0]
            else:
                missing.append(s)
        if missing:
            out.update(self.rest.get_quotes(missing))
        return out


# One feed per account, shared by every run on it (one WS connection, shared subscriptions).
_feeds: dict[int, KiteTickerFeed] = {}
_feeds_lock = threading.Lock()


def feed_for(account_id: int | None, adapter) -> KiteTickerFeed | None:
    """Return the account's feed, rebuilding it if the adapter's token changed (re-login).
    None if account_id is unknown (can't share a feed safely)."""
    if account_id is None:
        return None
    token = getattr(adapter, "access_token", None)
    api_key = getattr(getattr(adapter, "creds", None), "api_key", None)
    with _feeds_lock:
        cur = _feeds.get(account_id)
        if cur is not None and cur.access_token == token and cur.api_key == api_key:
            return cur
        if cur is not None:
            cur.stop()
        feed = KiteTickerFeed(adapter)
        _feeds[account_id] = feed
        return feed


def build_quote_source(account, adapter):
    """Factory used at every quote-source construction site. Returns a WS-backed
    ``FeedQuoteSource`` when the feed is enabled AND the account is a zerodha account;
    otherwise the legacy REST ``ZerodhaQuoteSource``. Behavior-identical to consumers."""
    rest = ZerodhaQuoteSource(adapter)
    settings = get_settings()
    broker = getattr(account, "broker", None)
    broker = getattr(broker, "value", broker)  # enum or str
    if not settings.ws_feed_enabled or str(broker) != "zerodha":
        return rest
    feed = feed_for(getattr(account, "id", None), adapter)
    if feed is None:
        return rest
    return FeedQuoteSource(feed, rest, settings.ws_feed_stale_s)
