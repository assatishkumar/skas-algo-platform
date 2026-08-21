"""cycle_detail: reassemble a cycle's flat legs into the entry→adjustments→exit event log
with reconstructed net delta. Synthetic cycle, no network."""

from __future__ import annotations

from skas_algo.services.cycle_detail import build_cycle_detail, reconstruct_cycles


def _leg(strike, right, side, units, entry_dt, entry_px, exit_dt, exit_px, pnl, reason):
    return {"symbol": f"BANKNIFTY|2026-05-26|{strike}|{right}", "underlying": "BANKNIFTY",
            "strike": strike, "right": right, "side": side, "units": units,
            "entry_date": entry_dt, "entry_premium": entry_px, "exit_date": exit_dt,
            "exit_price": exit_px, "exit_reason": reason, "pnl": pnl, "holding_days": 5}


def test_event_log_entry_roll_hedge_exit():
    # A mini delta_neutral cycle: entry (CE+PE) → roll the CE → add a hedge (long) → exit.
    cycle = {
        "underlying": "BANKNIFTY", "expiry": "2026-05-26",
        "entry_date": "2026-04-29 11:00", "exit_date": "2026-05-25 09:15",
        "exit_reason": "target", "net_pnl": 50000.0, "holding_days": 26,
        "underlying_entry": 56000.0, "underlying_exit": 55000.0,
        "vix_entry": 17.0, "vix_exit": 16.0, "underlying_pct": -1.79,
        "daily_pnl": [{"date": "2026-04-29", "pnl": -2000.0}, {"date": "2026-05-10", "pnl": -8000.0},
                      {"date": "2026-05-25", "pnl": 50000.0}],
        "legs_detail": [
            _leg(58000, "CE", "short", 175, "2026-04-29 11:00", 200, "2026-04-30 10:00", 120, 14000, "dnm_roll"),
            _leg(53000, "PE", "short", 175, "2026-04-29 11:00", 210, "2026-05-25 09:15", 90, 21000, "target"),
            _leg(57000, "CE", "short", 175, "2026-04-30 10:00", 380, "2026-05-25 09:15", 100, 49000, "target"),
            _leg(59000, "CE", "long", 175, "2026-04-30 10:00", 60, "2026-05-25 09:15", 3, -9975, "target"),
        ],
    }
    trade_rows = [
        {"date": "2026-04-29 11:00", "ticker": cycle["legs_detail"][0]["symbol"], "tag": "dnm_entry"},
        {"date": "2026-04-29 11:00", "ticker": cycle["legs_detail"][1]["symbol"], "tag": "dnm_entry"},
        {"date": "2026-04-30 10:00", "ticker": cycle["legs_detail"][2]["symbol"], "tag": "dnm_ironfly"},
    ]
    spots = {"2026-04-30": 55500.0}
    model = build_cycle_detail(cycle, trade_rows, lambda d: spots.get(str(d)), [],
                               index=0, run_id=1, strategy_id="delta_neutral_monthly", name="dnm")

    ids = [(e["id"], e["kind"]) for e in model["events"]]
    assert ids == [("E", "entry"), ("R1", "hedge"), ("T", "exit")]   # the roll+hedge share one instant
    entry = model["events"][0]
    assert {o["strike"] for o in entry["opened"]} == {58000, 53000} and not entry["closed"]
    hedge = model["events"][1]
    assert any(o["side"] == "long" for o in hedge["opened"])          # the long hedge marks it
    assert {c["strike"] for c in hedge["closed"]} == {58000}          # the rolled CE closed here
    # net delta reconstructed at entry (an ~ATM-ish short strangle → modest signed value)
    assert entry["net_delta"] is not None
    # KPIs
    assert model["pnl"] == 50000.0 and model["worst_mtm"] == -8000.0
    assert model["n_hedges"] == 1
    assert model["legs"][0]["open_event"] == "E" and model["legs"][0]["close_event"] == "R1"


