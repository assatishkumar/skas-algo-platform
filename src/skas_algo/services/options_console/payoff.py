"""Expiry payoff metrics for a console book, server-side — the Simulator's decision
snapshot needs max P/L, breakevens and POP at the moment an action lands, without a browser
in the loop. Same construction as `web/src/lib/payoff.ts::computeMetrics` (intrinsic at the
nearest expiry, unlimited tails from the net call units, POP as risk-neutral lognormal mass
over the profit segments); a calendar's far legs are priced at intrinsic too — a rougher
tent than the page's, said so in `basis`."""

from __future__ import annotations

import math

from skas_algo.engine.options import black_scholes as bs


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def metrics(legs: list[dict], spot: float, *, offset: float = 0.0, sigma: float | None = None,
            t: float | None = None, r: float = 0.065) -> dict | None:
    """``legs``: [{right, strike, direction, units, entry}] (enabled legs only).
    Returns max_profit / max_loss (None = unlimited), breakevens, the nearest breakeven's
    distance from spot in %, POP (0..1) when ``sigma`` and ``t`` are given."""
    if not legs or not spot or spot <= 0:
        return None

    def pnl(s: float) -> float:
        return offset + sum(leg["direction"] * (bs.intrinsic(leg["right"], s, leg["strike"])
                                                - leg["entry"]) * leg["units"] for leg in legs)

    lo, hi = spot * 0.7, spot * 1.3
    n = 1200
    xs = [lo + (hi - lo) * i / n for i in range(n + 1)]
    ys = [pnl(x) for x in xs]
    ce_net = sum(leg["direction"] * leg["units"] for leg in legs if leg["right"] == "CE")
    unlimited_up = ce_net > 1e-9
    unlimited_down = ce_net < -1e-9
    bes: list[float] = []
    for i in range(n):
        a, b = ys[i], ys[i + 1]
        if (a <= 0 < b) or (a > 0 >= b):
            frac = a / (a - b) if a != b else 0.0
            bes.append(round(xs[i] + (xs[i + 1] - xs[i]) * frac, 2))
    max_profit = None if unlimited_up else round(max(ys), 2)
    max_loss = None if unlimited_down else round(min(ys), 2)
    near = min(bes, key=lambda k: abs(k - spot)) if bes else None
    pop = None
    if sigma and sigma > 0 and t and t > 0:
        bounds = [0.0, *sorted(bes), float("inf")]
        mass = 0.0
        for i in range(len(bounds) - 1):
            a, b = bounds[i], bounds[i + 1]
            mid = (a + b) / 2 if b != float("inf") else a * 1.05 + 1
            if pnl(mid) <= 0:
                continue
            def cdf(k: float) -> float:
                if k <= 0:
                    return 0.0
                if k == float("inf"):
                    return 1.0
                return _norm_cdf((math.log(k / spot) - (r - 0.5 * sigma * sigma) * t)
                                 / (sigma * math.sqrt(t)))
            mass += cdf(b) - cdf(a)
        pop = round(max(0.0, min(1.0, mass)), 4)
    return {"max_profit": max_profit, "max_loss": max_loss, "breakevens": bes,
            "nearest_be": near,
            "be_dist_pct": round(100.0 * (near - spot) / spot, 2) if near else None,
            "pop": pop, "basis": "intrinsic at expiry"}
