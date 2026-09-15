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


# ------------------------------------------------------------- the record (2026-09-15)
# The Simulator logs enough for a later review to judge every adjustment on what was
# known at the minute it was made: the decision context stamped by the console, the
# cycle's path (MAE/MFE off the tape), the owner's why, what Undo took back.

def test_the_cycle_path_reads_mae_and_mfe_off_the_tape(days):
    j = _straddle_cycle(DAY)        # short straddle 10:00 @150/152, covered 11:30 @140/160
    path = simulator.cycle_path(j, "NIFTY")
    assert path and [d["date"] for d in path["daily"]] == [DAY.isoformat()]
    # the ATM pair drifts: CE +1.5/5min, PE −1.5/5min → the straddle's combined premium
    # is flat, so MTM stays near zero until the cover books +2/unit
    assert path["exit_mtm"] == 130.0
    assert path["mfe"]["mtm"] >= 130.0 and path["mae"]["mtm"] <= 0.0
    assert path["daily"][0]["high"] >= path["daily"][0]["low"]
    assert path["exit_vs_mfe_pct"] is not None


def test_actions_are_grouped_and_labelled_by_shape():
    k = DAY.isoformat()
    j = [_row(f"{k}T10:00", CE, "SHORT", 65, 150.0, group=1),
         _row(f"{k}T10:00", PE, "SHORT", 65, 152.0, group=1),
         # roll the CE up: cover it and short the 24100 in one group
         _row(f"{k}T10:30", CE, "COVER", 65, 160.0, group=2),
         _row(f"{k}T10:30", f"NIFTY|{EXP}|24100|CE", "SHORT", 65, 110.0, group=2),
         # buy a wing → hedge
         _row(f"{k}T10:45", f"NIFTY|{EXP}|24500|CE", "BUY", 65, 18.0, group=3),
         # cover half the PE → partial exit
         _row(f"{k}T11:00", PE, "COVER", 30, 150.0, group=4),
         _row(f"{k}T11:30", PE, "COVER", 35, 150.0, group=5),
         _row(f"{k}T11:30", f"NIFTY|{EXP}|24100|CE", "COVER", 65, 100.0, group=5),
         _row(f"{k}T11:30", f"NIFTY|{EXP}|24500|CE", "SELL", 65, 20.0, group=5)]
    j[0]["context"] = {"kind": "straddle", "before": {"spot": 24000}, "after": {"spot": 24000}}
    j[2]["why"] = "CE tested, rolled up"
    acts = simulator.action_groups(j)
    assert [a["label"] for a in acts] == ["entry", "roll", "hedge", "partial_exit", "exit"]
    assert acts[0]["context"]["kind"] == "straddle" and acts[0]["group"] == 1
    assert acts[1]["why"] == "CE tested, rolled up"
    assert all("context" not in r for a in acts for r in a["rows"])   # rows stay light


def test_annotate_and_the_dossier(days):
    with session_scope() as db:
        s = simulator.create(db, name="Straddle", underlying="NIFTY", capital=500_000,
                             playbook="sell the ATM straddle at 10:00", start_day=DAY.isoformat())
        sid = s["id"]
        j = _straddle_cycle(DAY)
        j[0]["context"] = {"kind": "straddle",
                           "before": {"at": f"{DAY}T10:00", "spot": 24000.0, "dte": 14,
                                      "vix": 12.5, "mtm": 0.0, "legs_open": 0, "payoff": None},
                           "after": {"at": f"{DAY}T10:00", "spot": 24000.0, "dte": 14,
                                     "vix": 12.5, "mtm": -100.0, "legs_open": 2,
                                     "greeks": {"delta": 0.1, "gamma": -0.01, "theta": 30,
                                                "vega": -40},
                                     "payoff": {"max_profit": 19630.0, "max_loss": None,
                                                "breakevens": [23698.0, 24302.0],
                                                "be_dist_pct": 1.26, "pop": 0.61}}}
        out = simulator.bank(db, sid, note="held", margin=150_000, margin_source="zerodha",
                             payload={"day": DAY.isoformat(), "clock": "11:30", "expiry": EXP,
                                      "capital": 500_000, "journal": j,
                                      "alerts": [{"kind": "target", "value": 5000,
                                                  "fired_at": None}],
                                      "discarded": [{"group": 9, "undone_at": f"{DAY}T10:20",
                                                     "rows": [_row(f"{DAY}T10:20", CE, "COVER",
                                                                   65, 155.0)]}]})
        c = out["cycle_rows"][0]
        assert c["path"]["exit_mtm"] == 130.0
        assert [a["label"] for a in c["actions"]] == ["entry", "exit"]
        assert c["alerts"][0]["kind"] == "target" and c["discarded"][0]["group"] == 9
        # the owner's why, after the fact
        d = simulator.annotate_action(db, sid, 1, 2, "took the +2 once the PE stalled")
        assert d["cycle_rows"][0]["actions"][1]["why"] == "took the +2 once the PE stalled"
        with pytest.raises(KeyError):
            simulator.annotate_action(db, sid, 1, 42, "no such group")
        with pytest.raises(KeyError):
            simulator.annotate_action(db, sid, 7, 1, "no such cycle")
        md = simulator.dossier_markdown(db, sid)
        db.commit()
    for needle in ("# Straddle — Simulator dossier", "## Playbook", "sell the ATM straddle",
                   "## Cycle 1", "MFE ₹", "**Entry** at", "**Exit** at",
                   "why: took the +2 once the PE stalled", "before: spot 24000.0 · DTE 14",
                   "max P ₹19,630 / max L unlimited", "POP 0.61",
                   "alerts: target 5000 (armed)", "taken back (undo): group 9"):
        assert needle in md, needle
    # the routes: annotate + dossier
    client = TestClient(create_app())
    r = client.patch(f"/api/v1/simulator/{sid}/cycles/1/actions/1", json={"why": "entered flat"})
    assert r.status_code == 200 and r.json()["cycle_rows"][0]["actions"][0]["why"] == "entered flat"
    assert client.patch(f"/api/v1/simulator/{sid}/cycles/1/actions/99",
                        json={"why": "x"}).status_code == 404
    r = client.get(f"/api/v1/simulator/{sid}/dossier")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "why: entered flat" in r.text
    assert client.get("/api/v1/simulator/999999/dossier").status_code == 404