def test_fixed_structure_has_no_adjustments():
    # A batman-like fixed structure: all legs open at entry, all close at exit → E + T only.
    cycle = {
        "underlying": "NIFTY", "expiry": "2026-02-26",
        "entry_date": "2026-02-02 09:30", "exit_date": "2026-02-20 15:20",
        "exit_reason": "time", "net_pnl": 5000.0, "holding_days": 18,
        "underlying_entry": 24000.0, "underlying_exit": 24100.0, "daily_pnl": [],
        "legs_detail": [
            _leg(24300, "CE", "short", 75, "2026-02-02 09:30", 100, "2026-02-20 15:20", 60, 3000, "time"),
            _leg(23700, "PE", "short", 75, "2026-02-02 09:30", 100, "2026-02-20 15:20", 70, 2000, "time"),
        ],
    }
    model = build_cycle_detail(cycle, [], lambda d: None, [], index=0, run_id=2,
                               strategy_id="batman_ratio_monthly", name="batman")
    assert [e["id"] for e in model["events"]] == ["E", "T"]
    assert model["n_rolls"] == 0 and model["n_hedges"] == 0


def _t(date, action, strike, right, units, price, **kw):
    return {"date": date, "ticker": f"BANKNIFTY|2026-05-26|{strike}|{right}", "action": action,
            "units": units, "price": price, **kw}


def test_reconstruct_cycles_open_and_closed():
    # A closed strangle (entry then both legs covered) + a still-OPEN one on a later expiry.
    trades = [
        _t("2026-04-01 11:00", "SHORT", 57000, "CE", 175, 200, tag="dnm_entry", underlying_spot=55000),
        _t("2026-04-01 11:00", "SHORT", 53000, "PE", 175, 210, tag="dnm_entry", underlying_spot=55000),
        _t("2026-04-20 15:15", "COVER", 57000, "CE", 175, 100, exit_reason="target", underlying_spot=55500),
        _t("2026-04-20 15:15", "COVER", 53000, "PE", 175, 120, exit_reason="target", underlying_spot=55500),
        # a second cycle (different expiry), still open (only entered)
        {"date": "2026-05-02 11:00", "ticker": "BANKNIFTY|2026-06-30|56000|CE", "action": "SHORT",
         "units": 175, "price": 300, "tag": "dnm_entry", "underlying_spot": 55800},
    ]
    cycles = reconstruct_cycles(trades)
    assert len(cycles) == 2
    # newest-first: the open May cycle leads
    assert cycles[0]["entry_date"].startswith("2026-05-02") and cycles[0]["live"] is True
    assert cycles[0]["exit_date"] is None and cycles[0]["legs_detail"][0]["exit_date"] is None
    closed = cycles[1]
    assert closed["live"] is False and closed["exit_reason"] == "target"
    assert len(closed["legs_detail"]) == 2
    # realized = short: (entry−exit)×units for both legs
    assert closed["net_pnl"] == round((200 - 100) * 175 + (210 - 120) * 175, 2)
    # the open cycle build_cycle_detail marks live + has no exit event
    model = build_cycle_detail(cycles[0], trades, lambda d: 55800.0, [], index=0, run_id=9,
                               strategy_id="delta_neutral_monthly", name="dnm")
    assert model["live"] is True
    assert [e["kind"] for e in model["events"]] == ["entry"]   # only opened, nothing closed yet


