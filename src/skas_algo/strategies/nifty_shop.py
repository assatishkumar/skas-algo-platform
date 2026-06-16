"""Nifty_Shop — "shop" the most beaten-down names below their 20-DMA, average the dips.

A long-only equity accumulator (cousin of SST) for a universe like NIFTY 50:

Selection
  Each day, rank the universe by how far the close sits BELOW its N-day moving average
  (default 20-DMA) and take the ``num_candidates`` (5) most-below names.

Entry
  * Case 1 — if any of those 5 is NOT already held: buy up to ``new_buys_per_day`` (2) of
    the not-held names (most-below first; 1 if only 1 is available).
  * Case 2 — if all 5 are already held: among them, find names that have dropped more than
    ``avg_down_pct`` (3%) from their LAST entry price and average into the worst performer
    (``max_avg_per_day`` = 1 averaging trade/day).

Sizing
  Every buy invests the SAME RUPEE AMOUNT = ``allocation_pct`` (4%) of CURRENT equity (not a
  fixed quantity) → the per-trade size scales with the book (built-in compounding). A trade
  is skipped (you "wait") when there isn't enough cash for it.

Exit
  Sell a name (whole position) once its close is ``profit_target`` (5%, configurable) above
  the average buy price.
"""

from __future__ import annotations

from typing import Any

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class NiftyShopStrategy:
    strategy_id = "nifty_shop"

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 1_000_000,
        allocation_pct: float = 0.04,     # rupees per trade = this × current equity
        profit_target: float = 0.05,      # exit a name at +5% over its average cost
        num_candidates: int = 5,          # rank the N most-below-DMA names
        new_buys_per_day: int = 2,        # Case 1: open up to this many not-held names/day
        avg_down_pct: float = 0.03,       # Case 2: average a name down >3% from last entry
        max_avg_per_day: int = 1,         # Case 2: averaging trades allowed per day
        lookback: int | None = None,      # DMA window; defaults to the run's lookback (20)
        **_ignored,                       # tolerate SST-style params from shared forms
    ):
        self.universe = universe
        self.initial_capital = float(initial_capital)
        self.allocation_pct = float(allocation_pct)
        self.profit_target = float(profit_target)
        self.num_candidates = int(num_candidates)
        self.new_buys_per_day = int(new_buys_per_day)
        self.avg_down_pct = float(avg_down_pct)
        self.max_avg_per_day = int(max_avg_per_day)
        self.lookback = lookback  # informational; the market view's window is authoritative

    # Stateless — every decision is derived from the portfolio + market each slice.
    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def export_state(self) -> dict[str, Any]:
        return {}

    def load_state(self, state: dict[str, Any]) -> None:
        return None

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        present = ctx.present_symbols()
        if not present:
            return []
        signals: list[Signal] = []
        selling: set[str] = set()
        running_cash = ctx.cash

        # --- 1) Exits: whole position out at +profit_target over average cost ---
        for sym in ctx.lot_symbols():
            if sym not in present:
                continue  # no fresh price to value/exit against
            lots = ctx.lots(sym)
            if not lots:
                continue
            close = ctx.close(sym)
            units = sum(lot.units for lot in lots)
            avg = sum(lot.units * lot.price for lot in lots) / units
            if avg > 0 and (close - avg) / avg >= self.profit_target:
                signals.append(Signal(symbol=sym, action=SignalAction.EXIT_ALL))
                selling.add(sym)
                running_cash += units * close

        # --- 2) Selection: the N names furthest BELOW their DMA ---
        scored: list[tuple[float, str, float]] = []
        for sym in present:
            dma = ctx.rolling_mean(sym)
            if not dma or dma <= 0:
                continue
            close = ctx.close(sym)
            belowness = (dma - close) / dma  # +ve = below the DMA
            if belowness > 0:
                scored.append((belowness, sym, close))
        scored.sort(key=lambda x: x[0], reverse=True)
        candidates = scored[: self.num_candidates]
        if not candidates:
            return signals

        held = {s for s in ctx.lot_symbols() if s not in selling}
        allocation = self.allocation_pct * ctx.equity()

        def _buy(sym: str, close: float) -> bool:
            nonlocal running_cash
            if allocation <= 0:
                return False
            units = int(allocation // close)
            if units <= 0 or running_cash < units * close:
                return False  # not enough cash → wait
            running_cash -= units * close
            signals.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG, quantity=units))
            return True

        new_candidates = [(b, s, c) for (b, s, c) in candidates if s not in held]
        if new_candidates:
            # Case 1 — open up to new_buys_per_day not-held names (most-below first).
            bought = 0
            for _b, sym, close in new_candidates:
                if bought >= self.new_buys_per_day:
                    break
                if _buy(sym, close):
                    bought += 1
        else:
            # Case 2 — all candidates held → average the worst performer(s) that have
            # dropped more than avg_down_pct from their last entry price.
            qualifiers: list[tuple[float, str, float]] = []
            for _b, sym, close in candidates:
                last = self._last_entry_price(ctx, sym)
                if last and (last - close) / last > self.avg_down_pct:
                    qualifiers.append(((last - close) / last, sym, close))
            qualifiers.sort(key=lambda x: x[0], reverse=True)  # worst drop first
            for _d, sym, close in qualifiers[: self.max_avg_per_day]:
                _buy(sym, close)

        return signals

    @staticmethod
    def _last_entry_price(ctx: AlgoContext, sym: str) -> float | None:
        lots = ctx.lots(sym)
        return lots[-1].price if lots else None
