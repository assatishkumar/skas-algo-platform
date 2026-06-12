"""Black-Scholes pricing and greeks for European, cash-settled index options.

NIFTY/BANKNIFTY options are European and cash-settled, so Black-Scholes (with a
continuous dividend yield ``q``) is the theoretically correct model — there is no
early-exercise error. Per the project decision, **real bhavcopy premiums are the
primary backtest fill source**; this module is used for:

  * greeks on a chain (delta-based strike selection for strangles),
  * backing out implied volatility from a *real* observed premium,
  * a synthetic chain / forward premium estimate when real data is missing.

Pure functions, ``math``-only (no scipy/numpy dependency). ``right`` is "CE" (call)
or "PE" (put). Time ``t`` is in years; ``r``/``q``/``sigma`` are annualized decimals.
``vega``/``rho`` are returned per 1.00 change in vol/rate (divide by 100 for per-1%).
"""

from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _is_call(right: str) -> bool:
    r = right.upper()
    if r in ("CE", "C", "CALL"):
        return True
    if r in ("PE", "P", "PUT"):
        return False
    raise ValueError(f"unknown option right {right!r} (expected CE/PE)")


def intrinsic(right: str, spot: float, strike: float) -> float:
    """Payoff at expiry: max(S-K,0) for a call, max(K-S,0) for a put."""
    return max(spot - strike, 0.0) if _is_call(right) else max(strike - spot, 0.0)


def d1_d2(spot: float, strike: float, t: float, r: float, sigma: float, q: float = 0.0):
    """The Black-Scholes d1, d2 terms (caller must ensure t>0 and sigma>0)."""
    vol_t = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / vol_t
    return d1, d1 - vol_t


def price(spot: float, strike: float, t: float, r: float, sigma: float,
          right: str, q: float = 0.0) -> float:
    """Theoretical option premium. At/After expiry (t<=0) returns intrinsic value."""
    call = _is_call(right)
    if t <= 0 or sigma <= 0:
        return intrinsic(right, spot, strike)
    d1, d2 = d1_d2(spot, strike, t, r, sigma, q)
    disc_s = spot * math.exp(-q * t)
    disc_k = strike * math.exp(-r * t)
    if call:
        return disc_s * _norm_cdf(d1) - disc_k * _norm_cdf(d2)
    return disc_k * _norm_cdf(-d2) - disc_s * _norm_cdf(-d1)


def delta(spot, strike, t, r, sigma, right, q: float = 0.0) -> float:
    if t <= 0 or sigma <= 0:
        # Degenerate: 1/-1 if ITM, 0 if OTM (0.5/-0.5 exactly ATM).
        itr = intrinsic(right, spot, strike)
        if itr > 0:
            return math.exp(-q * t) * (1.0 if _is_call(right) else -1.0)
        return 0.0
    d1, _ = d1_d2(spot, strike, t, r, sigma, q)
    base = math.exp(-q * t)
    return base * _norm_cdf(d1) if _is_call(right) else -base * _norm_cdf(-d1)


def gamma(spot, strike, t, r, sigma, q: float = 0.0) -> float:
    if t <= 0 or sigma <= 0:
        return 0.0
    d1, _ = d1_d2(spot, strike, t, r, sigma, q)
    return math.exp(-q * t) * _norm_pdf(d1) / (spot * sigma * math.sqrt(t))


def vega(spot, strike, t, r, sigma, q: float = 0.0) -> float:
    """Per 1.00 change in vol (i.e. divide by 100 for per-1% / per-vol-point)."""
    if t <= 0 or sigma <= 0:
        return 0.0
    d1, _ = d1_d2(spot, strike, t, r, sigma, q)
    return spot * math.exp(-q * t) * _norm_pdf(d1) * math.sqrt(t)


