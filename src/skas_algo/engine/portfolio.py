"""Lot-aware portfolio — the single source of truth for cash and positions.

Many Indian strategies (SST, SHOP, PKP) average into positions and exit specific
lots (LIFO/FIFO), so the portfolio tracks individual lots, not just an aggregate
quantity. Cash, lots, realized PnL, and the monthly tax/withdrawal flush all live
here so every mode (BACKTEST/PAPER/LIVE) shares one accounting path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import count


@dataclass
class Lot:
    """One purchase lot of a symbol."""

    id: int
    symbol: str
    units: int
    price: float
    opened_at: date | datetime


@dataclass
class MonthlyFlush:
    tax: float
    withdrawal: float
    realized: float


@dataclass
class Portfolio:
    cash: float
    _lots: dict[str, list[Lot]] = field(default_factory=dict)
    _ids: count = field(default_factory=lambda: count(1))

    # Realized PnL accumulated within the current calendar month (for tax/withdrawal).
    month_realized: float = 0.0
    total_taxes: float = 0.0
    total_withdrawals: float = 0.0

    # ------------------------------------------------------------------ trades
    def buy(self, symbol: str, units: int, price: float, when: date | datetime) -> Lot:
        """Open a new lot, paying cash."""
        self.cash -= units * price
        lot = Lot(id=next(self._ids), symbol=symbol, units=units, price=price, opened_at=when)
        self._lots.setdefault(symbol, []).append(lot)
        return lot

    def close_lot(self, symbol: str, lot_id: int, price: float) -> float:
        """Sell an entire lot at ``price``; return realized profit (gross)."""
        lots = self._lots.get(symbol, [])
        for i, lot in enumerate(lots):
            if lot.id == lot_id:
                revenue = lot.units * price
                profit = revenue - lot.units * lot.price
                self.cash += revenue
                self.month_realized += profit
                lots.pop(i)
                if not lots:
                    del self._lots[symbol]
                return profit
        raise KeyError(f"Lot {lot_id} not found for {symbol}")

    # ------------------------------------------------------------------ views
    def lots(self, symbol: str) -> list[Lot]:
        return list(self._lots.get(symbol, []))

    def lot_symbols(self) -> list[str]:
        """Symbols with open lots, in insertion (first-bought) order."""
        return list(self._lots.keys())

    def units(self, symbol: str) -> int:
        return sum(lot.units for lot in self._lots.get(symbol, []))

    def holdings_value(self, closes: dict[str, float]) -> float:
        """Mark-to-market value, counting only symbols priced in ``closes`` today.

        (Matches SST: a held symbol with no print today contributes 0 that day.)
        """
        total = 0.0
        for symbol, lots in self._lots.items():
            if symbol in closes:
                total += sum(lot.units for lot in lots) * closes[symbol]
        return total

    def invested_capital(self) -> float:
        return sum(lot.units * lot.price for lots in self._lots.values() for lot in lots)

    # --------------------------------------------------------- monthly flush
    def flush_month(self, tax_rate: float, withdrawal_rate: float) -> MonthlyFlush | None:
        """Apply tax (and optional withdrawal) on the month's realized profit."""
        gross = self.month_realized
        if gross <= 0:
            self.month_realized = 0.0
            return None
        tax = gross * tax_rate
        self.cash -= tax
        self.total_taxes += tax
        net = gross - tax
        withdrawal = 0.0
        if net > 0 and withdrawal_rate > 0:
            withdrawal = net * withdrawal_rate
            self.cash -= withdrawal
            self.total_withdrawals += withdrawal
        self.month_realized = 0.0
        return MonthlyFlush(tax=tax, withdrawal=withdrawal, realized=gross)
