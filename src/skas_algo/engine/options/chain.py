"""Option-chain view a strategy queries to pick expiries and strikes.

Backed by two callables (supplied by the platform's options wiring):
  * ``chain_provider(underlying, on_date) -> DataFrame`` — the day's chain rows
    (skas-data ``get_option_chain``), columns incl. expiry_date/strike_price/option_type/close/...
  * ``spot_provider(underlying, on_date) -> float | None`` — underlying spot
    (cached ``NIFTY 50`` / ``NIFTY BANK`` index close).

Results are cached per ``(underlying, on_date)`` so a slice doesn't re-hit the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from .instrument import make

ChainProvider = Callable[[str, date], "object"]   # -> pandas DataFrame
SpotProvider = Callable[[str, date], "float | None"]


@dataclass(frozen=True)
class ChainRow:
    underlying: str
    expiry: date
    strike: float
    right: str
    close: float
    settle: float
    oi: int
    symbol: str  # engine-encoded contract symbol


class OptionChainView:
    def __init__(self, chain_provider: ChainProvider, spot_provider: SpotProvider,
                 lot_overrides: dict | None = None):
        self._chain = chain_provider
        self._spot = spot_provider
        self._lot_overrides = lot_overrides
        self._cache: dict[tuple[str, date], object] = {}

    def _df(self, underlying: str, on_date: date):
        key = (underlying.upper(), on_date)
        if key not in self._cache:
            self._cache[key] = self._chain(underlying.upper(), on_date)
        return self._cache[key]

    def spot(self, underlying: str, on_date: date) -> float | None:
        return self._spot(underlying.upper(), on_date)

    def expiries(self, underlying: str, on_date: date) -> list[date]:
        df = self._df(underlying, on_date)
        if df is None or len(df) == 0:
            return []
        vals = sorted({_as_date(x) for x in df["expiry_date"].tolist()})
        return vals

    def nearest_expiry(self, underlying: str, on_date: date, min_dte: int = 0) -> date | None:
        """The soonest expiry at least ``min_dte`` calendar days out (else None)."""
        cands = [e for e in self.expiries(underlying, on_date) if (e - on_date).days >= min_dte]
        return cands[0] if cands else None

    def expiry_for_dte(self, underlying: str, on_date: date, dte_target: int) -> date | None:
        """The available expiry whose DTE is closest to ``dte_target`` (ties → sooner)."""
        exps = self.expiries(underlying, on_date)
        if not exps:
            return None
        return min(exps, key=lambda e: (abs((e - on_date).days - dte_target), (e - on_date).days))

    def chain(self, underlying: str, on_date: date, expiry: date) -> list[ChainRow]:
        df = self._df(underlying, on_date)
        if df is None or len(df) == 0:
            return []
        rows: list[ChainRow] = []
        for _, r in df.iterrows():
            if _as_date(r["expiry_date"]) != expiry:
                continue
            strike = float(r["strike_price"])
            right = str(r["option_type"]).upper()
            inst = make(underlying.upper(), expiry, strike, right, lot_overrides=self._lot_overrides)
            rows.append(ChainRow(
                underlying=underlying.upper(), expiry=expiry, strike=strike, right=right,
                close=float(r["close"]) if r["close"] is not None else float("nan"),
                settle=float(r.get("settle_price") or r["close"] or 0.0),
                oi=int(r.get("open_interest") or 0), symbol=inst.symbol,
            ))
        return rows

    def strikes(self, underlying: str, on_date: date, expiry: date) -> list[float]:
        return sorted({row.strike for row in self.chain(underlying, on_date, expiry)})

    def atm_strike(self, underlying: str, on_date: date, expiry: date,
                   spot: float | None = None) -> float | None:
        """Listed strike nearest the underlying spot for the given expiry."""
        ks = self.strikes(underlying, on_date, expiry)
        if not ks:
            return None
        if spot is None:
            spot = self.spot(underlying, on_date)
        if spot is None:
            return None
        return min(ks, key=lambda k: abs(k - spot))


def _as_date(x) -> date:
    """Coerce a pandas Timestamp / datetime / date to a plain date."""
    if isinstance(x, date) and not hasattr(x, "hour"):
        return x
    return x.date() if hasattr(x, "date") else date.fromisoformat(str(x)[:10])