def test_preview_cycle_detail_endpoint(client, monkeypatch):
    """POST /backtest/cycle-detail rebuilds the lifecycle from an UNSAVED preview's report+trades
    (no run_id) — the report's 'click the entry date ↗' popup works before saving. Stub the
    cache so the test never touches the DuckDB store."""
    import skas_algo.data.options_provider as op
    import skas_algo.data.provider as provider

    monkeypatch.setattr(provider, "get_data_cache", lambda: object())
    monkeypatch.setattr(op, "_ffill_lookup", lambda sd, sym: (lambda d: None))

    cycle = {
        "underlying": "NIFTY", "expiry": "2026-02-26",
        "entry_date": "2026-02-02 09:30", "exit_date": "2026-02-20 15:20",
        "exit_reason": "time", "net_pnl": 5000.0, "holding_days": 18,
        "underlying_entry": 24000.0, "underlying_exit": 24100.0, "daily_pnl": [],
        "legs_detail": [
            _leg(24300, "CE", "short", 75, "2026-02-02 09:30", 100, "2026-02-20 15:20", 60, 3000, "time"),
            _leg(23700, "PE", "short", 75, "2026-02-02 09:30", 100, "2026-02-20 15:20", 70, 2000, "time"),
        ],
    }
    report = {"strategy_id": "batman_ratio_monthly",
              "options": {"cycles": [cycle], "margin_series": []}}
    resp = client.post("/api/v1/backtest/cycle-detail",
                       json={"report": report, "trades": [], "index": 0})
    assert resp.status_code == 200
    m = resp.json()
    assert [e["id"] for e in m["events"]] == ["E", "T"]   # fixed structure → entry + exit only
    assert m["run_id"] == 0 and m["is_deployment"] is False
    assert m["pnl"] == 5000.0

    # index out of range → 404 (not a 500)
    assert client.post("/api/v1/backtest/cycle-detail",
                       json={"report": report, "trades": [], "index": 9}).status_code == 404


def test_mtm_series_extends_to_intraday_exit():
    """A cycle exiting INTRADAY (last EOD mark is the prior day) gets its MTM line connected to
    the exit: the final point is (exit_date, net_pnl) so the strip reaches the 'exit' figure."""
    cycle = {
        "underlying": "NIFTY", "expiry": "2026-02-26",
        "entry_date": "2026-02-02 09:30", "exit_date": "2026-02-20 09:15",
        "exit_reason": "target", "net_pnl": 5000.0, "holding_days": 18,
        "underlying_entry": 24000.0, "underlying_exit": 24100.0,
        "daily_pnl": [{"date": "2026-02-18", "pnl": -1000.0}, {"date": "2026-02-19", "pnl": 2500.0}],
        "legs_detail": [
            _leg(24300, "CE", "short", 75, "2026-02-02 09:30", 100, "2026-02-20 09:15", 60, 3000, "target"),
            _leg(23700, "PE", "short", 75, "2026-02-02 09:30", 100, "2026-02-20 09:15", 70, 2000, "target"),
        ],
    }
    model = build_cycle_detail(cycle, [], lambda d: None, [], index=0, run_id=4,
                               strategy_id="delta_neutral_monthly", name="dnm")
    assert model["mtm_series"][-1] == {"date": "2026-02-20", "value": 5000.0}
    # the EOD marks are untouched before the appended exit point
    assert model["mtm_series"][:-1] == [
        {"date": "2026-02-18", "value": -1000.0}, {"date": "2026-02-19", "value": 2500.0}]


def test_mtm_series_no_duplicate_when_exit_is_an_eod_day():
    """If the last EOD mark already IS the exit day, no duplicate point is appended."""
    cycle = {
        "underlying": "NIFTY", "expiry": "2026-02-26",
        "entry_date": "2026-02-02 09:30", "exit_date": "2026-02-20 15:20",
        "exit_reason": "time", "net_pnl": 5000.0, "holding_days": 18,
        "underlying_entry": 24000.0, "underlying_exit": 24100.0,
        "daily_pnl": [{"date": "2026-02-19", "pnl": 2500.0}, {"date": "2026-02-20", "pnl": 5000.0}],
        "legs_detail": [
            _leg(24300, "CE", "short", 75, "2026-02-02 09:30", 100, "2026-02-20 15:20", 60, 3000, "time"),
            _leg(23700, "PE", "short", 75, "2026-02-02 09:30", 100, "2026-02-20 15:20", 70, 2000, "time"),
        ],
    }
    model = build_cycle_detail(cycle, [], lambda d: None, [], index=0, run_id=5,
                               strategy_id="batman_ratio_monthly", name="b")
    assert len(model["mtm_series"]) == 2
    assert model["mtm_series"][-1] == {"date": "2026-02-20", "value": 5000.0}


