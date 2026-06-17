"""SuperTrend Momentum — an SST-style trend-rider driven by the SuperTrend indicator.

Entry:  buy one lot when SuperTrend flips GREEN (direction −1 → +1) on the chosen timeframe.
Exit:   a fixed configurable % profit AND/OR a SuperTrend RED flip:
        * a RED flip always exits whatever remains;
        * at the % target, book ``partial_book_pct`` of the position (default 50%) and let the
          remainder ride until the RED flip. partial_book_pct = 1.0 → full exit at the target;
          partial_book_pct = 0 → ignore the % target (pure SuperTrend exit on red).

Timeframe ∈ {daily, weekly, monthly}: the SuperTrend direction (computed from OHLC by the market
view and read via ``ctx.supertrend_dir``) reflects the chosen timeframe, so a flip occurs on the
relevant bar's close. Sizing reuses SST's capital/parts (fixed or equity-scaled). Runs unchanged
in BACKTEST and PAPER/LIVE (live SuperTrend is computed from the cached OHLC).
"""

from __future__ import annotations

from typing import Any

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class SuperTrendMomentumStrategy:
    strategy_id = "supertrend_momentum"
    needs_supertrend = True  # tells the build wiring to compute SuperTrend for this run
    report_deployed_metrics = True  # adds deployed-capital + idle-cash CAGR to the report

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 2_500_000,
        capital_parts: int = 50,
        allocation_mode: str = "fixed",      # "fixed" | "equity_scaled"
        timeframe: str = "daily",            # "daily" | "weekly" | "monthly"
        supertrend_period: int = 10,         # ATR period (configurable)
        supertrend_multiplier: float = 3.0,  # ATR band multiplier (configurable)
        profit_target: float = 0.05,         # book at +this% over average cost
        partial_book_pct: float = 0.5,       # share booked at the target (1.0 = full, 0 = none)
        entry_mode: str = "flip",            # "flip" = buy on green flip; "pullback" = wait for a dip + breakout
        pullback_pct: float = 0.0,           # min dip below the post-flip peak to count as a pullback
        idle_return: float = 0.06,           # reporting-only: assumed annual yield on idle cash
        **_ignored,
    ):
        self.universe = universe
        self.capital_parts = int(capital_parts)
        self.allocation_mode = allocation_mode
        self.allocation_amount = initial_capital / capital_parts
        self.timeframe = str(timeframe).lower()
        self.supertrend_period = int(supertrend_period)
        self.supertrend_multiplier = float(supertrend_multiplier)
        self.profit_target = float(profit_target)
        self.partial_book_pct = float(partial_book_pct)
        self.entry_mode = str(entry_mode).lower()
        self.pullback_pct = float(pullback_pct)
        self.idle_return = float(idle_return)
        # Per-symbol state: last seen SuperTrend direction + whether we've booked the partial.
        self.prev_dir: dict[str, float] = {}
        self.partial_booked: dict[str, bool] = {}
        # Pending pullback setups (pullback mode): symbol -> {peak, pulled_back, pivot}.
        self.setup: dict[str, dict] = {}

    def supertrend_config(self) -> dict:
        """Params the market view needs to precompute SuperTrend for this run."""
        return {
            "period": self.supertrend_period,
            "multiplier": self.supertrend_multiplier,
            "timeframe": self.timeframe,
        }

    def _allocation(self, ctx: AlgoContext) -> float:
        if self.allocation_mode == "equity_scaled":
            return ctx.equity() / self.capital_parts
        return self.allocation_amount

    # ------------------------------------------------------- (de)serialize
    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.export_state()

    def export_state(self) -> dict[str, Any]:
        return {
            "prev_dir": dict(self.prev_dir),
            "partial_booked": dict(self.partial_booked),
            "setup": {s: dict(v) for s, v in self.setup.items()},
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.prev_dir = {k: float(v) for k, v in state.get("prev_dir", {}).items()}
        self.partial_booked = {**self.partial_booked, **state.get("partial_booked", {})}
        self.setup = {s: dict(v) for s, v in state.get("setup", {}).items()}

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        present = ctx.present_symbols()
        signals: list[Signal] = []
        running_cash = ctx.cash
        allocation = self._allocation(ctx)
        held = set(ctx.lot_symbols())

        # --- Step 1: exits (held names) — RED flip exits the remainder; % target books a share ---
        for sym in held:
            if sym not in present:
                continue
            dir_now = ctx.supertrend_dir(sym)
            if dir_now is None:
                continue
            lots = ctx.lots(sym)
            if not lots:
                continue
            close = ctx.close(sym)
            units = sum(lot.units for lot in lots)
            avg = sum(lot.units * lot.price for lot in lots) / units if units else 0.0

            if dir_now < 0:  # SuperTrend red → exit everything that remains
                signals.append(Signal(symbol=sym, action=SignalAction.EXIT_ALL, reason="supertrend_red"))
                running_cash += units * close
                self.partial_booked[sym] = False
                continue

            # Still green: book the configured share once, at the % target.
            if (
                self.partial_book_pct > 0
                and not self.partial_booked.get(sym, False)
                and avg > 0
                and (close - avg) / avg >= self.profit_target
            ):
                book_units = int(round(units * self.partial_book_pct))
                if self.partial_book_pct >= 1.0 or book_units >= units:
                    signals.append(Signal(symbol=sym, action=SignalAction.EXIT_ALL, reason="target"))
                    running_cash += units * close
                    self.partial_booked[sym] = False
                elif book_units > 0:
                    lot = lots[0]  # one lot per entry → book part of it; remainder rides to red
                    signals.append(Signal(symbol=sym, action=SignalAction.EXIT, lot_id=lot.id,
                                          quantity=book_units, reason="partial_target",
                                          meta={"tag": "BOOK"}))
                    running_cash += book_units * close
                    self.partial_booked[sym] = True

        # --- Step 2: entries — buy one lot on a GREEN flip ("flip"), or after a pullback +
        #     breakout of the post-flip high ("pullback") ---
        def _buy(sym: str, close: float) -> bool:
            nonlocal running_cash
            if running_cash < allocation:
                return False
            units = int(allocation // close)
            if units <= 0:
                return False
            running_cash -= units * close
            signals.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG, quantity=units))
            self.partial_booked[sym] = False
            return True

        for sym in present:
            if sym in held:
                continue
            dir_now = ctx.supertrend_dir(sym)
            prev = self.prev_dir.get(sym)
            if dir_now is None or prev is None:
                continue  # need a prior direction to detect an actual flip (no mid-trend entry)
            close = ctx.close(sym)
            flipped_green = prev < 0 and dir_now > 0

            if self.entry_mode != "pullback":
                if flipped_green:
                    _buy(sym, close)
                continue

            # Pullback mode: arm on the green flip, then enter on the breakout of the post-flip
            # high after a dip. A red flip cancels the pending setup.
            if dir_now < 0:
                self.setup.pop(sym, None)
                continue
            if flipped_green:
                self.setup[sym] = {"peak": close, "pulled_back": False, "pivot": None}
            s = self.setup.get(sym)
            if s is None:
                continue  # green but no fresh flip armed (don't enter mid-trend)
            if not s["pulled_back"]:
                if close > s["peak"]:
                    s["peak"] = close
                elif s["peak"] > 0 and (s["peak"] - close) / s["peak"] >= self.pullback_pct and close < s["peak"]:
                    s["pulled_back"] = True
                    s["pivot"] = s["peak"]  # the prior high to break for entry
                continue
            if close > s["pivot"] and _buy(sym, close):  # breakout above the pre-pullback high
                self.setup.pop(sym, None)

        # --- Step 3: remember today's direction for the next slice's flip detection ---
        for sym in present:
            d = ctx.supertrend_dir(sym)
            if d is not None:
                self.prev_dir[sym] = d

        return signals
