"""Contract specs (lot sizes) for index option underlyings — data-driven & overridable.

Lot sizes change over time via NSE circulars, so each underlying carries a list of
``(effective_from, lot_size)`` revisions; ``lot_size_for`` picks the one in force on a
date. The seeded values are best-effort current sizes — **historical revision dates
should be confirmed against NSE circulars** for accurate older backtests. A backtest
can override the whole table via ``BacktestRequest.params["contract_specs"]``.
"""

from __future__ import annotations

from datetime import date

# underlying -> sorted list of (effective_from, lot_size). Latest entry <= trade_date wins.
# NOTE: seed = current sizes (confirm historical revisions before relying on old backtests).
_LOT_SIZES: dict[str, list[tuple[date, int]]] = {
    "NIFTY": [(date(2000, 1, 1), 75)],
    "BANKNIFTY": [(date(2000, 1, 1), 35)],
    "FINNIFTY": [(date(2000, 1, 1), 65)],
    "MIDCPNIFTY": [(date(2000, 1, 1), 140)],
    # GOLD (MCX) is synthetic; multiplier used for sizing/margin — NEEDS-CONFIRM vs MCX
    # (GOLD 1kg vs GOLDM 100g). Overridable via params["contract_specs"].
    "GOLD": [(date(2000, 1, 1), 100)],
}


def lot_size_for(underlying: str, on: date, overrides: dict | None = None) -> int:
    """Lot size in force for ``underlying`` on ``on``.

    ``overrides`` (from backtest params) may map ``underlying -> int`` (a flat size)
    or ``underlying -> [[iso_date, size], ...]`` (a revision schedule).
    """
    u = underlying.upper()
    if overrides and u in overrides:
        ov = overrides[u]
        if isinstance(ov, int):
            return ov
        revs = sorted((date.fromisoformat(d) if isinstance(d, str) else d, int(s)) for d, s in ov)
    else:
        revs = _LOT_SIZES.get(u)
        if not revs:
            raise KeyError(f"no lot size known for underlying {underlying!r}")
    size = revs[0][1]
    for eff, s in revs:
        if eff <= on:
            size = s
        else:
            break
    return size


def known_underlyings() -> list[str]:
    return sorted(_LOT_SIZES)
