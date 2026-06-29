"""Donchian Strangle Monthly — basket short-strangle screener (analysis layer).

Per name we SELL a call at the strike nearest last month's Donchian **high** and a put at the
strike nearest the **low**; a leg whose premium is below a floor (% of spot) is skipped, so the
name runs single-leg. The whole basket is tail-hedged at the portfolio level with long OTM NIFTY
options sized to total notional, and governed by a combined (stock legs + hedge) 2%-of-notional stop.

This module is the *analysis* layer only — pure functions that, given a name's daily OHLC, its live
option chain and its Sensibull row (ATMIV/IVP/Event), build one screener row, plus the portfolio-panel
math (notional, notional-matched hedge, SL/target). The route (api/routes/trade.py) fetches the data
and wires these together; deployment registers the ``donchian_strangle_monthly`` strategy with the
resolved legs. There is **no backtest path** — the screener deploys to PAPER (forward-test) or LIVE only.

Geometry (spec §7), with the Donchian window = [range_start, range_end] (the previous monthly cycle):
  • range_high = max(daily High), range_low = min(daily Low)
  • CE_strike ≈ nearest listed strike to range_high   (round-out → up);   SELL CE
  • PE_strike ≈ nearest listed strike to range_low     (round-out → down); SELL PE
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from skas_algo.engine.options.contract_specs import expected_monthly_expiry
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.engine.options.realized_vol import realized_vol_series
from skas_algo.services.fibret import spread_pct  # reuse the bid-ask liquidity gauge


@dataclass
class DonchianParams:
    ivp_min: float = 50.0                  # Sensibull IVPercentile floor
    require_iv_gt_hv: bool = True          # keep only ATMIV > annualised HV
    hv_window: int = 20                    # trading days, close-to-close
    skip_leg_min_premium_pct: float = 0.5  # leg premium ÷ spot (%) — below → skip the leg
    round_out: bool = False                # nearest (default) vs round-out (more cushion, less premium)
    hedge_otm_pct: float = 4.5             # each side of the NIFTY hedge
    hedge_beta_weight: bool = False        # weight hedge lots by each name's beta vs NIFTY
    hedge_cost_cap_pct: float = 25.0       # soft flag only (% of premium collected)
    portfolio_sl_pct: float = 2.0          # of aggregate notional (combined incl. hedge)
    portfolio_target_enabled: bool = False
    portfolio_target_pct: float = 50.0     # unit = % of PREMIUM COLLECTED (the capturable lever)
    lots_per_name: int = 1
    breach_basis: str = "close"            # "close" (EOD) | "touch" (intraday)
    breach_buffer_pct: float = 0.5         # spot must clear the strike by this % to count as a breach
    max_flips: int = 2                     # per name (Phase 2)


# ───────────────────────────────────────────────────────────── per-name math

def annualized_hv(closes, window: int) -> float | None:
    """Annualised close-to-close realized vol as a PERCENT (comparable to Sensibull ATMIV)."""
    s = pd.Series([float(c) for c in closes]).dropna()
    if len(s) <= 2:
        return None
    rv = realized_vol_series(s, window=window)
    return float(rv.iloc[-1]) * 100.0 if len(rv) else None


def donchian_range(df: pd.DataFrame, start: date, end: date) -> tuple[float, float] | None:
    """(range_high, range_low) over [start, end] inclusive; None if no bars in the window."""
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.date
    w = d[(d["date"] >= start) & (d["date"] <= end)]
    if w.empty:
        return None
    return float(w["high"].max()), float(w["low"].min())


def strike_step(strikes: list[float]) -> float | None:
    """The listed strike step = smallest positive gap between consecutive strikes (used to place
    the ATM strike on a breach flip)."""
    s = sorted(set(strikes))
    diffs = [round(b - a, 4) for a, b in zip(s, s[1:]) if b > a]
    return min(diffs) if diffs else None


def beta_from_frames(name_df, nifty_df, window: int = 60) -> float | None:
    """Beta of a name vs NIFTY over the last ``window`` shared daily closes (cov/var of log returns).
    None when either series is missing or too short."""
    import numpy as np

    if name_df is None or nifty_df is None or len(name_df) == 0 or len(nifty_df) == 0:
        return None
    a = name_df[["date", "close"]].copy()
    b = nifty_df[["date", "close"]].copy()
    a["date"] = pd.to_datetime(a["date"])
    b["date"] = pd.to_datetime(b["date"])
    m = a.merge(b, on="date", suffixes=("_n", "_x")).tail(window + 1)
    if len(m) < 12:
        return None
    rn = np.diff(np.log(m["close_n"].to_numpy()))
    rx = np.diff(np.log(m["close_x"].to_numpy()))
    cov = np.cov(rn, rx)  # 2×2 sample covariance — use its own var(rx) so ddof matches
    var = float(cov[1][1])
    if var <= 0:
        return None
    return float(cov[0][1] / var)


def pick_strike(strikes: list[float], level: float, side: str, round_out: bool) -> float | None:
    """Nearest listed strike to ``level`` (default), or round-out (CE up / PE down) for more cushion."""
    if not strikes:
        return None
    if not round_out:
        return min(strikes, key=lambda s: abs(s - level))
    if side == "CE":  # round out = above the high
        out = [s for s in strikes if s >= level]
        return min(out) if out else max(strikes)
    out = [s for s in strikes if s <= level]  # PE round out = below the low
    return max(out) if out else min(strikes)


def _leg(rows: list[dict], strike: float | None, right: str) -> dict | None:
    """Premium/bid/ask/oi/spread/liquidity for one resolved leg, or None if not listed/priced."""
    if strike is None:
        return None
    row = next((r for r in rows if float(r["strike"]) == float(strike)), None)
    side = (row.get("ce") if right == "CE" else row.get("pe")) if row else None
    if not side:
        return None
    bid, ask = side.get("bid"), side.get("ask")
    spr = spread_pct(bid, ask)
    mid = (bid + ask) / 2 if (bid and ask and ask >= bid) else None
    # Premium you'd actually COLLECT selling this leg = the bid — NOT the last-traded price, which on
    # an illiquid strike can be a stale/erroneous print (e.g. a 4%-OTM call showing ₹900). Fall back
    # to the mid, then ltp/close, when the book is one-sided.
    premium = bid or mid or side.get("ltp") or side.get("close")
    return {
        "strike": float(strike), "premium": premium, "bid": bid, "ask": ask,
        "oi": int(side.get("oi") or 0), "spread_pct": spr,
        "liquid": spr is not None and spr <= 10.0,
    }


def _event_in_window(event: str | None, entry_date: date | None, sell_expiry: date | None) -> bool:
    """True if a Sensibull Event date falls in [entry_date, sell_expiry] (the holding cycle)."""
    if not event or event.strip() in ("", "-"):
        return False
    try:
        ev = date.fromisoformat(event.strip()[:10])
    except ValueError:
        return False
    lo = entry_date or date.min
    hi = sell_expiry or date.max
    return lo <= ev <= hi


def analyze_name(
    *,
    symbol: str,
    df: pd.DataFrame,
    chain: dict,
    sell_expiry: date,
    range_start: date,
    range_end: date,
    entry_date: date | None,
    atm_iv: float | None,
    ivp: float | None,
    event: str | None,
    params: DonchianParams,
) -> dict:
    """One screener row. Never raises — returns ``{"symbol", "status": "error", "error": ...}``
    when the range/chain can't be resolved. ``status`` ∈ {strangle, CE-only, PE-only,
    excluded:event, excluded:filter, error}."""
    spot = chain.get("spot")
    lot_size = int(chain.get("lot_size") or 0)
    rows = chain.get("rows") or []
    hv = annualized_hv(df["close"].tolist(), params.hv_window) if df is not None and len(df) else None
    base = {"symbol": symbol, "spot": spot, "ivp": ivp, "atm_iv": atm_iv, "hv": hv,
            "event": (event or None), "lot_size": lot_size, "lots": params.lots_per_name}

    # Filters first (spec §6): event window, then IV>HV and IVP floor.
    if _event_in_window(event, entry_date, sell_expiry):
        return {**base, "status": "excluded:event", "error": None}
    if ivp is not None and ivp < params.ivp_min:
        return {**base, "status": "excluded:filter", "reason": f"IVP {ivp:.0f} < {params.ivp_min:.0f}", "error": None}
    if params.require_iv_gt_hv and atm_iv is not None and hv is not None and not (atm_iv > hv):
        return {**base, "status": "excluded:filter", "reason": f"ATMIV {atm_iv:.1f} ≤ HV {hv:.1f}", "error": None}

    rng = donchian_range(df, range_start, range_end)
    if rng is None or not spot or lot_size <= 0 or not rows:
        return {**base, "status": "error", "error": "no Donchian range / live chain / lot size"}
    range_high, range_low = rng
    strikes = [float(r["strike"]) for r in rows]

    ce = _leg(rows, pick_strike(strikes, range_high, "CE", params.round_out), "CE")
    pe = _leg(rows, pick_strike(strikes, range_low, "PE", params.round_out), "PE")

    def keep(leg: dict | None) -> bool:
        if not leg or leg["premium"] is None or spot <= 0:
            return False
        return (leg["premium"] / spot * 100.0) >= params.skip_leg_min_premium_pct

    ce_ok, pe_ok = keep(ce), keep(pe)
    if ce and not ce_ok:
        ce["skip"] = True
    if pe and not pe_ok:
        pe["skip"] = True

    if ce_ok and pe_ok:
        status = "strangle"
    elif ce_ok:
        status = "CE-only"
    elif pe_ok:
        status = "PE-only"
    else:
        return {**base, "range_high": range_high, "range_low": range_low, "ce": ce, "pe": pe,
                "status": "excluded:filter", "reason": "both legs below premium floor", "error": None}

    units = lot_size * params.lots_per_name
    margin = 0.0
    if ce_ok:
        margin += short_option_margin(spot, units, 1, MarginParams())
    if pe_ok:
        margin += short_option_margin(spot, units, 1, MarginParams())

    return {**base, "expiry": sell_expiry.isoformat(), "range_high": range_high, "range_low": range_low,
            "ce": ce, "pe": pe, "margin": margin, "strike_step": strike_step(strikes),
            "status": status, "error": None}


# ───────────────────────────────────────────────────────── date anchors (§5)

def _next_trading_day(d: date | None, trading_days: list[date] | None = None) -> date | None:
    """The next actual trading day after ``d`` (from the index calendar when given; else the next
    weekday)."""
    if d is None:
        return None
    if trading_days:
        nxt = [t for t in trading_days if t > d]
        if nxt:
            return nxt[0]
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:  # Sat/Sun
        nd += timedelta(days=1)
    return nd


def _snap_back(d: date | None, trading_days: list[date] | None) -> date | None:
    """Roll a calendar anchor back to the latest actual trading day on/before it (holiday-adjusted)."""
    if d is None or not trading_days:
        return d
    prior = [t for t in trading_days if t <= d]
    return prior[-1] if prior else d


def resolve_cycle(
    today: date,
    listed_expiries: list[date],
    *,
    underlying: str = "NIFTY",
    trading_days: list[date] | set[date] | None = None,
    range_start: date | None = None,
    range_end: date | None = None,
    entry_date: date | None = None,
    sell_expiry: date | None = None,
) -> dict:
    """Resolve the monthly cycle anchors (spec §5). The sell expiry comes from the broker's
    actual listed (future) expiries — the primary, holiday-correct source; the range window
    (prev/last monthly) uses the calendar ``expected_monthly_expiry``, snapped back to the actual
    index trading calendar (holiday-adjusted). Any anchor the caller passes (the UI override) wins."""
    tds = sorted(trading_days) if trading_days else None
    future = sorted(e for e in listed_expiries if e >= today)
    sell = sell_expiry or (future[0] if future else None)

    anchors: list[date] = []
    y, m = today.year, today.month
    for _ in range(4):
        a = expected_monthly_expiry(underlying, y, m)
        if a:
            anchors.append(a)
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    past = sorted({a for a in anchors if a <= today})
    auto_last = _snap_back(past[-1] if past else None, tds)
    auto_prev = _snap_back(past[-2] if len(past) >= 2 else None, tds)
    last = range_end or auto_last
    prev = range_start or auto_prev
    # A stale/invalid override (or a clamp to a short trading calendar) must NOT invert the window:
    # an inverted range start≥end yields an empty Donchian lookup → every screener row "error".
    # Fall back to the auto-resolved anchors.
    if prev and last and prev >= last:
        prev, last = auto_prev, auto_last
    entry = entry_date or _next_trading_day(last, tds)
    return {"prev_expiry": prev, "last_expiry": last, "sell_expiry": sell, "entry_date": entry,
            "range_start": prev, "range_end": last}


# ───────────────────────────────────────────────────────── portfolio layer (§8)

def _short_legs(row: dict) -> list[dict]:
    """The non-skipped short legs of a screener row (0–2 of CE/PE)."""
    out = []
    for leg in (row.get("ce"), row.get("pe")):
        if leg and leg.get("premium") is not None and not leg.get("skip"):
            out.append(leg)
    return out


def portfolio_panel(
    selected: list[dict],
    *,
    nifty_spot: float | None,
    nifty_lot_size: int,
    nifty_chain: dict | None,
    params: DonchianParams,
    basket_margin: float | None = None,
) -> dict:
    """Aggregate notional, premium collected, the notional-matched NIFTY hedge, and the
    portfolio stop/target levels for the selected names (spec §8)."""
    agg_notional = sum((r.get("spot") or 0) * (r.get("lot_size") or 0) * (r.get("lots") or 1) for r in selected)
    premium_collected = sum(
        leg["premium"] * (r.get("lot_size") or 0) * (r.get("lots") or 1)
        for r in selected for leg in _short_legs(r)
    )
    # Hedge sizing notional: notional-match, or beta-weighted (Σ notional·beta) when enabled.
    hedge_notional = sum(
        (r.get("spot") or 0) * (r.get("lot_size") or 0) * (r.get("lots") or 1)
        * ((r.get("beta") if (params.hedge_beta_weight and r.get("beta") is not None) else 1.0))
        for r in selected
    )

    hedge: dict = {"nifty_lots": 0}
    if nifty_spot and nifty_lot_size > 0 and hedge_notional > 0:
        nifty_lots = round(hedge_notional / (nifty_spot * nifty_lot_size))
        ce_target = nifty_spot * (1 + params.hedge_otm_pct / 100.0)
        pe_target = nifty_spot * (1 - params.hedge_otm_pct / 100.0)
        rows = (nifty_chain or {}).get("rows") or []
        strikes = [float(r["strike"]) for r in rows]
        ce_strike = pick_strike(strikes, ce_target, "CE", round_out=True)
        pe_strike = pick_strike(strikes, pe_target, "PE", round_out=True)
        ce = _leg(rows, ce_strike, "CE") or {}
        pe = _leg(rows, pe_strike, "PE") or {}
        ce_prem = ce.get("premium") or 0.0
        pe_prem = pe.get("premium") or 0.0
        cost = (ce_prem + pe_prem) * nifty_lot_size * nifty_lots
        cost_pct = (cost / premium_collected * 100.0) if premium_collected > 0 else None
        hedge = {
            "nifty_lots": int(nifty_lots), "nifty_lot_size": nifty_lot_size,
            "ce_strike": ce_strike, "pe_strike": pe_strike,
            "ce_premium": ce_prem or None, "pe_premium": pe_prem or None,
            "cost": cost, "cost_pct_of_premium": cost_pct,
            "cap_flag": cost_pct is not None and cost_pct > params.hedge_cost_cap_pct,
        }

    sl_amount = params.portfolio_sl_pct / 100.0 * agg_notional
    target_amount = (params.portfolio_target_pct / 100.0 * premium_collected
                     if params.portfolio_target_enabled else None)
    return {
        "selected_count": len(selected),
        "agg_notional": agg_notional,
        "premium_collected": premium_collected,
        "premium_pct_of_notional": (premium_collected / agg_notional * 100.0) if agg_notional > 0 else None,
        "hedge": hedge,
        "portfolio_sl_amount": sl_amount,
        "portfolio_target_amount": target_amount,
        "basket_margin": basket_margin,
    }
