"""FibRet — Fibonacci-retracement option-selling screener.

In a high-IVP stock, fade a recent daily swing by SELLING an OTM option at the Fibonacci 1.618
extension, with a spot-based stop at the 0.786 level and a 90%-of-premium profit target.

This module is the *analysis* layer only: pure functions that, given a stock's daily OHLC and a
(live) option chain, compute the suggested short leg + risk metrics for one row of the screener
table. The route (api/routes/trade.py) fetches the data and wires these together; deployment reuses
``custom_options`` (single short leg + spot_upper/spot_lower stop + target_pct=0.9) — same path the
manually-built "INFY_FibRet" run already uses, so there is no new engine strategy.

Geometry (confirmed with the user), with R = swing_high − swing_low:
  • down-leg (low more recent) → SELL CALL: strike ≈ low + 1.618·R (above the high),
    stop if spot ≥ low + 0.786·R.
  • up-leg   (high more recent) → SELL PUT:  strike ≈ high − 1.618·R (below the low),
    stop if spot ≤ high − 0.786·R.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.engine.options.realized_vol import realized_vol_series

R_FREE = 0.065  # annualized risk-free for IV/greeks (matches data routes' DEFAULT_RISK_FREE)


@dataclass
class FibParams:
    swing_lookback: int = 20  # "recent" swing — wider windows push the 1.618 level far OTM/unlisted
    entry_fib: float = 1.618
    stop_fib: float = 0.786
    target_pct: float = 0.90  # fraction (0.90 = book at 90% premium decay)
    lots: int = 1
    min_oi: int = 0


@dataclass
class Swing:
    high: float
    high_date: str
    low: float
    low_date: str
    side: str  # "CE" (down-leg → sell call) | "PE" (up-leg → sell put)

    @property
    def range(self) -> float:
        return self.high - self.low


def _dstr(v) -> str:
    if hasattr(v, "date"):
        v = v.date()
    return v.isoformat() if hasattr(v, "isoformat") else str(v)[:10]


def detect_swing(df: pd.DataFrame, lookback: int, live_price: float | None = None) -> Swing | None:
    """Most recent swing over the last ``lookback`` daily bars: H = highest high, L = lowest low.
    The more-recent extreme sets the leg direction (and thus which side we sell).

    ``live_price`` (the broker's current spot) is treated as the latest, most-recent point so the
    current leg's endpoint is captured even when the daily cache lags by a few days — otherwise a
    fresh down-leg low (e.g. price now 174 but cache ends at 177) is missed."""
    if df is None or len(df) == 0:
        return None
    d = df.tail(lookback).reset_index(drop=True)
    highs = d["high"].astype(float).tolist()
    lows = d["low"].astype(float).tolist()
    dates = [_dstr(v) for v in d["date"].tolist()]
    if live_price is not None and live_price > 0:
        highs.append(float(live_price))
        lows.append(float(live_price))
        dates.append("now")
    hi_pos = max(range(len(highs)), key=lambda i: highs[i])
    lo_pos = min(range(len(lows)), key=lambda i: lows[i])
    high, low = highs[hi_pos], lows[lo_pos]
    if high <= low:
        return None
    side = "CE" if lo_pos > hi_pos else "PE"  # low more recent → down-leg → sell call
    return Swing(high, dates[hi_pos], low, dates[lo_pos], side)


def fib_levels(sw: Swing, entry_fib: float, stop_fib: float) -> tuple[float, float]:
    """(entry strike level, stop spot level) for the swing's side."""
    r = sw.range
    if sw.side == "CE":
        return sw.low + entry_fib * r, sw.low + stop_fib * r
    return sw.high - entry_fib * r, sw.high - stop_fib * r


def _nearest_strike(strikes: list[float], level: float) -> float | None:
    return min(strikes, key=lambda s: abs(s - level)) if strikes else None


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    """Relative bid-ask spread as a % of the mid price (a liquidity gauge). None when either side
    is missing/non-positive (no two-sided market)."""
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100 if mid > 0 else None


def analyze_symbol(
    *,
    symbol: str,
    df: pd.DataFrame,
    chain: dict,
    expiry: date,
    on_date: date,
    params: FibParams,
) -> dict:
    """Build one screener row from a stock's OHLC ``df`` and a live ``chain`` dict
    (``{spot, lot_size, rows:[{strike, ce:{ltp,oi}, pe:{ltp,oi}}]}``). Never raises — returns
    ``{"symbol", "error"}`` when the swing/chain/strike can't be resolved."""
    rows = chain.get("rows") or []
    spot = chain.get("spot")
    lot_size = int(chain.get("lot_size") or 0)

    sw = detect_swing(df, params.swing_lookback, live_price=spot)
    if sw is None:
        return {"symbol": symbol, "error": "no swing in price history"}

    entry_level, stop_level = fib_levels(sw, params.entry_fib, params.stop_fib)
    strikes = [float(r["strike"]) for r in rows]
    strike = _nearest_strike(strikes, entry_level)
    if strike is None or not spot or lot_size <= 0:
        return {"symbol": symbol, "error": "no live chain / lot size", "side": sw.side,
                "swing_high": sw.high, "swing_low": sw.low, "entry_level": entry_level,
                "stop_level": stop_level}

    # The 1.618 level can sit beyond the listed strikes (far-OTM, not traded) — nearest-strike then
    # clamps to the chain edge. Flag it so the UI doesn't present a misleading clamped strike.
    lo_k, hi_k = min(strikes), max(strikes)
    out_of_range = entry_level > hi_k + 1e-9 or entry_level < lo_k - 1e-9
    note = (
        f"1.618 level {entry_level:.0f} is beyond listed strikes ({lo_k:.0f}–{hi_k:.0f}) — "
        "too far OTM to trade; tighten the swing lookback" if out_of_range else None
    )

    row = next(r for r in rows if float(r["strike"]) == strike)
    leg = (row.get("ce") if sw.side == "CE" else row.get("pe")) or {}
    premium = leg.get("ltp") or leg.get("close")
    oi = int(leg.get("oi") or 0)
    bid, ask = leg.get("bid"), leg.get("ask")
    spread = spread_pct(bid, ask)
    # Liquidity by the bid-ask spread (>10% of mid → illiquid); spread None (one-sided) also flags.
    liquid = spread is not None and spread <= 10.0

    dte = max((expiry - on_date).days, 0)
    t = dte / 365.0
    qty = lot_size * params.lots

    iv = bs.implied_vol(premium, spot, strike, t, R_FREE, sw.side) if (premium and t > 0) else None
    if iv and premium:
        val_at_stop = bs.price(stop_level, strike, t, R_FREE, iv, sw.side)
    else:
        val_at_stop = bs.intrinsic(sw.side, stop_level, strike)
    max_profit = (premium or 0.0) * qty
    est_loss = (val_at_stop - (premium or 0.0)) * qty  # short → loss when the option richens
    rr = (max_profit / est_loss) if est_loss > 1e-9 else None
    breakeven = (strike + premium) if sw.side == "CE" else (strike - premium) if premium else None

    closes = df["close"].tail(120).tolist()
    rv = float(realized_vol_series(pd.Series(closes)).iloc[-1]) if len(closes) > 10 else None
    iv_rich = (iv / rv) if (iv and rv) else None

    margin = short_option_margin(spot, qty, 1, MarginParams()) if spot else None

    cushion_strike = (strike - spot) / spot * 100 if sw.side == "CE" else (spot - strike) / spot * 100
    cushion_stop = (stop_level - spot) / spot * 100 if sw.side == "CE" else (spot - stop_level) / spot * 100

    return {
        "symbol": symbol,
        "spot": spot,
        "side": sw.side,
        "swing_high": sw.high,
        "swing_high_date": sw.high_date,
        "swing_low": sw.low,
        "swing_low_date": sw.low_date,
        "entry_level": entry_level,
        "strike": strike,
        "expiry": expiry.isoformat(),
        "dte": dte,
        "premium": premium,
        "oi": oi,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread,
        "liquid": liquid,
        "lot_size": lot_size,
        "lots": params.lots,
        "qty": qty,
        "iv": iv,
        "stop_level": stop_level,
        "est_stop_loss": est_loss if premium else None,
        "max_profit": max_profit if premium else None,
        "reward_risk": rr,
        "breakeven": breakeven,
        "realized_vol": rv,
        "iv_richness": iv_rich,
        "margin": margin,
        "cushion_to_strike_pct": cushion_strike,
        "cushion_to_stop_pct": cushion_stop,
        "out_of_range": out_of_range,
        "note": note,
        "error": None if premium else "no live premium at strike (illiquid?)",
    }
