"""Override resolver — the seam where user rules reshape strategy decisions.

A strategy proposes a default action (e.g. "exit this whole lot"). The resolver
checks the active overrides for that lot/symbol/algo and may reshape it — for
example "book 50% now, trail the remaining 50% with a 2% stop". This single seam
serves both pre-trade config rules (source=CONFIG) and mid-session live
intervention (source=LIVE): live intervention just mutates the override list the
running engine reads each slice.

Rule shape (stored in override.rule JSON), e.g.:
    {"exit": [{"at_pct": 6, "action": "book", "qty_pct": 50},
              {"action": "trail_sl", "trail_pct": 2}]}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context import AlgoContext
from .stops import Stop, StopKind
from .types import Signal, SignalAction


@dataclass
class OverrideRule:
    """A user override, decoupled from the DB model so the engine stays standalone."""

    scope: str  # "ALGO" | "SYMBOL" | "POSITION"
    target: str | None  # symbol, or str(lot_id), or None for whole-algo
    rule: dict[str, Any]
    active: bool = True


# --- resolved actions the runner executes ---
@dataclass
class CloseLot:
    symbol: str
    lot_id: int
    units: int
    tag: str = "STRATEGY"


@dataclass
class AttachStop:
    stop: Stop


@dataclass
class BuyLot:
    symbol: str
    units: int


_PRECEDENCE = {"POSITION": 0, "SYMBOL": 1, "ALGO": 2}


class OverrideResolver:
    def __init__(self, overrides: list[OverrideRule] | None = None):
        # A mutable list so live intervention can append/replace rules at runtime.
        self.overrides = overrides if overrides is not None else []

    def _match(self, symbol: str, lot_id: int) -> dict[str, Any] | None:
        """Most specific active rule for this lot: POSITION > SYMBOL > ALGO."""
        candidates = []
        for ov in self.overrides:
            if not ov.active:
                continue
            if ov.scope == "POSITION" and ov.target == str(lot_id):
                candidates.append(ov)
            elif ov.scope == "SYMBOL" and ov.target == symbol:
                candidates.append(ov)
            elif ov.scope == "ALGO":
                candidates.append(ov)
        if not candidates:
            return None
        candidates.sort(key=lambda o: _PRECEDENCE.get(o.scope, 9))
        return candidates[0].rule

    def resolve(self, signals: list[Signal], ctx: AlgoContext) -> list:
        """Turn strategy signals into concrete actions, applying overrides to exits."""
        actions: list = []
        for sig in signals:
            if sig.action is SignalAction.ENTER_LONG:
                actions.append(BuyLot(sig.symbol, sig.quantity or 0))
            elif sig.action is SignalAction.EXIT:
                actions.extend(self._resolve_exit(sig, ctx))
        return actions

    def _resolve_exit(self, sig: Signal, ctx: AlgoContext) -> list:
        lot = ctx.portfolio.get_lot(sig.symbol, sig.lot_id) if sig.lot_id else None
        if lot is None:
            return []
        rule = self._match(sig.symbol, lot.id)
        exit_rules = (rule or {}).get("exit") if rule else None
        if not exit_rules:
            return [CloseLot(sig.symbol, lot.id, lot.units, tag="STRATEGY")]

        book = next((r for r in exit_rules if r.get("action") == "book"), None)
        trail = next((r for r in exit_rules if r.get("action") == "trail_sl"), None)

        # Honour an at_pct gate on the book rule: only reshape once the lot is up enough.
        close = ctx.close(sig.symbol)
        if book and "at_pct" in book:
            pnl_pct = (close - lot.price) / lot.price * 100
            if pnl_pct < book["at_pct"]:
                return [CloseLot(sig.symbol, lot.id, lot.units, tag="STRATEGY")]

        qty_pct = float(book["qty_pct"]) if book and "qty_pct" in book else 100.0
        book_units = int(lot.units * qty_pct / 100.0)
        remaining = lot.units - book_units

        actions: list = []
        if book_units > 0:
            actions.append(CloseLot(sig.symbol, lot.id, book_units, tag="BOOK"))
        if remaining > 0 and trail is not None:
            trail_frac = float(trail.get("trail_pct", 0)) / 100.0
            actions.append(
                AttachStop(
                    Stop(
                        symbol=sig.symbol,
                        lot_id=lot.id,
                        kind=StopKind.TRAILING,
                        trail=trail_frac,
                        hwm=close,
                        reason="override trail_sl",
                    )
                )
            )
        elif book_units == 0:
            # No-op override (e.g. qty_pct=0 with no trail): fall back to full exit.
            actions.append(CloseLot(sig.symbol, lot.id, lot.units, tag="STRATEGY"))
        return actions