def test_add_event_and_exit_reason_and_unrealized(monkeypatch=None):
    """The run-#23x iron-fly cycle complaints (owner, 2026-07-27): an event that OPENS a
    short with NOTHING closed must not narrate a 'premium imbalance roll'; the exit copy
    must name the reason; every event carries the standing book (open_refs) and the
    EOD-marked unrealized P&L."""
    cycle = {
        "underlying": "NIFTY", "expiry": "2026-09-30",
        "entry_date": "2026-08-29 11:00", "exit_date": "2026-09-18 15:20",
        "exit_reason": None, "net_pnl": -11923.0, "holding_days": 21,
        "underlying_entry": 24542.0, "underlying_exit": 25451.0,
        "vix_entry": None, "vix_exit": None, "underlying_pct": 3.7,
        "daily_pnl": [{"date": "2026-08-29", "pnl": -1000.0},
                      {"date": "2026-09-04", "pnl": 4000.0},
                      {"date": "2026-09-17", "pnl": -9000.0}],
        "legs_detail": [
            # the iron fly held E→T…
            _leg(24500, "CE", "short", 225, "2026-08-29 11:00", 409.95,
                 "2026-09-18 15:20", 1023.10, -138085, "stop"),
            _leg(24500, "PE", "short", 225, "2026-08-29 11:00", 239.65,
                 "2026-09-18 15:20", 9.85, 51680, "stop"),
            _leg(25100, "CE", "long", 225, "2026-08-29 11:00", 136.75,
                 "2026-09-18 15:20", 449.85, 70281, "stop"),
            # …plus a naked ADD (nothing closed with it) later rolled once
            _leg(24400, "PE", "short", 225, "2026-09-04 09:35", 78.00,
                 "2026-09-12 10:40", 29.15, 10965, "ifm_adjust_roll"),
            _leg(24700, "PE", "short", 225, "2026-09-12 10:40", 57.90,
                 "2026-09-18 15:20", 25.25, 7320, "stop"),
        ],
    }
    trade_rows = [
        {"date": "2026-09-12 10:40", "ticker": cycle["legs_detail"][3]["symbol"],
         "exit_reason": "ifm_adjust_roll"},
        {"date": "2026-09-18 15:20", "ticker": cycle["legs_detail"][0]["symbol"],
         "exit_reason": "stop"},
    ]
    model = build_cycle_detail(cycle, trade_rows, lambda d: 25000.0, [],
                               index=0, run_id=1, strategy_id="iron_fly_monthly", name="ifm")
    ev = {e["id"]: e for e in model["events"]}
    # R1 = the naked ADD: honest copy + title material (opened, nothing closed)
    assert not ev["R1"]["closed"] and ev["R1"]["opened"]
    assert "NEW short" in ev["R1"]["reason"] and "nothing was closed" in ev["R1"]["reason"]
    # R2 = the adjustment roll, classified from the close row's exit_reason tag
    assert "adjustment roll" in ev["R2"]["reason"]
    # T = exit names the reason (close-row tag wins even with cycle exit_reason unset)
    assert "Stop-loss hit" in ev["T"]["reason"]
    # standing book after R1 = the 3 fly legs + the new short (4 legs, by ref)
    assert len(ev["R1"]["open_refs"]) == 4
    assert len(ev["E"]["open_refs"]) == 3
    # unrealized (EOD mark − realized so far): R1 day EOD mtm=4000, realized 0 → 4000
    assert ev["R1"]["unrealized_eod"] == 4000.0
    # R2: last EOD ≤ 09-12 is 4000, realized so far = 10965 → −6965
    assert ev["R2"]["unrealized_eod"] == 4000.0 - 10965.0
    # flat exit → exactly 0
    assert ev["T"]["unrealized_eod"] == 0.0


