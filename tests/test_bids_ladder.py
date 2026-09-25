"""BIDS ladder (services/bids_ladder.py): levels at k·X% below the recent high, a linear
y, 2y, 3y ladder, a cap, a gap firing every level it crossed, and a reset on a new high."""

from __future__ import annotations

import pytest

from skas_algo.services.bids_ladder import LadderRule, LadderState, evaluate, next_trigger

RULE = LadderRule(dip_pct=5.0, amount=1000.0, max_levels=3)


def test_first_close_sets_the_peak_and_fires_nothing():
    out = evaluate(LadderState(), 100.0, RULE)
    assert out.state.peak == 100.0 and out.state.levels_fired == 0 and out.triggers == []


def test_levels_fire_once_each_with_a_linear_ladder():
    s = LadderState(100.0, 0)
    out = evaluate(s, 96.0, RULE)
    assert out.triggers == []                                  # -4% is not -5%
    out = evaluate(out.state, 95.0, RULE)
    assert [(t.level, t.amount) for t in out.triggers] == [(1, 1000.0)]
    assert out.triggers[0].trigger_price == pytest.approx(95.0)
    out2 = evaluate(out.state, 94.0, RULE)
    assert out2.triggers == [] and out2.state.levels_fired == 1  # level 1 never fires twice
    out3 = evaluate(out2.state, 89.9, RULE)
    assert [(t.level, t.amount) for t in out3.triggers] == [(2, 2000.0)]


def test_a_gap_fires_every_level_it_crossed_up_to_the_cap():
    out = evaluate(LadderState(100.0, 0), 80.0, RULE)          # -20% in one close
    assert [(t.level, t.amount) for t in out.triggers] == [(1, 1000.0), (2, 2000.0),
                                                            (3, 3000.0)]
    assert out.state.levels_fired == 3
    assert next_trigger(out.state, RULE) is None               # capped until a reset
    assert evaluate(out.state, 50.0, RULE).triggers == []


def test_a_new_high_resets_the_ladder_and_the_peak_never_falls():
    s = evaluate(LadderState(100.0, 0), 94.0, RULE).state      # level 1
    out = evaluate(s, 99.0, RULE)
    assert out.state.peak == 100.0 and out.state.levels_fired == 1   # a bounce is not a reset
    out = evaluate(out.state, 101.0, RULE)
    assert out.state.peak == 101.0 and out.state.levels_fired == 0 and out.reset
    assert next_trigger(out.state, RULE).trigger_price == pytest.approx(95.95)


def test_bad_inputs_never_turn_into_a_buy():
    s = LadderState(100.0, 0)
    for close in (0.0, -5.0, None):
        assert evaluate(s, close, RULE).triggers == []
    for bad in (LadderRule(0, 1000, 3), LadderRule(5, 0, 3), LadderRule(5, 1000, 0)):
        out = evaluate(s, 50.0, bad)
        assert out.triggers == [] and out.state.peak == 100.0
