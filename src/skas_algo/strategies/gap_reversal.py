"""gap_reversal — daily gap-up + 21-EMA + oversold-RSI long entries (owner spec 2026-07-28,
"Low Risk High Reward"). LONG-ONLY: Indian cash equity can't hold an overnight short, so the
spec's short leg (gap-down + RSI>90 + below-EMA) is deferred to a future sell-CE options
variant (with a sell-PE variant of this long signal).

Entry (all three, evaluated on the daily bar at the 15:20 decision):
  * the stock GAPPED UP: today's open ≥ previous close × (1 + min_gap_pct/100);
  * today's close is ABOVE the EMA(ema_period) of closes;
  * Wilder RSI(rsi_period) of the EMA SERIES (rsi_source="ema", the chart's "RSI 10
    EMA:EMA") is BELOW rsi_entry_below — the smoothed RSI saturates near 0 through a
    downtrend, so this fires on the first gap-up recovery that retakes the EMA.
Exit: close BELOW the EMA (the whole position, reason "ema_exit").

Data plumbing: the equity ctx never exposed the day's OPEN — this strategy rides the new
generic indicator precompute (``needs_indicators``/``indicator_config()`` → MarketView
computes per-date ema/rsi/gap_pct from the RAW OHLC frame, mirroring SuperTrend). LIVE:
the LiveMarketView's indicator store is EMPTY until the manager seeds it (Phase 2) — every
check reads None → the strategy FAILS CLOSED (no entries, no blind exits) rather than
trading without data. Full EOD backtest is the Phase-1 surface.
"""

from __future__ import annotations

from typing import Any

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class GapReversalStrategy:
    strategy_id = "gap_reversal"
    needs_indicators = True  # tells the build wiring to precompute ema/rsi/gap_pct

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 2_500_000,
        capital_parts: int = 10,
        allocation_mode: str = "fixed",  # "fixed" | "equity_scaled"
        min_gap_pct: float = 0.0,        # min gap-up %, 0 = any gap (open > prev close)
        ema_period: int = 21,
        rsi_period: int = 10,
        # "ema" = RSI computed ON the EMA(ema_period) series (the owner's TradingView
        # "RSI 10 EMA:EMA" setup — saturates near 0/100 through trends, so the <10 band
        # fires routinely); "close" = classic Wilder RSI of raw closes (rare at <10).
        rsi_source: str = "ema",
        rsi_entry_below: float = 10.0,   # the spec's lower band
        **_ignored,
    ):
        self.universe = universe
        self.capital_parts = int(capital_parts)
        self.allocation_mode = str(allocation_mode)
        self.allocation_amount = initial_capital / self.capital_parts
        self.min_gap_pct = float(min_gap_pct)
        self.ema_period = int(ema_period)
        self.rsi_period = int(rsi_period)
        self.rsi_source = str(rsi_source or "ema").lower()
        self.rsi_entry_below = float(rsi_entry_below)

    def indicator_config(self) -> dict:
        """Params the market view needs for the per-date precompute (mirror of
        supertrend_config)."""
        return {
            "ema": {"span": self.ema_period},
            "rsi": {"period": self.rsi_period, "source": self.rsi_source,
                    "source_span": self.ema_period},
            "gap_pct": {},
        }

    def _allocation(self, ctx: AlgoContext) -> float:
        if self.allocation_mode == "equity_scaled":
            return ctx.equity() / self.capital_parts
        return self.allocation_amount

    # ------------------------------------------------------- (de)serialize
    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def export_state(self) -> dict[str, Any]:
        return {}  # stateless: everything derives from the book + today's indicators

    def load_state(self, state: dict[str, Any]) -> None:
        return None

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        present = ctx.present_symbols()
        signals: list[Signal] = []
        running_cash = ctx.cash
        allocation = self._allocation(ctx)
        held = set(ctx.lot_symbols())

        # --- exits first: close below the EMA ends the trade (fail closed on a missing EMA —
        # never exit blind; live is unseeded until Phase 2 and must simply hold) ---
        for sym in held:
            if sym not in present:
                continue
            ema = ctx.indicator(sym, "ema")
            if ema is None:
                continue
            if ctx.close(sym) < ema:
                signals.append(
                    Signal(symbol=sym, action=SignalAction.EXIT_ALL, reason="ema_exit")
                )
                running_cash += sum(lot.units for lot in ctx.lots(sym)) * ctx.close(sym)

        # --- entries: gap up + above EMA + RSI oversold (all three or nothing) ---
        for sym in present:
            if sym in held:
                continue
            gap = ctx.indicator(sym, "gap_pct")
            ema = ctx.indicator(sym, "ema")
            rsi = ctx.indicator(sym, "rsi")
            if gap is None or ema is None or rsi is None:
                continue  # warming up / live not seeded → fail closed
            close = ctx.close(sym)
            # min_gap_pct=0 = the literal spec ("any gap up"): strictly above the prev close.
            threshold = self.min_gap_pct / 100.0
            gapped = gap > 0 if threshold == 0 else gap >= threshold
            if not gapped or close <= ema or rsi >= self.rsi_entry_below:
                continue
            if running_cash < allocation:
                continue
            units = int(allocation // close)
            if units <= 0:
                continue
            running_cash -= units * close
            signals.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG, quantity=units))
        return signals
