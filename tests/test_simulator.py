"""The Simulator ledger: a manual backtest stored as an ordinary run (owner design,
2026-09-11 — flat-based cycles, compounding capital, hidden from the Runs list)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data import option_intraday_store as store
from skas_algo.db.base import session_scope
from skas_algo.services import simulator
from tests.test_options_console import DAY, EXP, _day

CE = f"NIFTY|{EXP}|24000|CE"
PE = f"NIFTY|{EXP}|24000|PE"


def _row(at, symbol, action, units, price, charges=10.0, spot=24010.0, group=1):
    return {"at": at, "symbol": symbol, "action": action, "group": group, "units": units,
            "price": price, "charges": charges, "spot": spot}


def _straddle_cycle(d: date, entry=("10:00", 150.0, 152.0), exit=("11:30", 140.0, 160.0)):
    """Sell the ATM straddle at `entry`, buy it back at `exit` — a net +₹130/unit × 65."""
    a, ce_in, pe_in = entry
    b, ce_out, pe_out = exit
    k = d.isoformat()
    return [_row(f"{k}T{a}", CE, "SHORT", 65, ce_in, spot=24000.0),
            _row(f"{k}T{a}", PE, "SHORT", 65, pe_in, spot=24000.0),
            _row(f"{k}T{b}", CE, "COVER", 65, ce_out, spot=24050.0, group=2),
            _row(f"{k}T{b}", PE, "COVER", 65, pe_out, spot=24050.0, group=2)]


@pytest.fixture
def days():
    store.write_day(DAY, _day())
    store.write_day(date(2026, 7, 15), _day(date(2026, 7, 15)))
    store.write_day(date(2026, 7, 16), _day(date(2026, 7, 16)))


def test_a_cycle_is_reconstructed_from_its_journal_alone():
    rec = simulator.reconstruct(_straddle_cycle(DAY), "NIFTY")
    # short CE 150 → 140 = +10, short PE 152 → 160 = −8 → +2/unit × 65 = ₹130 gross
    assert rec["realized"] == 130.0 and rec["charges"] == 40.0 and rec["net"] == 90.0
    assert rec["entered"] == f"{DAY}T10:00" and rec["exited"] == f"{DAY}T11:30"
    assert rec["entry_spot"] == 24000.0 and rec["exit_spot"] == 24050.0
    assert {leg["side"] for leg in rec["legs"]} == {"short"} and len(rec["legs"]) == 2
    assert rec["premium"] == pytest.approx((150.0 + 152.0) * 65)   # credit received
    assert simulator.is_flat(_straddle_cycle(DAY))
    assert not simulator.is_flat(_straddle_cycle(DAY)[:3])          # one leg still short
    # a settlement closes a leg to flat whatever the sign
    j = _straddle_cycle(DAY)[:2] + [_row(f"{EXP}T15:30", CE, "SETTLE", 65, 0.0, charges=0.0),
                                     _row(f"{EXP}T15:30", PE, "SETTLE", 65, 0.0, charges=0.0)]
    assert simulator.is_flat(j) and simulator.reconstruct(j, "NIFTY")["realized"] == 302 * 65


def test_create_bank_and_compound_across_two_cycles(days):
    with session_scope() as db:
        s = simulator.create(db, name="Weekly straddle", underlying="NIFTY", capital=500_000,
                             playbook="sell ATM straddle at 10:00, buy back by 11:30",
                             start_day=DAY.isoformat())
        sid = s["id"]
        assert s["cycles"] == 0 and s["equity"] == 500_000 and s["next_day"] == DAY.isoformat()
        spec = simulator.open_spec(db, sid)
        assert spec["day"] == DAY.isoformat() and spec["at"] == "09:30" and spec["restore"] is None
        assert spec["capital"] == 500_000 and spec["cycle_no"] == 1
        # autosave while the book is open, then bank once flat
        j = _straddle_cycle(DAY)
        st = simulator.autosave(db, sid, {"day": DAY.isoformat(), "clock": "10:05",
                                          "expiry": EXP, "capital": 500_000, "journal": j[:2]})
        assert st == {"fills": 2, "flat": False, "traded": True}
        with pytest.raises(ValueError, match="not flat"):
            simulator.bank(db, sid)
        spec = simulator.open_spec(db, sid)                        # reopens where it stands
        assert spec["restore"]["journal"] == j[:2] and spec["at"] == "10:05"
        out = simulator.bank(db, sid, note="held to 11:30", tags=["straddle"],
                             margin=150_000, margin_source="zerodha",
                             payload={"day": DAY.isoformat(), "clock": "11:30", "expiry": EXP,
                                      "capital": 500_000, "journal": j})
        b = out["banked"]
        assert b["n"] == 1 and b["net"] == 90.0 and b["rom_pct"] == round(100 * 90 / 150_000, 2)
        assert b["capital_before"] == 500_000 and b["capital_after"] == 500_090
        assert out["equity"] == 500_090 and out["cycles"] == 1 and out["open_cycle"] is None
        assert out["next_day"] == "2026-07-15"                   # the next captured trading day
        # the report is the standard contract, options sub-report complete
        _algo, run = simulator._run_of(db, sid)
        m = run.metrics
        assert m["metrics"]["Total Trades"] == 1 and m["metrics"]["Win Rate %"] == 100.0
        assert m["metrics"]["Net Realized P&L"] == 90.0 and m["metrics"]["Total Charges"] == 40.0
        assert m["equity_curve"][-1] == {"date": DAY.isoformat(), "equity": 500_090.0}
        assert m["options"]["summary"]["num_cycles"] == 1
        assert m["options"]["summary"]["total_charges"] == 40.0
        assert m["options"]["summary"]["max_margin_used"] == 150_000
        assert m["options"]["cycles"][0]["note"] == "held to 11:30"
        assert [t["action"] for t in run.trade_log] == ["SHORT", "SHORT", "COVER", "COVER"]
        assert run.trade_log[2]["profit"] == 650.0 and run.trade_log[3]["profit"] == -520.0
        # cycle 2 opens on the next day with the compounded capital; a loss compounds too
        spec = simulator.open_spec(db, sid)
        assert spec["day"] == "2026-07-15" and spec["capital"] == 500_090 and spec["cycle_no"] == 2
        j2 = _straddle_cycle(date(2026, 7, 15), exit=("11:30", 170.0, 172.0))   # −40/unit
        out = simulator.bank(db, sid, payload={"day": "2026-07-15", "clock": "11:30",
                                               "expiry": EXP, "capital": 500_090, "journal": j2})
        assert out["banked"]["net"] == -40 * 65 - 40 and out["equity"] == 500_090 - 2600 - 40
        assert out["win_rate"] == 50.0 and len(out["cycle_rows"]) == 2
        assert "journal" not in out["cycle_rows"][0]
        curve = simulator._run_of(db, sid)[1].metrics["equity_curve"]
        assert [c["date"] for c in curve] == [DAY.isoformat(), "2026-07-15"]
        db.commit()


def test_the_routes_and_the_runs_list_hide_it(days):
    client = TestClient(create_app())
    r = client.post("/api/v1/simulator", json={"name": "Sim A", "underlying": "NIFTY",
                                               "capital": 300_000, "start_day": DAY.isoformat()})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert client.get("/api/v1/simulator").json()["strategies"][0]["id"] == sid
    j = _straddle_cycle(DAY)
    r = client.post(f"/api/v1/simulator/{sid}/bank",
                    json={"note": "n", "payload": {"day": DAY.isoformat(), "clock": "11:30",
                                                   "expiry": EXP, "capital": 300_000,
                                                   "journal": j[:2]}})
    assert r.status_code == 422 and "not flat" in r.text
    r = client.post(f"/api/v1/simulator/{sid}/bank",
                    json={"note": "n", "payload": {"day": DAY.isoformat(), "clock": "11:30",
                                                   "expiry": EXP, "capital": 300_000,
                                                   "journal": j}})
    assert r.status_code == 200 and r.json()["cycles"] == 1
    run_id = r.json()["run_id"]
    assert all(x["run_id"] != run_id for x in client.get("/api/v1/runs").json())   # hidden
    detail = client.get(f"/api/v1/runs/{run_id}")
    assert detail.status_code == 200 and detail.json()["strategy_id"] == "manual_sim"
    assert detail.json()["report"]["options"]["summary"]["num_cycles"] == 1
    r = client.patch(f"/api/v1/simulator/{sid}", json={"playbook": "new rule"})
    assert r.json()["playbook"] == "new rule"
    assert client.delete(f"/api/v1/simulator/{sid}").json() == {"deleted": sid}
    assert client.get(f"/api/v1/simulator/{sid}").status_code == 404
