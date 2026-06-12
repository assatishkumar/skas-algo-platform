"""Contract specs (lot sizes) for index option underlyings — data-driven & overridable.

Lot sizes change over time via NSE circulars, so each underlying carries a list of
``(effective_from, lot_size)`` revisions; ``lot_size_for`` picks the one in force on a
date. The seeded values are best-effort current sizes — **historical revision dates
should be confirmed against NSE circulars** for accurate older backtests. A backtest
can override the whole table via ``BacktestRequest.params["contract_specs"]``.
"""

from __future__ import annotations

from datetime import date, timedelta

# underlying -> sorted list of (effective_from, lot_size). Latest entry <= trade_date wins.
# NOTE: seed = current sizes (confirm historical revisions before relying on old backtests).
_LOT_SIZES: dict[str, list[tuple[date, int]]] = {
    # NIFTY lot-size history (SEBI contract-value bands; user-confirmed 2026-06):
    #   …→2024-04-25: 50  (₹5–10L band; NIFTY 12k–22k → ₹6–11L/contract)
    #   2024-04-26→2024-11-19: 25  (NIFTY rallied past the ₹10L band → halved to ~₹5.5L)
    #   2024-11-20→2025-12-30: 75  (SEBI raised the minimum contract value to ₹15L)
    #   2026-01-01→now: 65  (NIFTY past ₹20L/contract → cut back to ~₹15.6–16.25L)
    "NIFTY": [
        (date(2000, 1, 1), 50),
        (date(2024, 4, 26), 25),
        (date(2024, 11, 20), 75),
        (date(2026, 1, 1), 65),
    ],
    # Others seeded with current sizes — historical revisions NEEDS-CONFIRM vs NSE circulars.
    "BANKNIFTY": [(date(2000, 1, 1), 35)],
    "FINNIFTY": [(date(2000, 1, 1), 65)],
    "MIDCPNIFTY": [(date(2000, 1, 1), 140)],
    # GOLD (MCX, synthetic) models GOLDM: 100 g quoted ₹/10g → multiplier 10 (verified
    # against live chain Jun-2026; big GOLD 1kg would be 100). Overridable via
    # params["contract_specs"].
    "GOLD": [(date(2000, 1, 1), 10)],
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


# --------------------------------------------------------------- expiry calendar
# Expiry WEEKDAY history per underlying (Mon=0 … Sun=6; None = product discontinued).
# Monthly expiry = the last such weekday of the month. User-confirmed 2026-06:
#   NIFTY: weekly & monthly on Thursday for years; SEBI moved NSE expiries to TUESDAY
#   from 2025-09-01 (reduce end-of-week volume concentration).
#   BANKNIFTY weekly: Thu → Fri (Jun 2023, briefly) → Wed (Sep 2023) → DISCONTINUED
#   (2024-11-20, along with FINNIFTY/MIDCPNIFTY weeklies — only NIFTY kept a weekly).
_EXPIRY_WEEKDAYS: dict[str, dict[str, list[tuple[date, int | None]]]] = {
    "NIFTY": {
        "weekly": [(date(2000, 1, 1), 3), (date(2025, 9, 1), 1)],
        "monthly": [(date(2000, 1, 1), 3), (date(2025, 9, 1), 1)],
    },
    "BANKNIFTY": {
        "weekly": [(date(2000, 1, 1), 3), (date(2023, 6, 1), 4), (date(2023, 9, 1), 2),
                   (date(2024, 11, 20), None)],
        "monthly": [(date(2000, 1, 1), 3), (date(2025, 9, 1), 1)],
    },
    "FINNIFTY": {
        "weekly": [(date(2000, 1, 1), 1), (date(2024, 11, 20), None)],
        "monthly": [(date(2000, 1, 1), 1), (date(2025, 9, 1), 1)],
    },
    "MIDCPNIFTY": {
        "weekly": [(date(2000, 1, 1), 0), (date(2024, 11, 20), None)],
        "monthly": [(date(2000, 1, 1), 0), (date(2025, 9, 1), 1)],
    },
}


def expiry_weekday_for(underlying: str, on: date, kind: str = "monthly") -> int | None:
    """Expiry weekday (Mon=0…) in force for ``underlying`` on ``on``; None if the
    product (e.g. a discontinued weekly) doesn't exist on that date."""
    revs = _EXPIRY_WEEKDAYS.get(underlying.upper(), {}).get(kind)
    if not revs:
        return None
    day = revs[0][1]
    for eff, d in revs:
        if eff <= on:
            day = d
        else:
            break
    return day


def expected_monthly_expiry(underlying: str, year: int, month: int) -> date | None:
    """The EXPECTED monthly expiry date (last expiry-weekday of the month), per the
    weekday history in force that month. Calendar-based — exchange holidays can shift
    the actual date earlier, so chain-data-driven selection should remain the primary
    source; this is the sanity-check/fallback."""
    import calendar as _cal

    last = date(year, month, _cal.monthrange(year, month)[1])
    wd = expiry_weekday_for(underlying, last, "monthly")
    if wd is None:
        return None
    return last - timedelta(days=(last.weekday() - wd) % 7)
