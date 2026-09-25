"""BIDS (Buy In Dips) — the one ladder rule, pure arithmetic (owner design 2026-09-25).

A holding is bought every time it falls another ``dip_pct`` below its RECENT HIGH: level 1 at
X% down buys ``amount``, level 2 at 2X% buys 2×amount, level k at k·X% buys k×amount (a LINEAR
ladder, the owner's "y, 2y, 3y"), up to ``max_levels``. A close at or above the peak is a new
high: the peak moves up and the ladder RESETS. The peak never moves down, and buying does not
touch it — the levels are measured from the high, never from the last fill.

No I/O here: the suggestion engine (services/bids.py, every market-linked holding) and the
auto strategy (strategies/bids.py, broker-held stocks/ETFs) call the SAME function, so a dip
means the same thing on the Portfolio tab and in a live run.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LadderRule:
    dip_pct: float           # X — each level is X% further below the peak
    amount: float            # y — level k invests k × y rupees
    max_levels: int          # N — no level beyond N until the next reset

    def trigger_price(self, peak: float, level: int) -> float:
        return peak * (1.0 - level * self.dip_pct / 100.0)

    def level_amount(self, level: int) -> float:
        return level * self.amount


@dataclass
class LadderState:
    peak: float | None = None
    levels_fired: int = 0


@dataclass(frozen=True)
class Trigger:
    level: int
    trigger_price: float
    amount: float


@dataclass
class Outcome:
    state: LadderState
    triggers: list[Trigger] = field(default_factory=list)
    reset: bool = False      # a new high reset the ladder this evaluation


def evaluate(state: LadderState, close: float, rule: LadderRule) -> Outcome:
    """One close against the ladder. A gap that crosses several levels fires ALL of them in
    one evaluation (y + 2y + …), because each level is a separate promise the rule made.

    A bad rule (non-positive X, y or N) or a non-positive close fires nothing and leaves the
    state as it was — a broken input must never turn into a buy."""
    if close is None or close <= 0:
        return Outcome(LadderState(state.peak, state.levels_fired))
    if rule.dip_pct <= 0 or rule.amount <= 0 or rule.max_levels <= 0:
        return Outcome(LadderState(state.peak, state.levels_fired))
    if state.peak is None or close >= state.peak:
        reset = state.peak is not None and state.levels_fired > 0
        return Outcome(LadderState(float(close), 0), reset=reset)
    triggers: list[Trigger] = []
    fired = state.levels_fired
    for level in range(state.levels_fired + 1, rule.max_levels + 1):
        px = rule.trigger_price(state.peak, level)
        if close <= px:
            triggers.append(Trigger(level, round(px, 4), rule.level_amount(level)))
            fired = level
        else:
            break
    return Outcome(LadderState(state.peak, fired), triggers)


def next_trigger(state: LadderState, rule: LadderRule) -> Trigger | None:
    """The level the ladder is waiting for, or None once the cap is reached."""
    if state.peak is None or state.levels_fired >= rule.max_levels:
        return None
    level = state.levels_fired + 1
    return Trigger(level, round(rule.trigger_price(state.peak, level), 4),
                   rule.level_amount(level))
