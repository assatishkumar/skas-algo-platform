"""The HANDOVER rule (owner, 2026-09-10): a manual order that leaves a run holding
positions pauses the strategy and installs the manual rail. Every option family, not just
the one the old generic rebuild happened to fit. See CLAUDE.md §1 and
docs/PLAN-options-console.md Part 2."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from skas_algo.data.options_provider import build_live_options_run
from skas_algo.engine.live import LiveSession
from skas_algo.engine.options.charges import ChargeModel
from skas_algo.strategies.manual_book import ManualBookStrategy
from tests.test_live_options import FakeLiveSD, _biz

TS = datetime(2026, 1, 5, 10, 30)
EXP = "2026-01-13"
CE, PE, WING = f"NIFTY|{EXP}|25000|CE", f"NIFTY|{EXP}|25000|PE", f"NIFTY|{EXP}|25400|CE"


def _families():
    """One strategy per leg MODEL the old rebuild met: dict-with-right (delta family),
    list-of-strings (custom_options), dict-without-right (ratio family), no legs at all
    (strangle combo). Each is seeded as if it had opened the straddle + a wing itself."""
    from skas_algo.strategies.call_ratio_monthly import CallRatioMonthlyStrategy
    from skas_algo.strategies.custom_options import CustomOptionsStrategy
    from skas_algo.strategies.delta_neutral_monthly import DeltaNeutralMonthlyStrategy
    from skas_algo.strategies.intraday_strangle_combo import IntradayStrangleComboStrategy
    from skas_algo.strategies.volcano_calendar import VolcanoCalendarStrategy

    delta = DeltaNeutralMonthlyStrategy(universe=["NIFTY"], initial_capital=1_000_000,
                                        stop_loss_pct=2.0)
    delta.legs = [{"symbol": CE, "right": "CE", "dir": -1, "units": 65, "entry": 100.0},
                  {"symbol": PE, "right": "PE", "dir": -1, "units": 65, "entry": 100.0},
                  {"symbol": WING, "right": "CE", "dir": 1, "units": 65, "entry": 20.0}]
    delta.phase = "ironfly"
    volcano = VolcanoCalendarStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    volcano.legs = [dict(leg) for leg in delta.legs]
    custom = CustomOptionsStrategy(universe=["NIFTY"], initial_capital=1_000_000, expiry=EXP,
                                   legs=[{"right": "CE", "strike": 25000, "side": "sell",
                                          "lots": 1}])
    custom.entered = True
    custom.legs = [CE, PE, WING]
    custom.entry_close = {CE: 100.0, PE: 100.0, WING: 20.0}
    custom.units = {CE: 65.0, PE: 65.0, WING: 65.0}
    custom.leg_side = {CE: "sell", PE: "sell", WING: "buy"}
    custom.leg_index = {CE: 0, PE: 1, WING: 2}
    ratio = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    ratio.legs = [{"symbol": CE, "dir": -1, "units": 65, "entry": 100.0},
                  {"symbol": PE, "dir": -1, "units": 65, "entry": 100.0},
                  {"symbol": WING, "dir": 1, "units": 65, "entry": 20.0}]
    combo = IntradayStrangleComboStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    return [("delta_neutral_monthly", delta), ("volcano_calendar", volcano),
            ("custom_options", custom), ("call_ratio_monthly", ratio),
            ("intraday_strangle_combo", combo)]


def _session(strategy, now=TS):
    sd = FakeLiveSD(_biz(date(2026, 1, 1), date(2026, 1, 20)))
    mv, _chain, settler, margin = build_live_options_run(sd, "NIFTY", now=now)
    sess = LiveSession(strategy, initial_capital=1_000_000, market_view=mv, settler=settler,
                       margin_model=margin, charge_model=ChargeModel())
    sess.portfolio.sell_to_open(CE, 65, 100.0, now)
    sess.portfolio.sell_to_open(PE, 65, 100.0, now)
    sess.portfolio.buy(WING, 65, 20.0, now)
    sess.update_quotes({CE: 100.0, PE: 100.0, WING: 20.0})
    return sess


@pytest.mark.parametrize("sid,strategy", _families(), ids=[f[0] for f in _families()])
def test_every_family_hands_over_and_the_next_slice_raises_nothing(sid, strategy):
    sess = _session(strategy)
    before = strategy.export_state() if hasattr(strategy, "export_state") else None
    events = sess.manual_order(TS, closes=[{"symbol": WING}])
    assert events and WING not in sess.portfolio.lot_symbols()
    # the strategy is PAUSED, whole — never told about the leg it lost
    assert sess.managed_by == "manual" and sess.paused_strategy is strategy
    assert sess.strategy.strategy_id == "manual_book"
    if before is not None:
        assert strategy.export_state() == before
    assert sess.handover["reason"] == "manual_order" and sess.handover["strategy_id"] == sid
    # the slice that used to raise KeyError/TypeError on the delta family / custom_options
    # runs the rail instead — no exception, no exits (nothing breached)
    assert sess.run_decision(datetime(2026, 1, 5, 10, 31)) == []
    assert {leg["symbol"] for leg in sess.strategy.legs} == {CE, PE}
    snap = sess.snapshot()
    assert snap["managed_by"] == "manual" and snap["rail"]["paused_strategy_id"] == sid
    assert snap["exit_rules"][0].startswith("Manual mode")


def test_a_manual_flatten_keeps_the_strategy_installed():
    sid, strategy = _families()[0]
    sess = _session(strategy)
    sess.flatten(TS)
    assert not sess.portfolio.lot_symbols()
    assert sess.managed_by == "strategy" and sess.strategy is strategy
    assert strategy.legs == []                      # the flat book is adopted as before


def test_the_rail_inherits_a_margin_stop_and_an_intraday_exit_but_never_invents_one():
    from skas_algo.strategies.intraday_straddle import IntradayStraddleStrategy

    fams = dict(_families())
    delta = fams["delta_neutral_monthly"]
    delta.margin_base, delta.margin_source = 150_000.0, "broker"
    rail = ManualBookStrategy.from_paused(delta, underlying="NIFTY", ts=TS, reason="manual_order")
    assert rail.stop_pct == 2.0 and rail.margin_base == 150_000.0 and rail.margin_source == "broker"
    assert rail.time_exit is None and getattr(rail, "strategy_alert", None) is None
    assert rail.exit_amounts() == (None, 3000.0)
    intraday = IntradayStraddleStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    rail = ManualBookStrategy.from_paused(intraday, underlying="NIFTY", ts=TS, reason="x")
    assert rail.time_exit is not None and rail.time_exit.strftime("%H:%M") == "15:25"
    # the ratio family's stop is a fraction of CAPITAL, not margin: NOT inherited → NO STOP
    rail = ManualBookStrategy.from_paused(fams["call_ratio_monthly"], underlying="NIFTY",
                                          ts=TS, reason="x")
    assert rail.stop_pct == 0 and getattr(rail, "strategy_alert", None) is None  # optional, no alarm
    assert rail.rail_status()["no_stop"] is True
    assert any("optional" in r for r in rail.exit_rules())


def test_the_rail_stop_closes_every_lot_including_a_manual_one():
    sid, strategy = _families()[0]
    sess = _session(strategy)
    sess.manual_order(TS, closes=[{"symbol": WING}],
                      opens=[{"right": "PE", "strike": 24600, "lots": 1, "side": "buy",
                              "expiry": EXP}])
    manual = f"NIFTY|{EXP}|24600|PE"
    assert sess.portfolio.lots(manual)[0].tag == "MANUAL"
    rail = sess.strategy
    rail.update(stop_pct=2.0, margin_anchor=100_000)
    assert rail.margin_source == "manual" and rail.exit_amounts() == (None, 2000.0)
    # shorts marked against by ₹40 a unit → −₹5,200 on 130 units: through the −₹2,000 stop
    sess.update_quotes({CE: 140.0, PE: 140.0, manual: 10.0})
    events = sess.run_decision(datetime(2026, 1, 5, 10, 40))
    assert {e["ticker"] for e in events} == {CE, PE, manual}
    assert all(e["exit_reason"] == "rail_stop" for e in events)
    assert not sess.portfolio.lot_symbols()
    assert sess.managed_by == "manual"                # flat, but still the owner's book


def test_the_rail_holds_before_the_open_settles_and_squares_off_at_its_time():
    fams = dict(_families())
    sess = _session(fams["delta_neutral_monthly"])
    sess.manual_order(TS, closes=[{"symbol": WING}])
    rail = sess.strategy
    rail.update(stop_pct=1.0, margin_anchor=100_000, time_exit="15:20")
    sess.update_quotes({CE: 140.0, PE: 140.0})
    assert sess.run_decision(datetime(2026, 1, 6, 9, 17)) == []      # 09:20 rule holds
    sess.update_quotes({CE: 100.0, PE: 100.0})
    events = sess.run_decision(datetime(2026, 1, 6, 15, 21))
    assert events and all(e["exit_reason"] == "rail_time_exit" for e in events)


def test_a_handover_survives_a_restart():
    fams = dict(_families())
    delta = fams["delta_neutral_monthly"]
    sess = _session(delta)
    sess.manual_order(TS, closes=[{"symbol": WING}])
    sess.strategy.update(stop_pct=3.0, margin_anchor=120_000)
    state = sess.export_state()
    assert state["managed_by"] == "manual" and state["paused_strategy"] == delta.export_state()
    # recovery builds the ORIGINAL strategy from params_snapshot, then load_state
    from skas_algo.strategies.delta_neutral_monthly import DeltaNeutralMonthlyStrategy

    fresh = DeltaNeutralMonthlyStrategy(universe=["NIFTY"], initial_capital=1_000_000,
                                        stop_loss_pct=2.0)
    sess2 = _session(fresh)
    sess2.portfolio._lots.clear()
    sess2.load_state(state)
    assert sess2.managed_by == "manual" and sess2.paused_strategy is fresh
    assert fresh.export_state() == delta.export_state()
    assert sess2.strategy.strategy_id == "manual_book"
    assert sess2.strategy.rail_status()["stop_pct"] == 3.0
    assert sess2.strategy.margin_base == 120_000 and sess2.strategy.margin_source == "manual"
    assert sess2.run_decision(datetime(2026, 1, 5, 10, 35)) == []


def test_resume_is_refused_on_a_held_book_and_reinstalls_the_strategy_when_flat():
    fams = dict(_families())
    delta = fams["delta_neutral_monthly"]
    sess = _session(delta)
    sess.manual_order(TS, closes=[{"symbol": WING}])
    with pytest.raises(ValueError, match="still holds"):
        sess.resume_strategy(TS)
    sess.flatten(datetime(2026, 1, 5, 10, 45))
    assert sess.managed_by == "manual"                # flat by hand: still paused until asked
    sess.resume_strategy(datetime(2026, 1, 5, 10, 46))
    assert sess.managed_by == "strategy" and sess.strategy is delta
    assert sess.paused_strategy is None and sess.handover is None
    assert delta.legs == []                           # the flat book, adopted as before
    with pytest.raises(ValueError, match="not paused"):
        sess.resume_strategy(TS)


def test_a_strategy_exception_halts_visibly(monkeypatch):
    """Until 2026-09-10 the tick loop swallowed this into a log line."""
    from skas_algo.live.manager import LiveConfig, LiveRun, manager

    class _Boom:
        strategy_id = "boom"
        intraday = True
        legs: list = []

        def on_slice(self, ctx):
            raise KeyError("right")

    sess = _session(_Boom())
    cfg = LiveConfig(name="boom", strategy_id="boom", symbols=["NIFTY"], capital=1_000_000,
                     instrument_class="DERIV", underlying="NIFTY")

    class _Q:
        adapter = None

        def refresh(self, *a, **k):
            return {}

    live = LiveRun(9912, 1, cfg, session=sess, quote_source=_Q(), broadcaster=manager.broadcaster)
    monkeypatch.setattr(live, "_persist_state", lambda: None)
    monkeypatch.setattr(live, "_maybe_refresh_margin", lambda: None)
    monkeypatch.setattr(live, "_maybe_refresh_funds", lambda: None)
    monkeypatch.setattr(live, "_maybe_adopt_fund_holding", lambda: None)
    monkeypatch.setattr(live, "_maybe_sync_universe", lambda: None)
    monkeypatch.setattr(live, "_refresh_supertrend", lambda: None)
    live.run_decision(datetime(2026, 1, 5, 10, 31))
    assert live.strategy_error and live.strategy_error.startswith("KeyError")
    assert live.snapshot()["strategy_error"] == live.strategy_error
    assert manager._maybe_self_stop(live) is False   # a halted run never self-stops