def test_cycle_absolute_target_and_stop_tile():
    """Owner ask 2026-07-27: each cycle states its ABSOLUTE ₹ target/SL, anchored to the
    cycle's ENTRY margin (the first margin-series point in its window). Fraction-unit
    families (ratio) convert; the delta family's basis label follows exit_margin_basis."""
    from skas_algo.services.cycle_detail import _threshold_info
    from datetime import date

    series = [{"date": "2026-02-01", "margin": 900_000.0},   # before the cycle — ignored
              {"date": "2026-02-02", "margin": 1_000_000.0},  # entry day → the anchor
              {"date": "2026-02-10", "margin": 2_500_000.0}]  # naked-add jump — NOT the anchor
    d1, d2 = date(2026, 2, 2), date(2026, 2, 20)

    # delta family, whole percents, entry basis
    info = _threshold_info({"profit_target_pct": 2.5, "stop_loss_pct": 1.0,
                            "exit_margin_basis": "entry"},
                           "iron_fly_monthly", series, d1, d2)
    assert info == {"entry_margin": 1_000_000, "target_amount": 25_000,
                    "stop_amount": 10_000, "threshold_basis": "entry"}

    # ratio family: fractions ×100; always entry-frozen by construction
    info = _threshold_info({"profit_target_pct": 0.025, "stop_loss_pct": 0.03},
                           "hni_weekly", series, d1, d2)
    assert info["target_amount"] == 25_000 and info["stop_amount"] == 30_000
    assert info["threshold_basis"] == "entry"

    # delta family without the param → historical re-base, labeled honestly
    info = _threshold_info({"profit_target_pct": 2.5}, "delta_neutral_monthly", series, d1, d2)
    assert info["threshold_basis"] == "current" and info["stop_amount"] is None

    # unknowable → hidden, never guessed
    assert _threshold_info(None, "iron_fly_monthly", series, d1, d2) is None
    assert _threshold_info({"profit_target_pct": 2.5}, "iron_fly_monthly", [], d1, d2) is None


def _cal(date, action, expiry, strike, units, price, **kw):
    return {"date": date, "ticker": f"NIFTY|{expiry}|{strike}|CE", "action": action,
            "units": units, "price": price, **kw}


def test_two_expiry_position_is_ONE_cycle():
    """A calendar holds a near weekly and a far monthly at once. Keyed by expiry (pre-2026-08-20)
    the live page split one fair_value_calendar deploy into a 2-leg cycle and a 1-leg cycle —
    two halves of the same trade, each with a nonsense P&L. Cycles key on the UNDERLYING."""
    trades = [
        _cal("2026-08-20 09:30", "SHORT", "2026-08-25", 23800, 195, 461.05, underlying_spot=24193),
        _cal("2026-08-20 09:30", "SHORT", "2026-08-25", 24200, 195, 130.35, underlying_spot=24193),
        _cal("2026-08-20 09:30", "BUY", "2026-09-29", 24600, 585, 202.45, underlying_spot=24193),
    ]
    cycles = reconstruct_cycles(trades)
    assert len(cycles) == 1
    c = cycles[0]
    assert len(c["legs_detail"]) == 3 and c["live"] is True
    # the cycle settles at its FURTHEST leg; each leg keeps its own expiry (the UI prints it)
    assert c["expiry"] == "2026-09-29"
    assert {lg["expiry"] for lg in c["legs_detail"]} == {"2026-08-25", "2026-09-29"}


