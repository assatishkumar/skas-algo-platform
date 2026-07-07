"""WebSocket PriceFeed tests — fake ticker/adapter, no real socket or broker.

Covers: token resolution + subscribe, tick → cache, staleness, reconnect re-subscribe,
FeedQuoteSource fresh-then-REST fallback, feed-dead degrade, the enable/broker gating,
and the near-cap warning.
"""

from __future__ import annotations

import time as _time
from types import SimpleNamespace

from skas_algo.live import pricefeed
from skas_algo.live.pricefeed import FeedQuoteSource, KiteTickerFeed
from skas_algo.live.quotes import ZerodhaQuoteSource

NIFTY_CE = "NIFTY|2026-07-09|24500|CE"


class FakeTicker:
    MODE_LTP = "ltp"

    def __init__(self, api_key, access_token):
        self.api_key = api_key
        self.access_token = access_token
        self.subscribed: list[int] = []
        self.modes: list = []
        self.connected = False
        self.on_ticks = self.on_connect = self.on_close = self.on_error = None

    def connect(self, threaded=False):
        self.connected = True
        if self.on_connect:
            self.on_connect(self, {"ok": True})

    def subscribe(self, tokens):
        self.subscribed.extend(tokens)

    def set_mode(self, mode, tokens):
        self.modes.append((mode, list(tokens)))

    def close(self):
        self.connected = False

    def push(self, ticks):
        self.on_ticks(self, ticks)


class FakeAdapter:
    def __init__(self, tokens=None, quote=None):
        self.creds = SimpleNamespace(api_key="k")
        self.access_token = "tok"
        self._tokens = tokens or {}
        self._quote = quote or {}
        self.quote_calls: list[list[str]] = []

    def instrument_tokens(self, symbols):
        return {s: self._tokens[s] for s in symbols if s in self._tokens}

    def get_quote(self, symbols):
        self.quote_calls.append(list(symbols))
        return {s: self._quote[s] for s in symbols if s in self._quote}


class FakeFeed:
    def __init__(self, prices):
        self.prices = prices
        self.subscribed: list[str] = []

    def ensure_subscribed(self, symbols):
        self.subscribed.extend(symbols)

    def get(self, symbol):
        return (self.prices[symbol], _time.monotonic()) if symbol in self.prices else None

    def stale(self, symbol, max_age_s):
        return symbol not in self.prices

    def status(self):
        return {}

    def stop(self):
        pass


def _feed(adapter):
    return KiteTickerFeed(adapter, ticker_factory=lambda k, t: FakeTicker(k, t))


def test_subscribe_resolves_tokens_and_ticks_cache():
    adapter = FakeAdapter(tokens={NIFTY_CE: 111, "NIFTY": 222})
    feed = _feed(adapter)
    feed.ensure_subscribed([NIFTY_CE, "NIFTY"])

    ticker = feed._ticker
    assert set(ticker.subscribed) == {111, 222}
    assert ticker.modes and ticker.modes[-1][0] == "ltp"

    # No ticks yet → stale, no price.
    assert feed.get("NIFTY") is None
    assert feed.stale("NIFTY", 10)

    ticker.push([{"instrument_token": 222, "last_price": 24500.0}])
    v = feed.get("NIFTY")
    assert v is not None and v[0] == 24500.0
    assert not feed.stale("NIFTY", 10)


def test_subscribe_dedup_only_resolves_unknown():
    adapter = FakeAdapter(tokens={"NIFTY": 222})
    feed = _feed(adapter)
    feed.ensure_subscribed(["NIFTY"])
    feed.ensure_subscribed(["NIFTY"])  # already known → no re-subscribe
    assert feed._ticker.subscribed == [222]


def test_reconnect_resubscribes_all():
    adapter = FakeAdapter(tokens={"NIFTY": 222, NIFTY_CE: 111})
    feed = _feed(adapter)
    feed.ensure_subscribed(["NIFTY", NIFTY_CE])
    ticker = feed._ticker
    ticker.subscribed.clear()
    # Simulate a reconnect: the socket fires on_connect again with an empty sub set.
    ticker.on_connect(ticker, {"reconnect": True})
    assert set(ticker.subscribed) == {222, 111}


def test_near_cap_warns(monkeypatch, caplog):
    monkeypatch.setattr(pricefeed, "_SUB_WARN", 2)
    adapter = FakeAdapter(tokens={"A": 1, "B": 2, "C": 3})
    feed = _feed(adapter)
    with caplog.at_level("WARNING"):
        feed.ensure_subscribed(["A", "B", "C"])
    assert any("3000-instrument cap" in r.message for r in caplog.records)


def test_feed_quote_source_prefers_fresh_feed_then_rest():
    feed = FakeFeed(prices={"A": 10.0})           # A fresh from feed; B missing
    rest = ZerodhaQuoteSource(FakeAdapter(quote={"A": 99.0, "B": 20.0}))
    qs = FeedQuoteSource(feed, rest, stale_s=10)

    out = qs.get_quotes(["A", "B"])
    assert out == {"A": 10.0, "B": 20.0}          # A from feed (not 99), B from REST
    assert rest.adapter.quote_calls == [["B"]]    # only the missing symbol hit REST
    assert feed.subscribed == ["A", "B"]          # feed was asked to subscribe both
    assert qs.adapter is rest.adapter             # gate/wiring compatibility


def test_feed_quote_source_all_rest_when_feed_dead():
    feed = FakeFeed(prices={})                    # feed never has data (dead socket)
    rest = ZerodhaQuoteSource(FakeAdapter(quote={"A": 1.0, "B": 2.0}))
    qs = FeedQuoteSource(feed, rest, stale_s=10)
    assert qs.get_quotes(["A", "B"]) == {"A": 1.0, "B": 2.0}


def test_build_quote_source_gating(monkeypatch):
    pricefeed._feeds.clear()
    adapter = FakeAdapter(tokens={})
    zer = SimpleNamespace(id=1, broker="zerodha")
    dhan = SimpleNamespace(id=2, broker="dhan")

    def settings(enabled):
        return SimpleNamespace(ws_feed_enabled=enabled, ws_feed_stale_s=10.0)

    monkeypatch.setattr(pricefeed, "get_settings", lambda: settings(True))
    qs = pricefeed.build_quote_source(zer, adapter)
    assert isinstance(qs, FeedQuoteSource)
    assert qs.adapter is adapter

    # Feed disabled → legacy REST source.
    monkeypatch.setattr(pricefeed, "get_settings", lambda: settings(False))
    assert type(pricefeed.build_quote_source(zer, adapter)).__name__ == "ZerodhaQuoteSource"

    # Non-zerodha account → REST even when enabled (feed is Kite-specific).
    monkeypatch.setattr(pricefeed, "get_settings", lambda: settings(True))
    assert type(pricefeed.build_quote_source(dhan, adapter)).__name__ == "ZerodhaQuoteSource"


def test_feed_for_rebuilds_on_token_change():
    pricefeed._feeds.clear()
    a1 = FakeAdapter()
    a1.access_token = "old"
    f1 = pricefeed.feed_for(7, a1)
    assert pricefeed.feed_for(7, a1) is f1          # same token → cached
    a2 = FakeAdapter()
    a2.access_token = "new"
    f2 = pricefeed.feed_for(7, a2)
    assert f2 is not f1                              # token changed → rebuilt