# ----------------------------------------------- counterfactuals + patterns (2026-09-15)

def _rolled_cycle(d: date):
    """Short straddle at 10:00, the CE rolled up to 24100 at 10:30, everything covered at
    11:30. On the fixture tape the CE drifts UP 1.5 every 5 min and the PE down."""
    k = d.isoformat()
    return [_row(f"{k}T10:00", CE, "SHORT", 65, 150.0, group=1),
            _row(f"{k}T10:00", PE, "SHORT", 65, 152.0, group=1),
            _row(f"{k}T10:30", CE, "COVER", 65, 159.0, group=2),
            _row(f"{k}T10:30", f"NIFTY|{EXP}|24100|CE", "SHORT", 65, 114.0, group=2),
            _row(f"{k}T11:30", PE, "COVER", 65, 125.0, group=3),
            _row(f"{k}T11:30", f"NIFTY|{EXP}|24100|CE", "COVER", 65, 132.0, group=3)]


def test_counterfactuals_replay_rules_over_the_tape(days):
    j = _rolled_cycle(DAY)
    rec = simulator.reconstruct(j, "NIFTY")
    cf = simulator.counterfactuals(j, "NIFTY", margin=150_000, credit=rec["premium"])
    by = {x["id"]: x for x in cf}
    assert by["actual"]["net"] == rec["net"] and by["actual"]["vs_actual"] == 0.0
    # the exit rules: a −2% stop is −₹3,000; the straddle's MTM never sinks that far on this
    # tape, so the stop reads "never triggered"; a 25% target on the ₹19,630 credit is
    # ₹4,907 — reached only by the exit itself
    assert {"stop_2", "stop_3", "stop_5", "target_25", "target_50", "trail_half"} <= set(by)
    assert all(x["ok"] for x in cf)
    for x in cf:
        if x["id"] != "actual" and x["note"] and "never triggered" in x["note"]:
            assert x["net"] == rec["net"]
    # the roll removed: the 24000 CE stays short and is closed at its MARK at 11:30 (the
    # tape prints 150 + 1.5 × 27 = 190.5 there, the PE 111.5). "Entry only" marks BOTH legs
    # off the tape (gross ≈ 0, so ≈ −charges); "without the roll" keeps the journal's own
    # PE cover at 125 and marks only the CE — a worse figure, because the fixture's cover
    # price is above the tape's mark. Both walk to the actual exit minute.
    assert "entry_only" in by and "without_2" in by
    assert by["entry_only"]["exit_at"] == f"{DAY}T11:30" == by["without_2"]["exit_at"]
    assert -120 < by["entry_only"]["net"] < rec["net"]
    assert by["without_2"]["net"] < by["entry_only"]["net"]
    assert by["entry_only"]["vs_actual"] == round(by["entry_only"]["net"] - rec["net"], 2)
    # banked: stored on the cycle, printed in the dossier
    with session_scope() as db:
        s = simulator.create(db, name="Roll", underlying="NIFTY", capital=500_000,
                             start_day=DAY.isoformat())
        out = simulator.bank(db, s["id"], margin=150_000, margin_source="zerodha",
                             payload={"day": DAY.isoformat(), "clock": "11:30", "expiry": EXP,
                                      "capital": 500_000, "journal": j})
        c = out["cycle_rows"][0]
        assert [x["id"] for x in c["counterfactuals"]][:2] == ["actual", "stop_2"]
        assert [a["label"] for a in c["actions"]] == ["entry", "roll", "exit"]
        md = simulator.dossier_markdown(db, s["id"])
        assert "### Counterfactuals" in md and "Hold the entry book, no adjustments" in md
        assert "## Patterns across cycles" in md and "needs 5 banked cycles (1 so far)" in md
        assert out["patterns"]["ok"] is False
        db.commit()


