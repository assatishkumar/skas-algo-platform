"""happy_twins — weekly dual-SuperTrend trend follower (owner's spec, 2026-08-18).

One indicator family, two speeds, long only:

    ENTER when the FAST SuperTrend (ATR 2, factor 1, weekly) TURNS green — a transition,
    not a state, so a freshly recovered/deployed run never chases an old signal;
    EXIT while the SLOW SuperTrend (ATR 3, factor 1, weekly) is red — a state, so a flip
    missed across a restart still closes the position on the next bar.

The fast trigger gets you in early; handing the exit to the slow one stops the whipsaw
that a single fast ST inflicts. Both series ride the generic indicator precompute
(``kind: "supertrend"`` — MarketView computes them from raw OHLC with weekly resampling,
flips visible the next trading day, no lookahead).

Sizing: each symbol gets an equal slot of CURRENT equity (equity / universe size) at entry
— single-instrument runs are all-in, multi-symbol runs are equal-weight, and growth
compounds into later entries. LIVE: the live view has no indicator seeding yet, so a
deploy FAILS CLOSED (no entries, no blind exits) — backtest-first, like gap_reversal.
"""

from __future__ import annotations

from typing import Any

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class HappyTwinsStrategy:
    strategy_id = "happy_twins"
    needs_indicators = True

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 1_000_000,
        fast_period: int = 2,        # ATR bars of the ENTRY SuperTrend
        fast_multiplier: float = 1.0,
        slow_period: int = 3,        # ATR bars of the EXIT SuperTrend
        slow_multiplier: float = 1.0,
        timeframe: str = "weekly",   # daily | weekly | monthly (bars both STs run on)
        **_ignored,
    ):
        self.universe = universe
        self.initial_capital = float(initial_capital)
        self.fast_period = int(fast_period)
        self.fast_multiplier = float(fast_multiplier)
        self.slow_period = int(slow_period)
        self.slow_multiplier = float(slow_multiplier)
        self.timeframe = str(timeframe)
        # prior slice's fast direction per symbol — what makes the entry a TRANSITION.
        self.prev_fast: dict[str, float] = {}

    def indicator_config(self) -> dict:
        return {
            "st_fast": {"kind": "supertrend", "period": self.fast_period,
                        "multiplier": self.fast_multiplier, "timeframe": self.timeframe},
            "st_slow": {"kind": "supertrend", "period": self.slow_period,
                        "multiplier": self.slow_multiplier, "timeframe": self.timeframe},
        }

    # ------------------------------------------------------------- persistence
    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def export_state(self) -> dict[str, Any]:
        return {"prev_fast": dict(self.prev_fast)}

    def load_state(self, state: dict[str, Any]) -> None:
        self.prev_fast = {k: float(v) for k, v in (state.get("prev_fast") or {}).items()}

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        signals: list[Signal] = []
        present = ctx.present_symbols()
        if not present:
            return signals
        held = set(ctx.lot_symbols())

        for sym in self.universe:
            if sym not in present:
                continue
            fast = ctx.indicator(sym, "st_fast")
            slow = ctx.indicator(sym, "st_slow")

            if sym in held:
                # EXIT: slow ST red — state-based, so a restart can never strand a
                # position that flipped while we were down.
                if slow is not None and slow < 0:
                    signals.append(Signal(symbol=sym, action=SignalAction.EXIT_ALL))
            elif fast is not None:
                prev = self.prev_fast.get(sym)
                if prev is not None and prev < 0 and fast > 0:
                    # ENTER on the green FLIP only. Equal slot of current equity;
                    # skip (never shrink) when cash is short.
                    slot = ctx.equity() / max(len(self.universe), 1)
                    close = ctx.close(sym)
                    units = int(slot // close) if close > 0 else 0
                    if units > 0 and ctx.cash >= units * close:
                        signals.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG,
                                              quantity=units))
            if fast is not None:
                self.prev_fast[sym] = float(fast)
        return signals
