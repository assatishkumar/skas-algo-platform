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
