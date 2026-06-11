"""Option instrument model — encodes a contract as the engine's ``symbol`` string.

An option contract flows through the whole engine (Portfolio lots, BrokerOrder,
MarketView series, trade events) as a single string key, so options need no special
plumbing in those layers. The encoding is ``UNDERLYING|EXPIRY|STRIKE|RIGHT``, e.g.
``"NIFTY|2024-01-25|21000|CE"``.

``parse()`` returns ``None`` for anything that isn't an option symbol (ordinary
tickers like ``"RELIANCE"``) — that is the seam that keeps the equity path untouched:
option-specific behavior is only ever triggered when ``parse()`` succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .contract_specs import lot_size_for

SEP = "|"


@dataclass(frozen=True)
class OptionInstrument:
    underlying: str          # "NIFTY", "BANKNIFTY"
    expiry: date
    strike: float
    right: str               # "CE" | "PE"
    lot_size: int            # contract lot size in force at the expiry/trade date
    multiplier: int = 1      # extra contract multiplier (1 for index options)

    @property
    def symbol(self) -> str:
        return encode(self)


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else str(strike)


def encode(inst: OptionInstrument) -> str:
    return SEP.join([inst.underlying, inst.expiry.isoformat(), _fmt_strike(inst.strike), inst.right])


def make(underlying: str, expiry: date, strike: float, right: str,
         lot_size: int | None = None, multiplier: int = 1,
         lot_overrides: dict | None = None) -> OptionInstrument:
    """Build an OptionInstrument, resolving lot size from the spec table if not given."""
    u = underlying.upper()
    r = right.upper()
    if r not in ("CE", "PE"):
        raise ValueError(f"right must be CE/PE, got {right!r}")
    if lot_size is None:
        lot_size = lot_size_for(u, expiry, overrides=lot_overrides)
    return OptionInstrument(u, expiry, float(strike), r, lot_size, multiplier)


def parse(symbol: str, lot_overrides: dict | None = None) -> OptionInstrument | None:
    """Decode an option symbol string, or return None for a non-option symbol."""
    if not isinstance(symbol, str) or SEP not in symbol:
        return None
    parts = symbol.split(SEP)
    if len(parts) != 4:
        return None
    underlying, exp_s, strike_s, right = parts
    try:
        expiry = date.fromisoformat(exp_s)
        strike = float(strike_s)
    except ValueError:
        return None
    if right.upper() not in ("CE", "PE"):
        return None
    try:
        lot_size = lot_size_for(underlying.upper(), expiry, overrides=lot_overrides)
    except KeyError:
        lot_size = 1  # unknown underlying — still parse, size 1 (caller may override)
    return OptionInstrument(underlying.upper(), expiry, strike, right.upper(), lot_size)


def is_option_symbol(symbol: str) -> bool:
    return parse(symbol) is not None
