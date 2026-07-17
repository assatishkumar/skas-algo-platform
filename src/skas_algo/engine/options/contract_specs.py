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
    # BANKNIFTY history (backfilled 2026-07-17 for the 5-year GFD intraday backtests;
    # keyed by EXPIRY date like NIFTY above — revisions apply to new contracts, so the
    # boundary is the first expiry that carried the new lot):
    #   …→2023-06-30: 25   (long-standing)
    #   2023-07-01→: 15    (NSE circular 56233, 2023-03-31 — July-2023 expiry onward)
    #   2024-11-20→: 30    (SEBI ₹15L minimum contract value revision)
    #   2026-01-01→: 35    (Oct/Dec-2025 periodic revision — same boundary convention
    #                       as NIFTY's 65 above; front contracts carried 30 until then)
    "BANKNIFTY": [
        (date(2000, 1, 1), 25),
        (date(2023, 7, 1), 15),
        (date(2024, 11, 20), 30),
        (date(2026, 1, 1), 35),
    ],
    # Others seeded with current sizes — historical revisions NEEDS-CONFIRM vs NSE circulars.
    "FINNIFTY": [(date(2000, 1, 1), 65)],
    "MIDCPNIFTY": [(date(2000, 1, 1), 140)],
    # GOLD (MCX, synthetic) models GOLDM: 100 g quoted ₹/10g → multiplier 10 (verified
    # against live chain Jun-2026; big GOLD 1kg would be 100). Overridable via
    # params["contract_specs"].
    "GOLD": [(date(2000, 1, 1), 10)],
    # SENSEX (BSE, BFO) is LIVE-ONLY on this platform — no BSE history exists anywhere, so
    # the lot table is a flat snapshot (20, verified from the live BFO dump 2026-07-03);
    # earlier revisions (10/15) are deliberately not modeled.
    "SENSEX": [(date(2000, 1, 1), 20)],
}

# Nifty-50 stock F&O lot sizes — snapshot of the Kite NFO instruments dump (2026-07-02),
# used by the synthetic donchian_strangle_bt backtest. FLAT approximation: NSE revises
# stock lots as prices move (contract-value band) and the historical revision schedule is
# NOT modeled here, so an old backtest sizes with TODAY's lot. Overridable per run via
# params["contract_specs"]. LTIM has no F&O listing (absent on purpose).
_STOCK_LOT_SIZES: dict[str, int] = {
    "ADANIENT": 309, "ADANIPORTS": 475, "APOLLOHOSP": 125, "ASIANPAINT": 250,
    "AXISBANK": 625, "BAJAJ-AUTO": 75, "BAJAJFINSV": 300, "BAJFINANCE": 750,
    "BHARTIARTL": 475, "BPCL": 1975, "BRITANNIA": 125, "CIPLA": 425, "COALINDIA": 1350,
    "DIVISLAB": 100, "DRREDDY": 625, "EICHERMOT": 100, "GRASIM": 250, "HCLTECH": 400,
    "HDFCBANK": 650, "HDFCLIFE": 1100, "HEROMOTOCO": 150, "HINDALCO": 700,
    "HINDUNILVR": 300, "ICICIBANK": 700, "INDUSINDBK": 700, "INFY": 400, "ITC": 1725,
    "JSWSTEEL": 675, "KOTAKBANK": 2000, "LT": 175, "M&M": 200, "MARUTI": 50,
    "NESTLEIND": 500, "NTPC": 1500, "ONGC": 2250, "POWERGRID": 1900, "RELIANCE": 500,
    "SBILIFE": 375, "SBIN": 750, "SHRIRAMFIN": 825, "SUNPHARMA": 350, "TATACONSUM": 550,
    "TATASTEEL": 2750, "TCS": 225, "TECHM": 600, "TITAN": 175, "TMPV": 1600,
    "ULTRACEMCO": 50, "WIPRO": 3000,
}
_LOT_SIZES.update({u: [(date(2000, 1, 1), s)] for u, s in _STOCK_LOT_SIZES.items()})


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


# --------------------------------------------------------- strike selection granularity
# The strike step the platform will SELECT for an underlying — which may be COARSER than the
# exchange's true LISTING step. NIFTY lists 50-point strikes, but the owner's rule (2026-07) is that
# automated strategies trade only round 100-multiples for NIFTY (better liquidity / round strikes;
# matches ``ema21_momentum``'s long-standing "50s not allowed"). It is enforced CENTRALLY at the
# chain-candidate choke points (OptionChainView / LiveChainView / LiveOptionsMarketView.live_chain)
# and the arithmetic ATM/wing steps (delta_neutral / momentum_theta), so no automated NIFTY strike
# can be a 50 — in backtest, paper AND live alike (parity). BANKNIFTY/SENSEX already list 100s
# (no-op). The MANUAL Option builder is deliberately NOT bound by this (it uses the data routes, a
# separate path). Table is extensible — add an underlying to coarsen its selection.
_SELECTION_STEP: dict[str, int] = {"NIFTY": 100}


def selection_step(underlying: str, listing_step: int | None = None) -> int | None:
    """Strike granularity to SELECT for ``underlying`` (100 for NIFTY), else ``listing_step``."""
    return _SELECTION_STEP.get(underlying.upper(), listing_step)


def strike_allowed(underlying: str, strike: float) -> bool:
    """True unless ``underlying`` has a selection step and ``strike`` isn't a multiple of it."""
    step = _SELECTION_STEP.get(underlying.upper())
    return step is None or round(strike) % step == 0


def eligible_strikes(underlying: str, strikes) -> list[float]:
    """Filter ``strikes`` to the underlying's selection-step multiples (NIFTY → 100s only).

    Identity for underlyings without a selection step. **Safety net:** if filtering would drop every
    strike (a chain somehow carrying no 100-multiples), returns the original list unchanged so a
    strategy never faces an empty chain and silently stops trading.
    """
    step = _SELECTION_STEP.get(underlying.upper())
    if step is None:
        return list(strikes)
    keep = [s for s in strikes if round(s) % step == 0]
    return keep or list(strikes)


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
    # SENSEX kept its weekly through the 2024-11 cull (BSE's product). Thursday as of
    # 2026-07 (live BFO dump: 07-09/16/23/30 all Thu); earlier weekday churn (Fri→Tue→Thu,
    # 2023-25) is not modeled — the underlying is live-only here, nothing reads history.
    "SENSEX": {
        "weekly": [(date(2000, 1, 1), 3)],
        "monthly": [(date(2000, 1, 1), 3)],
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