def theta(spot, strike, t, r, sigma, right, q: float = 0.0) -> float:
    """Per-year theta (divide by 365 for per-calendar-day)."""
    if t <= 0 or sigma <= 0:
        return 0.0
    call = _is_call(right)
    d1, d2 = d1_d2(spot, strike, t, r, sigma, q)
    term1 = -(spot * math.exp(-q * t) * _norm_pdf(d1) * sigma) / (2.0 * math.sqrt(t))
    if call:
        return (term1
                - r * strike * math.exp(-r * t) * _norm_cdf(d2)
                + q * spot * math.exp(-q * t) * _norm_cdf(d1))
    return (term1
            + r * strike * math.exp(-r * t) * _norm_cdf(-d2)
            - q * spot * math.exp(-q * t) * _norm_cdf(-d1))


def rho(spot, strike, t, r, sigma, right, q: float = 0.0) -> float:
    """Per 1.00 change in the rate (divide by 100 for per-1%)."""
    if t <= 0 or sigma <= 0:
        return 0.0
    _, d2 = d1_d2(spot, strike, t, r, sigma, q)
    if _is_call(right):
        return strike * t * math.exp(-r * t) * _norm_cdf(d2)
    return -strike * t * math.exp(-r * t) * _norm_cdf(-d2)


def greeks(spot, strike, t, r, sigma, right, q: float = 0.0) -> dict:
    """All greeks at once (handy for a chain row)."""
    return {
        "delta": delta(spot, strike, t, r, sigma, right, q),
        "gamma": gamma(spot, strike, t, r, sigma, q),
        "vega": vega(spot, strike, t, r, sigma, q),
        "theta": theta(spot, strike, t, r, sigma, right, q),
        "rho": rho(spot, strike, t, r, sigma, right, q),
    }


def black76_price(future: float, strike: float, t: float, r: float, sigma: float,
                  right: str) -> float:
    """Black-76: European option on a FUTURES price (MCX GOLD/GOLDM).

    A futures price already embeds the cost of carry, so the forward must equal the
    futures (no r-drift) — algebraically BS with q=r. Using plain BS here biases
    calls rich / puts cheap by ~F·(e^{rt}−1). At t<=0 returns intrinsic.
    """
    return price(future, strike, t, r, sigma, right, q=r)


def black76_implied_vol(observed_price: float, future: float, strike: float, t: float,
                        r: float, right: str, **kwargs) -> float | None:
    """Implied vol under Black-76 (see ``black76_price``)."""
    return implied_vol(observed_price, future, strike, t, r, right, q=r, **kwargs)


def implied_vol(observed_price: float, spot: float, strike: float, t: float, r: float,
                right: str, q: float = 0.0,
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6,
                max_iter: int = 100) -> float | None:
    """Implied volatility that reprices ``observed_price``.

    Newton-Raphson seeded near ATM vol, with a robust bisection fallback. Returns
    ``None`` when the price is below intrinsic / outside the no-arbitrage band (no
    real IV exists). Inputs are the *real* observed premium (the platform's primary
    source); this is how we attach a vol/greeks to a market price.
    """
    if t <= 0:
        return None
    itr = intrinsic(right, spot, strike)
    # Price must sit within [intrinsic, spot] (call) / [intrinsic, strike] (put) bounds.
    upper = spot * math.exp(-q * t) if _is_call(right) else strike * math.exp(-r * t)
    if observed_price < itr - tol or observed_price > upper + tol:
        return None

    sigma = 0.20  # ATM-ish seed
    for _ in range(max_iter):
        diff = price(spot, strike, t, r, sigma, right, q) - observed_price
        if abs(diff) < tol:
            return sigma
        v = vega(spot, strike, t, r, sigma, q)
        if v < 1e-8:
            break  # vega too small for Newton — fall back to bisection
        sigma -= diff / v
        if sigma <= lo or sigma >= hi:
            break  # left the bracket — fall back to bisection

    # Bisection fallback over [lo, hi].
    a, b = lo, hi
    fa = price(spot, strike, t, r, a, right, q) - observed_price
    for _ in range(max_iter):
        m = 0.5 * (a + b)
        fm = price(spot, strike, t, r, m, right, q) - observed_price
        if abs(fm) < tol:
            return m
        if (fa < 0) == (fm < 0):
            a, fa = m, fm
        else:
            b = m
    return 0.5 * (a + b)
