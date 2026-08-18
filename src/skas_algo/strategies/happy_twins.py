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
        # Park idle capital in this symbol (e.g. GOLDBEES) whenever EVERY trading symbol
        # is flat; unparked in full the moment an entry fires (its sale funds the buy in
        # the same slice). None = cash idles, the original behavior. The park symbol must
        # be in the run's symbol list (it needs price data) and is excluded from the
        # twins logic itself. Multi-symbol caveat: parking engages only when ALL trading
        # names are flat, and leftover cash after a partial entry stays cash.
        park_symbol: str | None = None,
        **_ignored,
    ):
        self.universe = universe
        self.initial_capital = float(initial_capital)
        self.fast_period = int(fast_period)
        self.fast_multiplier = float(fast_multiplier)
        self.slow_period = int(slow_period)
        self.slow_multiplier = float(slow_multiplier)
        self.timeframe = str(timeframe)
        self.park_symbol = park_symbol or None
        # the twins trade everything EXCEPT the parking vehicle
        self.trading = [s for s in universe if s != self.park_symbol]
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
        exits: list[Signal] = []
        entries: list[Signal] = []
        present = ctx.present_symbols()
        if not present:
            return []
        held = set(ctx.lot_symbols())
        park = self.park_symbol
        park_held = bool(park and park in held)
        # Cash as it will stand after this slice's sells — sells execute before buys
        # (signal order below), so entries may spend unparked/exit proceeds.
        running_cash = ctx.cash

        for sym in self.trading:
            if sym not in present:
                continue
            fast = ctx.indicator(sym, "st_fast")
            slow = ctx.indicator(sym, "st_slow")

            if sym in held:
                # EXIT: slow ST red — state-based, so a restart can never strand a
                # position that flipped while we were down.
                if slow is not None and slow < 0:
                    exits.append(Signal(symbol=sym, action=SignalAction.EXIT_ALL))
                    running_cash += sum(lot.units for lot in ctx.lots(sym)) * ctx.close(sym)
                    held.discard(sym)
            elif fast is not None:
                prev = self.prev_fast.get(sym)
                if prev is not None and prev < 0 and fast > 0:
                    if park_held and park in present:
                        # UNPARK first — its sale funds the entry in this same slice.
                        exits.insert(0, Signal(symbol=park, action=SignalAction.EXIT_ALL))
                        running_cash += (sum(lot.units for lot in ctx.lots(park))
                                         * ctx.close(park))
                        park_held = False
                    # ENTER on the green FLIP only. Equal slot of current equity;
                    # skip (never shrink) when cash is short.
                    slot = ctx.equity() / max(len(self.trading), 1)
                    close = ctx.close(sym)
                    units = int(slot // close) if close > 0 else 0
                    if units > 0 and running_cash >= units * close:
                        running_cash -= units * close
                        entries.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG,
                                              quantity=units))
                        held.add(sym)
            if fast is not None:
                self.prev_fast[sym] = float(fast)

        # PARK: every trading name flat and money sitting idle → hold gold instead.
        if park and park in present and not park_held and running_cash > 0 \
                and not (held & set(self.trading)):
            close = ctx.close(park)
            units = int(running_cash // close) if close > 0 else 0
            if units > 0:
                entries.append(Signal(symbol=park, action=SignalAction.ENTER_LONG,
                                      quantity=units))
        return exits + entries