def test_patterns_need_five_cycles_and_state_their_sample():
    def cyc(n, net, rank, vix, dte, mfe_pct, roll_after=None):
        ctx = {"after": {"iv_rank": {"rank": rank}, "vix": vix, "dte": dte, "mtm": 0.0}}
        acts = [{"group": 1, "at": f"2026-07-{n:02d}T10:00", "label": "entry", "context": ctx,
                 "rows": []}]
        if roll_after is not None:
            acts.append({"group": 2, "at": f"2026-07-{n:02d}T11:00", "label": "roll",
                         "context": {"after": {"mtm": roll_after}}, "rows": []})
        acts.append({"group": 3, "at": f"2026-07-{n:02d}T14:00", "label": "exit", "rows": []})
        return {"n": n, "net": net, "entered": f"2026-07-{n:02d}T10:00",
                "exited": f"2026-07-{n:02d}T14:00", "expiry": "2026-07-21",
                "entry_day": f"2026-07-{n:02d}", "margin": 100_000, "actions": acts,
                "path": {"exit_vs_mfe_pct": mfe_pct, "mae": {"mtm": -1000.0}}}
    few = [cyc(i, 100.0, 20, 11.0, 7, 80.0) for i in range(1, 5)]
    assert simulator.patterns(few)["ok"] is False and "needs 5" in simulator.patterns(few)["note"]
    many = [cyc(1, 500.0, 20, 11.0, 7, 90.0), cyc(2, 300.0, 25, 11.5, 8, 70.0),
            cyc(3, -800.0, 80, 17.0, 30, 20.0, roll_after=-1500.0),
            cyc(4, -200.0, 75, 16.5, 28, 30.0, roll_after=-600.0),
            cyc(5, 100.0, 50, 13.0, 15, 60.0)]
    p = simulator.patterns(many)
    assert p["ok"] and p["n"] == 5
    t = {r["bucket"]: r for r in p["tables"]["IV rank at entry"]}
    assert t["low (<30)"]["n"] == 2 and t["low (<30)"]["win_rate"] == 100.0
    assert t["high (≥70)"]["n"] == 2 and t["high (≥70)"]["avg"] == -500.0
    adj = p["tables"]["adjustments"][0]
    # after the two rolls the cycles moved −800−(−1500)=+700 and −200−(−600)=+400 → avg +550
    assert adj == {"adjustment": "roll", "n": 2, "avg_after": 550.0, "improved": 2}
    assert any("By IV rank at entry" in ln for ln in p["lines"])
    assert any("Exits kept 54% of each cycle's best MTM" in ln for ln in p["lines"])
    assert any("Winners were held" in ln for ln in p["lines"])


def test_the_open_cycle_can_be_discarded_without_touching_the_strategy(days):
    with session_scope() as db:
        s = simulator.create(db, name="D", underlying="NIFTY", capital=500_000,
                             start_day=DAY.isoformat())
        sid = s["id"]
        j = _straddle_cycle(DAY)
        simulator.bank(db, sid, payload={"day": DAY.isoformat(), "clock": "11:30",
                                         "expiry": EXP, "capital": 500_000, "journal": j})
        simulator.autosave(db, sid, {"day": "2026-07-15", "clock": "10:05", "expiry": EXP,
                                     "capital": 500_090, "journal": _straddle_cycle(
                                         date(2026, 7, 15))[:2]})
        assert simulator.get(db, sid)["open_cycle"]["fills"] == 2
        out = simulator.discard_open(db, sid)
        assert out["discarded"] is True and out["open_cycle"] is None
        assert out["cycles"] == 1 and out["equity"] == 500_090          # the bank stands
        assert simulator.open_spec(db, sid)["restore"] is None            # a fresh start
        assert simulator.discard_open(db, sid)["discarded"] is False
        db.commit()
    client = TestClient(create_app())
    assert client.delete(f"/api/v1/simulator/{sid}/open").status_code == 200
    assert client.delete("/api/v1/simulator/999999/open").status_code == 404
