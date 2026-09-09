"""A SPAN-shaped margin estimate for a book of index options.

The platform's `short_option_margin` is span+exposure on EACH short leg with no offset, so
a 1-lot short straddle read ₹4.1L and a hedged spread ₹1.9L — several times what a broker
blocks, and in the direction that makes every hedged structure look unaffordable (owner:
"margin is wrong", 2026-09-09). This mirrors the SHAPE of NSE's requirement instead:

  margin = SPAN-like scenario loss + exposure margin

* **Scenario loss** — the book's worst loss over a price scan of ±`scan_pct` around spot
  (1% steps) crossed with a vol scan of ±`vol_scan` on each leg's IV, every leg repriced
  with Black–Scholes at its own time to expiry. Hedges offset here, as they do at the
  exchange: a vertical's worst case is its width, a straddle's is one wing's move.
* **Exposure margin** — `exposure_pct` of notional on every SHORT unit, NO offset. This is
  the part a spread cannot hedge away, and it is why Kite asks ₹3.6L for a bear call
  spread whose max loss is ₹49k: 2% × 22,905 × 650 = ₹2.98L of it is exposure.

Calibration point (the only broker figure the design carries): the handoff's bear call
spread, 24000/24100 CE ×10 lots, Kite basket ₹3,63,826 → this reads ₹3.4L. A naked ATM
short comes out ~₹0.9–1.1L per lot and a 1-lot straddle ~₹1.3L, both in line with what
Kite shows in 2026. It is STILL an estimate — `margin_source` stays "model" and the
manual anchor still outranks it — but it is one whose errors are tens of percent, not
multiples, and which gets the ORDER of structures right (spread < straddle < naked).
"""

from __future__ import annotations

from dataclasses import dataclass

from skas_algo.engine.options import black_scholes as bs

SCAN_PCT = 6.0        # price scan, ± percent of spot
VOL_SCAN = 0.25       # vol scan, ± fraction of each leg's IV
EXPOSURE_PCT = 2.0    # exposure margin, % of notional per SHORT unit (index options)
T_FLOOR = 1.0 / 365   # a 0-DTE leg still scans as one day out, not as pure intrinsic


@dataclass(frozen=True)
class MarginLeg:
    right: str        # CE / PE
    strike: float
    direction: int    # +1 long, −1 short
    units: float
    iv: float         # decimal, e.g. 0.14; ≤0 → priced at intrinsic
    t: float          # years to expiry


def _value(leg: MarginLeg, spot: float, iv_mult: float, r: float) -> float:
    iv = leg.iv * iv_mult
    t = max(leg.t, T_FLOOR)
    if iv <= 0:
        return bs.intrinsic(leg.right, spot, leg.strike)
    return bs.price(spot, leg.strike, t, r, iv, leg.right)


def span_like(legs: list[MarginLeg], spot: float, *, r: float = 0.065,
              scan_pct: float = SCAN_PCT, vol_scan: float = VOL_SCAN,
              exposure_pct: float = EXPOSURE_PCT) -> dict:
    """``{"span", "exposure", "total", "worst_move_pct"}`` for the book at ``spot``."""
    # A long-only book is paid for up front: no margin is blocked against it. Longs only
    # matter here as HEDGES inside the scan.
    if not legs or spot <= 0 or not any(leg.direction < 0 for leg in legs):
        return {"span": 0.0, "exposure": 0.0, "total": 0.0, "worst_move_pct": 0.0}
    base = {id(leg): _value(leg, spot, 1.0, r) for leg in legs}
    worst, worst_move = 0.0, 0.0
    steps = int(round(scan_pct))
    for k in range(-steps, steps + 1):
        s = spot * (1 + k / 100.0)
        for vm in (1.0 - vol_scan, 1.0, 1.0 + vol_scan):
            pnl = sum(leg.direction * (_value(leg, s, vm, r) - base[id(leg)]) * leg.units
                      for leg in legs)
            if -pnl > worst:
                worst, worst_move = -pnl, k
    exposure = sum(exposure_pct / 100.0 * spot * leg.units for leg in legs if leg.direction < 0)
    return {"span": round(worst, 2), "exposure": round(exposure, 2),
            "total": round(worst + exposure, 2), "worst_move_pct": worst_move}