def test_two_expiry_cycle_closes_only_when_the_whole_book_is_flat():
    """The sells expire/roll first; the cycle is not over until the far buy is closed too."""
    trades = [
        _cal("2026-08-20 09:30", "SHORT", "2026-08-25", 23800, 195, 461.05, underlying_spot=24193),
        _cal("2026-08-20 09:30", "BUY", "2026-09-29", 24600, 585, 202.45, underlying_spot=24193),
        _cal("2026-08-25 15:00", "COVER", "2026-08-25", 23800, 195, 300.0, exit_reason="fvc_roll"),
        _cal("2026-08-25 15:00", "SHORT", "2026-09-01", 23800, 195, 420.0, exit_reason=None),
        _cal("2026-09-01 15:00", "COVER", "2026-09-01", 23800, 195, 250.0, exit_reason="target"),
        _cal("2026-09-01 15:00", "SELL", "2026-09-29", 24600, 585, 260.0, exit_reason="target"),
    ]
    cycles = reconstruct_cycles(trades)
    assert len(cycles) == 1, [c["entry_date"] for c in cycles]
    c = cycles[0]
    assert c["live"] is False and c["exit_reason"] == "target"
    assert len(c["legs_detail"]) == 3          # two sold episodes + the long
    # the roll's re-sell on a NEW expiry is part of the same cycle, not a new one
    assert {lg["expiry"] for lg in c["legs_detail"]} == {"2026-08-25", "2026-09-01", "2026-09-29"}


def test_event_carries_the_overall_pnl_and_per_leg_expiry():
    """Each event states realized + unrealized AND their sum (owner ask 2026-08-20), and every
    leg row names its own expiry so a calendar's two legs are tellable apart."""
    cycle = {
        "underlying": "NIFTY", "expiry": "2026-09-29",
        "entry_date": "2026-08-20 09:30", "exit_date": None,
        "net_pnl": None, "underlying_entry": 24193.0,
        "daily_pnl": [{"date": "2026-08-20", "pnl": -12000.0}],
        "live": True,
        "legs_detail": [
            {"symbol": "NIFTY|2026-08-25|23800|CE", "underlying": "NIFTY", "strike": 23800,
             "right": "CE", "expiry": "2026-08-25", "side": "short", "units": 195,
             "entry_date": "2026-08-20 09:30", "entry_premium": 461.05, "exit_date": None,
             "exit_price": None, "pnl": 0.0, "holding_days": None},
            {"symbol": "NIFTY|2026-09-29|24600|CE", "underlying": "NIFTY", "strike": 24600,
             "right": "CE", "expiry": "2026-09-29", "side": "long", "units": 585,
             "entry_date": "2026-08-20 09:30", "entry_premium": 202.45, "exit_date": None,
             "exit_price": None, "pnl": 0.0, "holding_days": None},
        ],
    }
    m = build_cycle_detail(cycle, [], lambda d: 24193.0, [], index=0, run_id=3,
                           strategy_id="fair_value_calendar", name="fvc")
    ev = m["events"][0]
    assert ev["realized_so_far"] == 0.0 and ev["unrealized_eod"] == -12000.0
    assert ev["total_so_far"] == -12000.0
    assert {lg["expiry"] for lg in m["legs"]} == {"2026-08-25", "2026-09-29"}
    assert {lg["expiry"] for lg in ev["opened"]} == {"2026-08-25", "2026-09-29"}
    # the near leg is 40 days shorter than the far one → its delta must NOT be priced off the
    # cycle expiry (that was the bug the per-leg expiry fixes)
    near = next(lg for lg in m["legs"] if lg["expiry"] == "2026-08-25")
    far = next(lg for lg in m["legs"] if lg["expiry"] == "2026-09-29")
    assert near["open_delta"] is not None and far["open_delta"] is not None


def _live_leg(strike, right, entry_dt, premium=100.0):
    return {"symbol": f"NIFTY|2026-08-25|{strike}|{right}", "underlying": "NIFTY",
            "strike": strike, "right": right, "expiry": "2026-08-25", "side": "short",
            "units": 75, "entry_date": entry_dt, "entry_premium": premium,
            "exit_date": None, "exit_price": None, "pnl": 0.0, "holding_days": None}


