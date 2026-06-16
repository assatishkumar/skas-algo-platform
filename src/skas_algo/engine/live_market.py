"""LiveMarketView — the real-time analogue of MarketView.

Seeded with each symbol's historical closes (up to "yesterday") so the rolling
Donchian levels are defined, then fed today's prices via ``update_quote``. The
rolling window is the last ``lookback`` historical closes — identical to the
backtest's prior-N-excluding-today window — so a strategy reading this view behaves
exactly as it does in backtest (proven in tests/test_mode_equivalence.py).

``roll_forward`` moves today's quotes into history (end of a trading day), advancing
the view to the next day. Implements the MarketLike protocol.
"""

from __future__ import annotations


class LiveMarketView:
    def __init__(self, lookback: int):
        self.lookback = lookback
        self._hist: dict[str, list[float]] = {}  # closes strictly before "today", chronological
        self._quotes: dict[str, float] = {}  # today's live prices
        self._order: list[str] = []  # universe order

    # ----------------------------------------------------------- building
    def seed(self, symbol: str, closes: list[float]) -> None:
        self._hist[symbol] = list(closes)
        if symbol not in self._order:
            self._order.append(symbol)

    def update_quote(self, symbol: str, price: float) -> None:
        if symbol not in self._order:
            self._order.append(symbol)
            self._hist.setdefault(symbol, [])
        self._quotes[symbol] = price

    def roll_forward(self) -> None:
        """End the day: today's quotes become history; clear quotes for the next day."""
        for symbol, price in self._quotes.items():
            self._hist.setdefault(symbol, []).append(price)
        self._quotes = {}

    # ------------------------------------------------------------- query
    def _rolling(self, symbol: str) -> tuple[float, float, float] | None:
        hist = self._hist.get(symbol, [])
        if len(hist) < self.lookback:
            return None
        window = hist[-self.lookback :]
        return max(window), min(window), sum(window) / len(window)

    def present_symbols(self) -> list[str]:
        return [s for s in self._order if s in self._quotes and self._rolling(s) is not None]

    def close(self, symbol: str) -> float:
        return self._quotes[symbol]

    def rolling_high(self, symbol: str) -> float:
        return self._rolling(symbol)[0]  # type: ignore[index]

    def rolling_low(self, symbol: str) -> float:
        return self._rolling(symbol)[1]  # type: ignore[index]

    def rolling_mean(self, symbol: str) -> float:
        """The trailing N-close moving average (DMA), excluding today."""
        return self._rolling(symbol)[2]  # type: ignore[index]

    def closes_today(self) -> dict[str, float]:
        return dict(self._quotes)

    def mark_prices(self) -> dict[str, float]:
        """Today's quote per symbol, else its last known historical close."""
        out: dict[str, float] = {}
        for s in self._order:
            if s in self._quotes:
                out[s] = self._quotes[s]
            elif self._hist.get(s):
                out[s] = self._hist[s][-1]
        return out

    # ----------------------------------------------------- introspection
    def universe(self) -> list[str]:
        return list(self._order)

    def quote(self, symbol: str) -> float | None:
        return self._quotes.get(symbol)

    def last_close(self, symbol: str) -> float | None:
        """Live quote if present, else the last seeded/rolled historical close."""
        if symbol in self._quotes:
            return self._quotes[symbol]
        hist = self._hist.get(symbol)
        return hist[-1] if hist else None

    def levels(self, symbol: str) -> tuple[float, float] | None:
        """(rolling_high, rolling_low) from history, or None if insufficient."""
        return self._rolling(symbol)
