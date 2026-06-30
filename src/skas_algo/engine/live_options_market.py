"""LiveOptionsMarketView — the real-time analogue of the backtest OptionMarketView.

Strike/expiry selection comes from an ``OptionChainView`` (backed by the skas-data
cache — strikes/expiries don't change intraday); contract **marks** come from live
quotes fed via ``update_quote`` (Zerodha LTP), with a cache fallback (last bhavcopy
close) for any contract not yet quoted. A ``current_datetime`` cursor drives
``ctx.now()`` / ``ctx.today()`` so the options strategies' intraday exit cadences work.

Satisfies the same surface the SliceExecutor + AlgoContext use for an options run
(``chain``, ``close``, ``has_print``, ``closes_today``, ``mark_prices``,
``present_symbols``), so the exact same decision/execution path drives it.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from skas_algo.db.enums import OrderSide
from skas_algo.engine.options.instrument import is_option_symbol, parse


class LiveOptionsMarketView:
    def __init__(self, chain, *, loader=None, current_datetime: datetime | None = None,
                 index_spots: dict[str, float] | None = None):
        self.chain = chain                       # OptionChainView (ctx.option_chain())
        self._cache_chain = chain                # the cache-backed view (LiveChainView falls back to it)
        self._loader = loader                    # optional cache loader(symbol, lo, hi)
        self._now = current_datetime or datetime.now()
        self._quotes: dict[str, float] = {}      # live LTP per contract symbol (today)
        self._last_close: dict[str, float] = {}  # forward-filled mark per symbol
        # Live underlying spot per index (fed from the index LTP) — used for strike
        # selection so live entries don't pick strikes off a stale cached close.
        self._index_spots: dict[str, float] = index_spots if index_spots is not None else {}
        # Optional live-quote callback (the deployment's quote source). Lets close() fetch
        # a freshly-selected contract's LIVE price at fill time, so an entry doesn't fill at
        # a days-stale cached close (which would book fake instant P&L).
        self._quote_fn = None
        # Optional live full-chain callback (underlying, expiry_iso) -> chain dict. Lets a
        # multi-underlying strategy (donchian) pick a delta-based strike off the LIVE chain of
        # ANY name at flip time — the per-symbol quote_fn can't enumerate strikes.
        self._chain_fn = None
        # Short-TTL cache of fetched live chains, keyed (underlying, expiry). A basket entry prices
        # every leg's bid/ask via live_chain() — without this it refetches the same name's chain once
        # per leg (CE + PE), doubling the broker round-trips. Chains are ~static intraday.
        self._chain_cache: dict[tuple[str, str], tuple[dict, float]] = {}

    _CHAIN_TTL = 20.0  # seconds

    def set_quote_fn(self, quote_fn) -> None:
        self._quote_fn = quote_fn

    def set_chain_fn(self, chain_fn) -> None:
        self._chain_fn = chain_fn

    def live_chain(self, underlying: str, expiry: str) -> dict | None:
        """LIVE option chain for any underlying/expiry (strikes + CE/PE LTP/bid/ask + spot), or None.
        Cached per (underlying, expiry) for a few seconds so a basket entry doesn't refetch the same
        chain once per leg."""
        if self._chain_fn is None:
            return None
        key = (underlying.upper(), expiry)
        hit = self._chain_cache.get(key)
        if hit is not None and (time.monotonic() - hit[1]) < self._CHAIN_TTL:
            return hit[0]
        try:
            chain = self._chain_fn(underlying, expiry)
        except Exception:  # pragma: no cover - network hiccup → caller falls back
            return None
        if chain is not None:
            self._chain_cache[key] = (chain, time.monotonic())
        return chain

    def prefetch_quotes(self, symbols) -> None:
        """Batch-fetch live quotes for many contracts in ONE quote-source call, so a basket entry
        prices all its legs from cache instead of one round-trip per leg via close(). No-op without a
        live quote fn (cache source) — close() then falls back per-leg as before."""
        if self._quote_fn is None:
            return
        want = [s for s in dict.fromkeys(symbols) if s not in self._quotes and is_option_symbol(s)]
        if not want:
            return
        try:
            q = self._quote_fn(want)
        except Exception:  # pragma: no cover - network hiccup → close() falls back per-leg
            return
        self.update_quotes({s: p for s, p in q.items() if p is not None})

    def set_chain_adapter(self, adapter, underlying: str, lot_overrides: dict | None = None) -> None:
        """Source TODAY's expiries/strikes/premiums from the broker (live) for strike selection;
        fall back to the cached chain for other dates / offline. None → revert to the cache."""
        if adapter is None:
            self.chain = self._cache_chain
            return
        from skas_algo.engine.options.live_chain import LiveChainView

        self.chain = LiveChainView(self._cache_chain, adapter, underlying, lot_overrides)

    def set_index_spot(self, underlying: str, price: float) -> None:
        self._index_spots[underlying.upper()] = price

    def index_spot(self, underlying: str) -> float | None:
        return self._index_spots.get(underlying.upper())

    # --------------------------------------------------------------- cursor
    @property
    def current_datetime(self) -> datetime:
        return self._now

    @property
    def current_date(self) -> date:
        return self._now.date()

    def set_now(self, ts: datetime) -> None:
        self._now = ts

    # --------------------------------------------------------------- quotes
    def update_quote(self, symbol: str, price: float) -> None:
        self._quotes[symbol] = price
        self._last_close[symbol] = price

    def update_quotes(self, quotes: dict[str, float]) -> None:
        for symbol, price in quotes.items():
            self.update_quote(symbol, price)

    def roll_forward(self) -> None:
        """End the day: today's quotes persist as the mark; clear the live tick set."""
        self._quotes = {}

    # --------------------------------------------------------------- query
    def _ensure_mark(self, symbol: str) -> None:
        if symbol in self._last_close or self._loader is None:
            return
        df = self._loader(symbol, self.current_date - timedelta(days=7), self.current_date)
        if df is not None and not df.empty:
            self._last_close[symbol] = float(df.iloc[-1]["close"])

    def close(self, symbol: str) -> float:
        if symbol in self._quotes:
            return self._quotes[symbol]
        # A newly-selected contract has no quote yet → fetch its live LTP once (so the fill
        # is at the live price), then fall back to the cached close.
        if self._quote_fn is not None and is_option_symbol(symbol):
            try:
                q = self._quote_fn([symbol])
            except Exception:  # pragma: no cover - network hiccup → fall back to cache
                q = {}
            if q.get(symbol) is not None:
                self.update_quote(symbol, q[symbol])
                return q[symbol]
        self._ensure_mark(symbol)
        if symbol in self._last_close:
            return self._last_close[symbol]
        raise KeyError(f"{symbol} has no live quote or cached mark at {self._now}")

    def fill_price(self, symbol: str, side: OrderSide) -> float:
        """Realistic option fill: a SELL fills at the BID (you sell into the bid), a BUY at the ASK
        (you lift the offer). This avoids booking an entry off a stale/illiquid last-traded print.
        Falls back to ``close()`` (LTP / cached mark) when no live two-sided book is available
        (cache quote source, one-sided book, or a network hiccup)."""
        if is_option_symbol(symbol):
            ba = self._bid_ask(symbol)
            if ba is not None:
                bid, ask = ba
                if side is OrderSide.SELL and bid and bid > 0:
                    return float(bid)
                if side is OrderSide.BUY and ask and ask > 0:
                    return float(ask)
        return self.close(symbol)

    def _bid_ask(self, symbol: str) -> tuple | None:
        """(bid, ask) for one option contract from the LIVE chain, or None when unavailable."""
        inst = parse(symbol)
        if inst is None:
            return None
        chain = self.live_chain(inst.underlying, inst.expiry.isoformat())
        if not chain:
            return None
        for row in chain.get("rows", []):
            try:
                if float(row.get("strike")) == float(inst.strike):
                    leg = row.get("ce") if inst.right == "CE" else row.get("pe")
                    return (leg.get("bid"), leg.get("ask")) if leg else None
            except (TypeError, ValueError):
                continue
        return None

    def has_print(self, symbol: str) -> bool:
        """True only when a fresh live tick exists (the strategy's stale-mark guard)."""
        return symbol in self._quotes

    def present_symbols(self) -> list[str]:
        return list(self._quotes)

    def closes_today(self) -> dict[str, float]:
        return dict(self._quotes)

    def mark_prices(self) -> dict[str, float]:
        return dict(self._last_close)

    def load_marks(self, marks: dict) -> None:
        """Restore the last-known marks (persisted across restarts) so an options run that recovers
        while DISCONNECTED still prices its legs off the last live quote — single-stock option
        premiums aren't in the bhavcopy cache, so without this they'd have no mark at all. Restores
        only the forward-filled mark (``_last_close``), NOT today's live-tick set (``_quotes``), so
        ``has_print`` stays honest about whether a fresh tick has arrived."""
        for symbol, price in (marks or {}).items():
            try:
                self._last_close[symbol] = float(price)
            except (TypeError, ValueError):  # pragma: no cover - skip junk
                continue

    # Donchian levels are undefined for option contracts (present for protocol parity).
    def rolling_high(self, symbol: str) -> float:  # pragma: no cover - unused
        raise NotImplementedError("rolling levels are not defined for option contracts")

    def rolling_low(self, symbol: str) -> float:  # pragma: no cover - unused
        raise NotImplementedError("rolling levels are not defined for option contracts")