def _live_cycle(legs):
    return {"underlying": "NIFTY", "expiry": "2026-08-25", "entry_date": "2026-08-20 09:30",
            "exit_date": None, "live": True, "net_pnl": None, "underlying_entry": 24000.0,
            "daily_pnl": [], "legs_detail": legs}          # ← daily_pnl empty = the LIVE shape


def test_a_live_cycle_gets_its_unrealized_from_the_store_not_the_daily_series():
    """A LIVE run's cycles are reconstructed from the trade log and carry NO daily_pnl, so the
    unrealized and overall lines were simply absent on every live event card (run #203,
    2026-08-21). The open book is marked from the 1-min store at the event minute instead."""
    import skas_algo.services.cycle_detail as cd

    cycle = _live_cycle([_live_leg(24200, "CE", "2026-08-20 09:30"),
                         _live_leg(24300, "CE", "2026-08-21 10:00", premium=80.0)])
    marks = {"NIFTY|2026-08-25|24200|CE": 60.0, "NIFTY|2026-08-25|24300|CE": 80.0}
    orig, cd._marks_at = cd._marks_at, lambda syms, ts: {s: marks[s] for s in syms if s in marks}
    try:
        m = cd.build_cycle_detail(cycle, [], lambda d: 24000.0, [], index=0, run_id=4,
                                  strategy_id="delta_neutral_monthly", name="dnm")
    finally:
        cd._marks_at = orig
    entry, later = m["events"][0], m["events"][1]
    # the entry: everything just opened → 0 by construction, no lookup
    assert entry["unrealized_eod"] == 0.0 and entry["total_so_far"] == 0.0
    # the later event: the FIRST leg has decayed 40 points, the new one marks at its entry
    assert later["unrealized_eod"] == (60.0 - 100.0) * 75 * -1 == 3000.0
    assert later["unrealized_basis"] == "event"      # the UI labels it "at event", not "EOD"
    assert later["total_so_far"] == 3000.0           # realized 0 + unrealized


def test_an_unpriceable_leg_leaves_the_unrealized_blank_rather_than_partial():
    """All-or-nothing: a partial sum of a two-leg book reads like a real number and is not one."""
    import skas_algo.services.cycle_detail as cd

    cycle = _live_cycle([_live_leg(24200, "CE", "2026-08-20 09:30"),
                         _live_leg(23800, "PE", "2026-08-21 10:00")])
    orig = cd._marks_at
    cd._marks_at = lambda syms, ts: {"NIFTY|2026-08-25|24200|CE": 60.0}   # only ONE prints
    try:
        m = cd.build_cycle_detail(cycle, [], lambda d: 24000.0, [], index=0, run_id=5,
                                  strategy_id="delta_neutral_monthly", name="dnm")
    finally:
        cd._marks_at = orig
    later = m["events"][1]
    assert later["unrealized_eod"] is None and later["total_so_far"] is None
    assert later["unrealized_basis"] is None


def test_a_saved_run_still_uses_its_daily_series():
    """The EOD path is unchanged for saved backtests — the store lookup is a fallback only."""
    cycle = {
        "underlying": "NIFTY", "expiry": "2026-08-25", "entry_date": "2026-08-20 09:30",
        "exit_date": None, "live": True, "net_pnl": None, "underlying_entry": 24000.0,
        "daily_pnl": [{"date": "2026-08-20", "pnl": -12000.0}],
        "legs_detail": [
            {"symbol": "NIFTY|2026-08-25|24200|CE", "underlying": "NIFTY", "strike": 24200,
             "right": "CE", "expiry": "2026-08-25", "side": "short", "units": 75,
             "entry_date": "2026-08-20 09:30", "entry_premium": 100.0, "exit_date": None,
             "exit_price": None, "pnl": 0.0, "holding_days": None}],
    }
    m = build_cycle_detail(cycle, [], lambda d: 24000.0, [], index=0, run_id=6,
                           strategy_id="delta_neutral_monthly", name="dnm")
    ev = m["events"][0]
    assert ev["unrealized_eod"] == -12000.0 and ev["unrealized_basis"] == "eod"
