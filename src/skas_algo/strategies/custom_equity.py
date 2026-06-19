"""Custom single-stock "trade" — an immediate or GTT-style managed equity position.

The Trade UI lets a user place one long position on a stock, either **immediately** or when the
price crosses a **trigger** (engine-managed GTT: the live loop watches the LTP and fires a market
order on the cross), then manages the exit by target %, hard stop %, and/or a trailing stop %.

One-shot: once the position exits it does not re-enter. Long-only (CNC); short-sell is out of scope.
Runs unchanged in backtest / paper / live — it only reads ``ctx.close(symbol)`` and emits Signals.
"""

from __future__ import annotations

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class CustomEquityStrategy:
    strategy_id = "custom_equity"
    intraday = True  # decide every tick so the trigger / SL / trailing react intraday

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 2_500_000,
        symbol: str | None = None,
        qty: int = 0,                          # explicit share count; 0 → size from capital
        entry_mode: str = "immediate",        # "immediate" | "trigger"
        trigger_price: float | None = None,
        target_pct: float | None = None,      # exit at +x from entry (fraction)
        stop_pct: float | None = None,        # hard stop at −x from entry (fraction)
        trailing: bool = False,
        trail_pct: float | None = None,       # trail x below the high-water mark (fraction)
        **_ignored,
    ):
        self.symbol = (symbol or (universe[0] if universe else "")).upper()
        self.qty = int(qty or 0)
        self.entry_mode = entry_mode
        self.trigger_price = trigger_price
        self.target_pct = target_pct
        self.stop_pct = stop_pct
        self.trailing = bool(trailing)
        self.trail_pct = trail_pct
        self.initial_capital = initial_capital

        # State (persisted for live recovery).
        self.entered = False
        self.done = False
        self.entry_price = 0.0
        self.hwm = 0.0
        self._arm_above: bool | None = None  # trigger mode: True if price started below the trigger

    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        if self.done or not self.symbol:
            return []
        try:
            close = ctx.close(self.symbol)
        except KeyError:
            return []  # no quote yet this slice
        if not self.entered:
            return self._maybe_enter(close)
        return self._manage(ctx, close)

    def _maybe_enter(self, close: float) -> list[Signal]:
        if self.entry_mode == "trigger" and self.trigger_price is not None:
            if self._arm_above is None:
                # Arm relative to where price sits now → fire when it crosses to the other side
                # (handles both "buy on breakout above" and "buy on the dip to").
                self._arm_above = close < self.trigger_price
            crossed = (self._arm_above and close >= self.trigger_price) or (
                not self._arm_above and close <= self.trigger_price
            )
            if not crossed:
                return []
        units = self.qty if self.qty > 0 else int(self.initial_capital // close)
        if units <= 0:
            return []
        self.entered = True
        self.entry_price = close
        self.hwm = close
        return [Signal(self.symbol, SignalAction.ENTER_LONG, quantity=units, reason="custom_trade")]

    def _manage(self, ctx: AlgoContext, close: float) -> list[Signal]:
        if not ctx.lots(self.symbol):
            self.done = True  # position exited (fill landed) — one-shot
            return []
        self.hwm = max(self.hwm, close)
        entry = self.entry_price
        if entry <= 0:
            return []
        if self.target_pct is not None and close >= entry * (1 + self.target_pct):
            return self._exit("target")
        if self.stop_pct is not None and close <= entry * (1 - self.stop_pct):
            return self._exit("stop")
        if self.trailing and self.trail_pct and close <= self.hwm * (1 - self.trail_pct):
            return self._exit("trailing_stop")
        return []

    def _exit(self, reason: str) -> list[Signal]:
        self.done = True  # one-shot: stop managing/re-entering once we've decided to exit
        return [Signal(self.symbol, SignalAction.EXIT_ALL, reason=reason)]

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "entered": self.entered,
            "done": self.done,
            "entry_price": self.entry_price,
            "hwm": self.hwm,
            "arm_above": self._arm_above,
        }

    def load_state(self, state: dict) -> None:
        self.entered = bool(state.get("entered", False))
        self.done = bool(state.get("done", False))
        self.entry_price = float(state.get("entry_price", 0.0))
        self.hwm = float(state.get("hwm", 0.0))
        self._arm_above = state.get("arm_above")
