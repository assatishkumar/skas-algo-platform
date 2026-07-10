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


def _parse_opened_at(v):
    """Recover a Lot.opened_at from persisted state (export_state stores str(dt)). Parse a
    string back to datetime (or date), pass a real datetime/date through, None on junk — so
    recovered lots and post-recovery lots share one comparable type."""
    if not isinstance(v, str):
        return v
    try:
        return datetime.fromisoformat(v)   # 3.11 handles "YYYY-MM-DD HH:MM:SS[.ffffff][+HH:MM]"
    except ValueError:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None


@dataclass
class Lot:
    """One lot of a symbol.

    ``direction`` is +1 for a long (bought) lot and -1 for a short (sold-to-open) lot;
    ``multiplier`` is the contract multiplier (1 for equities and index options, where
    the lot size is carried in ``units``). Both default so every existing long-equity
    lot is constructed and valued exactly as before.
    """

    id: int
    symbol: str
    units: int
    price: float
    opened_at: date | datetime
    direction: int = 1
    multiplier: int = 1


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

    def sell_to_open(self, symbol: str, units: int, price: float, when: date | datetime,
                     multiplier: int = 1) -> Lot:
        """Open a SHORT lot, receiving premium into cash (used for option writing)."""
        self.cash += units * price * multiplier
        lot = Lot(id=next(self._ids), symbol=symbol, units=units, price=price,
                  opened_at=when, direction=-1, multiplier=multiplier)
        self._lots.setdefault(symbol, []).append(lot)
        return lot

    def buy_to_close(self, symbol: str, lot_id: int, price: float) -> float:
        """Buy back a short lot at ``price``; return realized profit (entry − exit)·units·mult."""
        lots = self._lots.get(symbol, [])
        for i, lot in enumerate(lots):
            if lot.id == lot_id:
                cost = lot.units * price * lot.multiplier
                profit = (lot.price - price) * lot.units * lot.multiplier
                self.cash -= cost
                self.month_realized += profit
                lots.pop(i)
                if not lots:
                    del self._lots[symbol]
                return profit
        raise KeyError(f"Short lot {lot_id} not found for {symbol}")

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

    def reduce_lot(self, symbol: str, lot_id: int, units: int, price: float) -> float:
        """Sell ``units`` from a lot at ``price``; keep the remainder (same lot id).

        Sells the whole lot if ``units`` >= the lot's size. Returns realized profit.
        Used for partial booking under overrides (e.g. "book 50%, trail the rest").
        """
        for lot in self._lots.get(symbol, []):
            if lot.id == lot_id:
                if units >= lot.units:
                    return self.close_lot(symbol, lot_id, price)
                revenue = units * price
                profit = revenue - units * lot.price
                self.cash += revenue
                self.month_realized += profit
                lot.units -= units
                return profit
        raise KeyError(f"Lot {lot_id} not found for {symbol}")

    def get_lot(self, symbol: str, lot_id: int) -> Lot | None:
        return next((lot for lot in self._lots.get(symbol, []) if lot.id == lot_id), None)

    def close_position(self, symbol: str, price: float) -> tuple[int, float, float, int] | None:
        """Sell every lot of a symbol at ``price`` in one go (pooled exit).

        Returns (total_units, total_cost, gross_profit, lot_count). Used by SST's
        averaged/tiered exit where all lots leave together.
        """
        lots = self._lots.get(symbol, [])
        if not lots:
            return None
        total_units = sum(lot.units for lot in lots)
        total_cost = sum(lot.units * lot.price for lot in lots)
        revenue = total_units * price
        profit = revenue - total_cost
        self.cash += revenue
        self.month_realized += profit
        n = len(lots)
        del self._lots[symbol]
        return total_units, total_cost, profit, n

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
                # Long lots are assets (+), short lots are liabilities to buy back (−).
                # For a long equity lot (direction=1, multiplier=1) this is units*close.
                total += sum(lot.direction * lot.units * closes[symbol] * lot.multiplier
                             for lot in lots)
        return total

    def invested_capital(self) -> float:
        # Long cost basis (deployed capital). Shorts use margin, tracked separately.
        return sum(lot.units * lot.price * lot.multiplier
                   for lots in self._lots.values() for lot in lots if lot.direction == 1)

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "cash": self.cash,
            "month_realized": self.month_realized,
            "total_taxes": self.total_taxes,
            "total_withdrawals": self.total_withdrawals,
            "lots": {
                sym: [
                    {
                        "id": lot.id,
                        "units": lot.units,
                        "price": lot.price,
                        "opened_at": str(lot.opened_at),
                        "direction": lot.direction,
                        "multiplier": lot.multiplier,
                    }
                    for lot in lots
                ]
                for sym, lots in self._lots.items()
            },
        }

    def load_state(self, state: dict) -> None:
        self.cash = state["cash"]
        self.month_realized = state.get("month_realized", 0.0)
        self.total_taxes = state.get("total_taxes", 0.0)
        self.total_withdrawals = state.get("total_withdrawals", 0.0)
        self._lots = {}
        max_id = 0
        for sym, lots in state.get("lots", {}).items():
            self._lots[sym] = [
                Lot(
                    id=lot["id"],
                    symbol=sym,
                    units=lot["units"],
                    price=lot["price"],
                    # export_state stringifies opened_at (str(dt)); parse it BACK to a datetime.
                    # Left as a raw string, a recovered lot and a lot opened AFTER recovery
                    # (a real datetime) were a mixed str/datetime set that crashed snapshot()'s
                    # min() — "'<' not supported between datetime and str" — blanking /live
                    # (2026-07-10, an equity FIFO run that opened a lot post-recovery).
                    opened_at=_parse_opened_at(lot["opened_at"]),
                    direction=lot.get("direction", 1),
                    multiplier=lot.get("multiplier", 1),
                )
                for lot in lots
            ]
            max_id = max([max_id, *(lot["id"] for lot in lots)])
        self._ids = count(max_id + 1)

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
