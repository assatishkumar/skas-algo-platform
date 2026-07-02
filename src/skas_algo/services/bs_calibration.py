"""BS-vs-market calibration — how far off is Black-Scholes-with-realized-HV, today?

The synthetic donchian backtest prices stock options as BS(spot, K, t, r, HV×mult).
Realized HV systematically understates traded implied vol (the vol-risk premium), so
model premiums run cheap. This module quantifies TODAY's gap against the live chain at
the strikes the strategy actually trades (the screener's Donchian CE/PE picks + the ATM
pair per name) and suggests the ``vol_multiplier`` that would reprice the market:
``median(implied_vol / HV)`` across the basket.

Pure math over pre-fetched inputs (cached bars + a live chain dict) — the route
(api/routes/research.py) does the fetching. Nothing here can place an order.
"""

from __future__ import annotations

from datetime import date
from statistics import median, quantiles

from skas_algo.engine.options import black_scholes as bs
from skas_algo.services.donchian_strangle import annualized_hv, donchian_range, pick_strike

# Moneyness buckets for the aggregate table: |strike−spot|/spot.
_BUCKETS: list[tuple[str, float, float]] = [
    ("±1%", 0.0, 1.0), ("1–3%", 1.0, 3.0), ("3–6%", 3.0, 6.0), (">6%", 6.0, 1e9),
]


def _chain_side(rows: list[dict], strike: float, right: str) -> dict | None:
    row = next((r for r in rows if float(r["strike"]) == float(strike)), None)
    return (row.get("ce") if right == "CE" else row.get("pe")) if row else None


def calibrate_name(*, symbol: str, df, chain: dict, sell_expiry: date, today: date,
                   range_start: date, range_end: date, hv_window: int = 20,
                   r: float = 0.065, round_out: bool = False) -> list[dict]:
    """Comparison rows for one name: screener CE@high / PE@low + ATM CE/PE. Empty when
    the chain/spot/HV can't be resolved (the route reports those as per-name errors)."""
    spot = chain.get("spot")
    rows = chain.get("rows") or []
    hv_pct = annualized_hv(df["close"].tolist(), hv_window) if df is not None and len(df) else None
    t = max((sell_expiry - today).days, 0) / 365.0
    if not spot or not rows or hv_pct is None or t <= 0:
        return []
    sigma = hv_pct / 100.0
    strikes = [float(x["strike"]) for x in rows]
    targets: list[tuple[float | None, str, str]] = []
    rng = donchian_range(df, range_start, range_end)
    if rng is not None:
        range_high, range_low = rng
        targets.append((pick_strike(strikes, range_high, "CE", round_out), "CE", "screener"))
        targets.append((pick_strike(strikes, range_low, "PE", round_out), "PE", "screener"))
    atm = pick_strike(strikes, spot, "CE", False)
    targets.extend([(atm, "CE", "atm"), (atm, "PE", "atm")])

    out: list[dict] = []
    seen: set[tuple[float, str]] = set()
    for strike, right, kind in targets:
        if strike is None or (strike, right) in seen:
            continue
        seen.add((strike, right))
        side = _chain_side(rows, strike, right)
        if not side:
            continue
        bid, ask = side.get("bid"), side.get("ask")
        mid = (bid + ask) / 2 if (bid and ask and ask >= bid) else None
        market = mid or side.get("ltp") or side.get("close")
        if not market or market <= 0:
            continue
        bs_price = bs.price(spot, strike, t, r, sigma, right)
        market_iv = bs.implied_vol(market, spot, strike, t, r, right)
        out.append({
            "symbol": symbol, "spot": spot, "hv_pct": round(hv_pct, 1),
            "strike": float(strike), "right": right, "kind": kind,
            "moneyness_pct": round((strike - spot) / spot * 100.0, 2),
            "market_bid": bid, "market_mid": mid, "market": market,
            "bs_price": round(bs_price, 2),
            "ratio": round(bs_price / market, 3),
            "market_iv_pct": round(market_iv * 100.0, 1) if market_iv else None,
            "iv_over_hv": round(market_iv / sigma, 3) if (market_iv and sigma > 0) else None,
        })
    return out


def _stats(values: list[float]) -> dict | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    q = quantiles(vals, n=4) if len(vals) >= 4 else [vals[0], median(vals), vals[-1]]
    return {"n": len(vals), "median": round(median(vals), 3),
            "q1": round(q[0], 3), "q3": round(q[-1], 3)}


def aggregate(rows: list[dict]) -> dict:
    """Basket-level view: overall / per-right / per-moneyness-bucket stats of the
    BS/market price ratio and the IV/HV ratio, plus the suggested vol_multiplier."""
    ivhv = [r["iv_over_hv"] for r in rows if r["iv_over_hv"] is not None]
    by_bucket = []
    for label, lo, hi in _BUCKETS:
        sub = [r for r in rows if lo <= abs(r["moneyness_pct"]) < hi]
        by_bucket.append({
            "bucket": label,
            "ratio": _stats([r["ratio"] for r in sub]),
            "iv_over_hv": _stats([r["iv_over_hv"] for r in sub if r["iv_over_hv"]]),
        })
    return {
        "rows": len(rows),
        "ratio": _stats([r["ratio"] for r in rows]),
        "iv_over_hv": _stats(ivhv),
        "by_right": {
            right: _stats([r["iv_over_hv"] for r in rows
                           if r["right"] == right and r["iv_over_hv"]])
            for right in ("CE", "PE")
        },
        "by_moneyness": by_bucket,
        # The knob the backtest form asks for: sigma×this reprices today's market (median).
        "suggested_vol_multiplier": round(median(ivhv), 2) if ivhv else None,
    }
