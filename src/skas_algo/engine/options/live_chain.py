"""Live option chain backed by the broker (Kite) for the current trading day.

Coded options strategies pick their expiry/strikes via ``ctx.option_chain()``. In a LIVE deployment
that must reflect TODAY's market — the cached EOD bhavcopy lags (it has no intraday/today data), so a
strategy would find no current expiry and never enter. This view sources today's expiries, strikes,
premiums and spot from the live adapter, and delegates any OTHER date (or any adapter error) to the
cached ``OptionChainView`` — so it degrades gracefully offline and leaves backtests untouched.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime

from .chain import ChainRow, OptionChainView
from .contract_specs import strike_allowed
from .instrument import make


class LiveChainView:
    """Drop-in for OptionChainView that uses the broker chain for `today`, cache otherwise."""

    def __init__(self, cache_view: OptionChainView, adapter, underlying: str,
                 lot_overrides: dict | None = None, ttl: float = 15.0):
        self._cache = cache_view
        self._adapter = adapter
        self._u = underlying.upper()
        self._lot_overrides = lot_overrides
        self._ttl = ttl  # seconds — dedupe repeated broker hits within an entry window
        self._exp_cache: tuple[float, list[date]] | None = None
        self._spot_cache: tuple[float, float | None] | None = None
        self._chain_cache: dict[str, tuple[float, list[ChainRow]]] = {}

    def _is_live(self, underlying: str, on_date: date) -> bool:
        return underlying.upper() == self._u and on_date == datetime.now().date()

    def spot(self, underlying: str, on_date: date):
        if self._is_live(underlying, on_date):
            now = _time.time()
            if self._spot_cache and now - self._spot_cache[0] < self._ttl:
                return self._spot_cache[1]
            try:
                s = self._adapter.underlying_ltp(self._u)
                if s is not None:
                    self._spot_cache = (now, float(s))
                    return float(s)
            except Exception:
                pass
        return self._cache.spot(underlying, on_date)

    def expiries(self, underlying: str, on_date: date) -> list[date]:
        if self._is_live(underlying, on_date):
            now = _time.time()
            if self._exp_cache and now - self._exp_cache[0] < self._ttl:
                return self._exp_cache[1]
            try:
                exps = [date.fromisoformat(e) for e in self._adapter.option_expiries(self._u)]
                if exps:
                    self._exp_cache = (now, exps)
                    return exps
            except Exception:
                pass
        return self._cache.expiries(underlying, on_date)

    def nearest_expiry(self, underlying: str, on_date: date, min_dte: int = 0) -> date | None:
        cands = [e for e in self.expiries(underlying, on_date) if (e - on_date).days >= min_dte]
        return cands[0] if cands else None

    def expiry_for_dte(self, underlying: str, on_date: date, dte_target: int) -> date | None:
        exps = self.expiries(underlying, on_date)
        if not exps:
            return None
        return min(exps, key=lambda e: (abs((e - on_date).days - dte_target), (e - on_date).days))

    def chain(self, underlying: str, on_date: date, expiry: date) -> list[ChainRow]:
        if self._is_live(underlying, on_date):
            key = expiry.isoformat()
            now = _time.time()
            hit = self._chain_cache.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]
            try:
                rows = self._build_live_chain(expiry)
                if rows:
                    self._chain_cache[key] = (now, rows)
                    return rows
            except Exception:
                pass
        return self._cache.chain(underlying, on_date, expiry)

    def _build_live_chain(self, expiry: date) -> list[ChainRow]:
        data = self._adapter.live_option_chain(self._u, expiry.isoformat())
        if not data:
            return []
        lot = int(data.get("lot_size") or 0) or None  # pass to make() so stock F&O sizes too
        out: list[ChainRow] = []
        for r in data.get("rows", []):
            strike = float(r["strike"])
            for right, leg in (("CE", r.get("ce")), ("PE", r.get("pe"))):
                if not leg:
                    continue
                close = leg.get("ltp")
                close = close if close is not None else leg.get("close")
                if close is None:
                    continue
                sym = make(self._u, expiry, strike, right, lot_size=lot,
                           lot_overrides=self._lot_overrides).symbol
                out.append(ChainRow(self._u, expiry, strike, right, float(close), float(close),
                                    int(leg.get("oi") or 0), sym))
        # Coarsen NIFTY candidates to 100-multiples (owner rule) at this live choke point too, so
        # LIVE selection matches the cached/backtest path (parity). No-op for other underlyings;
        # falls back to the full set if the rule would empty the chain.
        allowed = [row for row in out if strike_allowed(self._u, row.strike)]
        return allowed or out

    def strikes(self, underlying: str, on_date: date, expiry: date) -> list[float]:
        return sorted({row.strike for row in self.chain(underlying, on_date, expiry)})

    def atm_strike(self, underlying: str, on_date: date, expiry: date, spot: float | None = None):
        ks = self.strikes(underlying, on_date, expiry)
        if not ks:
            return None
        if spot is None:
            spot = self.spot(underlying, on_date)
        return None if spot is None else min(ks, key=lambda k: abs(k - spot))
