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

from datetime import date, datetime, timedelta

from skas_algo.engine.options.instrument import is_option_symbol


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

    def set_quote_fn(self, quote_fn) -> None:
        self._quote_fn = quote_fn

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

    def has_print(self, symbol: str) -> bool:
        """True only when a fresh live tick exists (the strategy's stale-mark guard)."""
        return symbol in self._quotes

    def present_symbols(self) -> list[str]:
        return list(self._quotes)

    def closes_today(self) -> dict[str, float]:
        return dict(self._quotes)

    def mark_prices(self) -> dict[str, float]:
        return dict(self._last_close)

    # Donchian levels are undefined for option contracts (present for protocol parity).
    def rolling_high(self, symbol: str) -> float:  # pragma: no cover - unused
        raise NotImplementedError("rolling levels are not defined for option contracts")

    def rolling_low(self, symbol: str) -> float:  # pragma: no cover - unused
        raise NotImplementedError("rolling levels are not defined for option contracts")
