"""Prebuilt structures, defined RELATIVE TO AN ANCHOR so they apply at any spot and expiry.

A preset is not a list of strikes; it is a rule — "short call at Δ0.25, long call one grid
step above" — that the session RESOLVES against the chain at the cursor. That is what lets
the same eight cards work on NIFTY at 22,900 and BANKNIFTY at 52,000, and it is why the
resolution happens server-side: the Δ it needs is the one `chain_rows` already solved, so
the card, the ladder and the payoff cannot disagree about which strike is "the 25-delta".

Anchors: ``atm`` (the chain's ATM row), ``delta`` (the OTM strike whose |Δ| is nearest a
target), ``step`` (N grid steps from another leg — the design's "+100 pts" on NIFTY's
100-strike ladder), ``premium`` (an iron fly's wings at ATM ± the straddle's combined
premium, snapped to the grid). A preset that cannot be resolved — an unquoted wing, a Δ the
ladder does not reach — is reported with a reason rather than filled in with a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LegRule:
    right: str                       # CE / PE
    side: str                        # B / S
    anchor: str                      # atm | delta | step | premium
    lots: int = 1
    delta: float = 0.0               # anchor=delta: target |Δ|
    ref: int = -1                    # anchor=step/premium: index of the leg it hangs off
    steps: int = 0                   # anchor=step: grid steps AWAY from spot (+ = further OTM)


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    rule: str                        # the headline, in words
    legs: tuple[LegRule, ...]
    defined: bool                    # defined risk by construction
    tags: tuple[str, ...] = field(default_factory=tuple)


PRESETS: tuple[Preset, ...] = (
    Preset("short_straddle", "Short straddle", "Sell ATM call + put",
           (LegRule("CE", "S", "atm"), LegRule("PE", "S", "atm")), defined=False,
           tags=("neutral", "theta")),
    Preset("short_strangle", "Short strangle", "Sell Δ0.25 call + Δ0.25 put",
           (LegRule("CE", "S", "delta", delta=0.25), LegRule("PE", "S", "delta", delta=0.25)),
           defined=False, tags=("neutral", "theta")),
    Preset("iron_condor", "Iron condor", "Δ0.20 strangle · wings 2 steps out",
           (LegRule("CE", "S", "delta", delta=0.20), LegRule("PE", "S", "delta", delta=0.20),
            LegRule("CE", "B", "step", ref=0, steps=2), LegRule("PE", "B", "step", ref=1, steps=2)),
           defined=True, tags=("neutral", "defined")),
    Preset("iron_fly", "Iron fly", "ATM straddle · wings at ± combined premium",
           (LegRule("CE", "S", "atm"), LegRule("PE", "S", "atm"),
            LegRule("CE", "B", "premium", ref=0), LegRule("PE", "B", "premium", ref=1)),
           defined=True, tags=("neutral", "defined")),
    Preset("bear_call_spread", "Bear call spread", "Short call Δ0.25 · long call +1 step",
           (LegRule("CE", "S", "delta", delta=0.25), LegRule("CE", "B", "step", ref=0, steps=1)),
           defined=True, tags=("bearish", "defined")),
    Preset("bull_put_spread", "Bull put spread", "Short put Δ0.25 · long put −1 step",
           (LegRule("PE", "S", "delta", delta=0.25), LegRule("PE", "B", "step", ref=0, steps=1)),
           defined=True, tags=("bullish", "defined")),
    Preset("long_straddle", "Long straddle", "Buy ATM call + put",
           (LegRule("CE", "B", "atm"), LegRule("PE", "B", "atm")), defined=True,
           tags=("vol", "defined")),
    Preset("call_ratio", "Call ratio 1:2", "Buy ATM call · sell 2× Δ0.25 call",
           (LegRule("CE", "B", "atm"), LegRule("CE", "S", "delta", delta=0.25, lots=2)),
           defined=False, tags=("mild bullish", "ratio")),
)

BY_ID = {p.id: p for p in PRESETS}


def resolve(preset: Preset, rows: list[dict], atm: float | None, grid: float,
            lots: int = 1) -> dict:
    """Turn a preset's rules into concrete legs against ``rows`` (the session's
    ``chain_rows()``). Returns ``{legs: [...], ok: bool, reason: str | None}`` — a leg
    carries right/side/strike/lots/ltp; ``ok`` False names the FIRST rule that could not
    be honoured, with no strike invented for it."""
    if atm is None or not rows:
        return {"legs": [], "ok": False, "reason": "no chain at the cursor"}
    by_k = {float(r["strike"]): r for r in rows}

    def quoted(k: float, right: str) -> dict | None:
        r = by_k.get(float(k))
        cell = r and r[right.lower()]
        return cell if cell and cell.get("quoted") and cell.get("ltp") else None

    def nearest_delta(right: str, target: float) -> float | None:
        best, best_d = None, 9.0
        for r in rows:
            k = float(r["strike"])
            # OTM side only: a 25-delta call is above the money by definition
            if (right == "CE" and k < atm) or (right == "PE" and k > atm):
                continue
            cell = r[right.lower()]
            if not cell.get("quoted") or cell.get("delta") is None:
                continue
            d = abs(abs(float(cell["delta"])) - target)
            if d < best_d:
                best, best_d = k, d
        return best

    legs: list[dict] = []
    for i, rule in enumerate(preset.legs):
        k: float | None
        if rule.anchor == "atm":
            k = atm
        elif rule.anchor == "delta":
            k = nearest_delta(rule.right, rule.delta)
            if k is None:
                return {"legs": legs, "ok": False,
                        "reason": f"no quoted Δ{rule.delta:.2f} {rule.right} on the ladder"}
        elif rule.anchor == "step":
            base = legs[rule.ref]["strike"]
            k = base + (grid * rule.steps if rule.right == "CE" else -grid * rule.steps)
        elif rule.anchor == "premium":
            ce = next((x for x in legs if x["right"] == "CE" and x["strike"] == atm), None)
            pe = next((x for x in legs if x["right"] == "PE" and x["strike"] == atm), None)
            if not (ce and pe):
                return {"legs": legs, "ok": False, "reason": "no ATM straddle to hang wings on"}
            width = max(grid, round((ce["ltp"] + pe["ltp"]) / grid) * grid)
            k = atm + width if rule.right == "CE" else atm - width
        else:
            return {"legs": legs, "ok": False, "reason": f"unknown anchor {rule.anchor!r}"}
        cell = quoted(k, rule.right)
        if cell is None:
            return {"legs": legs, "ok": False,
                    "reason": f"{int(k)} {rule.right} has not traded at the cursor"}
        legs.append({"right": rule.right, "side": rule.side, "strike": float(k),
                     "lots": rule.lots * max(1, int(lots)), "ltp": float(cell["ltp"]),
                     "delta": cell.get("delta"), "i": i})
    return {"legs": legs, "ok": True, "reason": None}
