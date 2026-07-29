"""Analytics bundle for the Analyze workbench: per-trade MAE/MFE math, cache round-trip,
job-slot isolation, and the route surface. Synthetic run in the isolated test DB; the
1-min-store path reconstruction is faked (the real reader is loss_study's, tested there)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from skas_algo.services import replay_jobs, run_analytics


def _cycle(entry="2026-03-02 09:18", exit_="2026-03-02 15:25", net=3000.0):
    return {
        "underlying": "NIFTY", "expiry": "2026-03-03",
        "entry_date": entry, "exit_date": exit_, "exit_reason": "eod",
        "net_pnl": net, "holding_days": 0,
        "underlying_entry": 24000.0, "underlying_exit": 24050.0, "underlying_pct": 0.21,
        "vix_entry": 14.2, "vix_exit": 13.8,
        "legs_detail": [
            {"symbol": "NIFTY|2026-03-03|24000|CE", "strike": 24000, "right": "CE",
             "side": "short", "expiry": "2026-03-03", "entry_premium": 100.0,
             "exit_price": 80.0, "units": 75, "pnl": 1500.0},
            {"symbol": "NIFTY|2026-03-03|24000|PE", "strike": 24000, "right": "PE",
             "side": "short", "expiry": "2026-03-03", "entry_premium": 50.0,
             "exit_price": 30.0, "units": 75, "pnl": 1500.0},
        ],
    }


def _mk_run(db_session=None, cycles=None, basis="intraday"):
    from skas_algo.db.base import session_scope
    from skas_algo.db.enums import InstrumentClass, TradingMode
    from skas_algo.db.models import Algo, AlgoRun

    report = {
        "metrics": {"Net Realized P&L": 6000.0, "CAGR %": 12.0},
        "equity_curve": [{"date": "2026-03-02", "equity": 1_003_000.0},
                         {"date": "2026-03-03", "equity": 1_006_000.0}],
        "options": {"cycles": cycles or [_cycle(), _cycle("2026-03-03 09:18",
                                                          "2026-03-03 15:25")],
                    "summary": {"max_margin_used": 300000.0},
                    "margin_series": []},
    }
    params = {"underlying": "NIFTY"}
    if basis == "intraday":
        params["data_basis"] = "intraday"
    with session_scope() as db:
        algo = Algo(name="an_test", strategy_id="intraday_straddle", capital=1_000_000,
                    params=params, instrument_class=InstrumentClass.DERIV,
                    mode=TradingMode.BACKTEST)
        db.add(algo)
        db.flush()
        run = AlgoRun(algo_id=algo.id, mode=TradingMode.BACKTEST, params_snapshot=params)
        run.metrics = report
        run.trade_log = [{"date": "2026-03-02 09:18", "charge": 120.0},
                         {"date": "2026-03-02 15:25", "charge": 130.0}]
        db.add(run)
        db.flush()
        return run.id


def _fake_path(cycle, loader):
    """10-minute path: dips to −₹2,250 (−20% of the ₹11,250 credit) at minute 2, peaks
    +₹3,375 (+30%) at minute 6."""
    minutes = pd.date_range(str(cycle["entry_date"]).replace(" ", "T"), periods=10, freq="min")
    mtm = np.array([0, -1000, -2250, -500, 1000, 2000, 3375, 3000, 2800, 3000], dtype=float)
    return {"entry": minutes[0], "exit": minutes[-1], "body_units": 150,
            "minutes": minutes, "ts": np.array([m.value for m in minutes]),
            "mtm": mtm, "day": np.array([m.date() for m in minutes]),
            "entry_spot": 24000.0}


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(run_analytics, "ANALYTICS_DIR", tmp_path / "analytics")


def test_bundle_trade_math_and_cache(monkeypatch):
    monkeypatch.setattr(run_analytics, "reconstruct_path", _fake_path)
    rid = _mk_run()
    b = run_analytics.build_bundle(rid)
    assert b["basis"] == "intraday" and b["kpis"]["trades"] == 2
    t = b["trades"][0]
    # credit = 100×75 + 50×75 = ₹11,250 → MAE −2250 = −20%, MFE +3375 = +30% at minute 6
    assert t["credit_rs"] == 11250.0 and t["credit_pts"] == 150.0
    assert t["mae_pct"] == -20.0 and t["mfe_pct"] == 30.0 and t["t_mfe_min"] == 6
    assert t["skew_entry"] == 0.5 and t["skew_exit"] == pytest.approx(0.375)
    assert t["path5"][0] == [0, 0.0] and t["path5"][-1][0] == 9   # last point always kept
    assert t["dte"] == 1 and t["weekday"] == 0 and t["entry_slot"] == "09:18"
    # headline KPIs copied from the run's own report (Analyze == Run Detail, to the rupee)
    assert b["kpis"]["total_pnl"] == 6000.0 and b["kpis"]["win_rate"] == 100.0
    assert b["costs"] == {"charges": 250.0, "net": 6000.0, "gross": 6250.0,
                          "breakdown": None}
    # v5 surfaces: regimes within the window, liquidity counters, daily margin key
    assert isinstance(b["regimes"], list) and "liquidity" in b
    assert "margin" in b["daily"][0] and b["trades"][0]["n_legs"] == 2
    # per-trade charge allocation: both fills fall in cycle-1's window → 250/0
    assert t["charge_rs"] == 250.0 and b["trades"][1]["charge_rs"] == 0.0
    assert b["coverage"]["simulator"] == "full"
    # melt aggregation from the same paths: both fake trades land in slot 0 (09:15-09:29),
    # normalized premium 1.0 at the first observed slot, MTM overlay = 3000/11250 = 26.67%
    m = b["melt"]
    assert m["trades"] == 2 and m["premium_by_dte"]["1"][0] == 1.0
    assert m["mtm_by_dte"]["all"][0] == pytest.approx(26.67, abs=0.01)
    assert m["entries"][0] == 2 and b["coverage"]["melt"] == "full"
    assert "ivhv" in b["trades"][0]
    # cache round-trip + staleness on trade-count change
    assert run_analytics.load_cached(rid, 2)["run_id"] == rid
    assert run_analytics.load_cached(rid, 99) is None


def test_eod_basis_degrades_not_errors(monkeypatch):
    monkeypatch.setattr(run_analytics, "reconstruct_path",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not be called")))
    rid = _mk_run(basis="eod")
    b = run_analytics.build_bundle(rid)
    assert b["basis"] == "eod"
    assert b["coverage"]["simulator"] == "none" and b["coverage"]["lifecycle"] == "none"
    assert b["trades"][0]["mae_pct"] is None and b["trades"][0]["path5"] == []
    assert b["trades"][0]["skew_entry"] == 0.5     # conditioning fields still full


def test_job_slots_are_isolated():
    import threading

    gate = threading.Event()
    replay_jobs.start(lambda p: gate.wait(5), slot="analytics", busy_msg="busy-a")
    # the analytics slot is busy…
    with pytest.raises(RuntimeError, match="busy-a"):
        replay_jobs.start(lambda p: None, slot="analytics", busy_msg="busy-a")
    # …but the default replay slot is untouched (and vice versa)
    jid = replay_jobs.start(lambda p: "ok")
    assert jid and replay_jobs.snapshot()["status"] in ("running", "done")
    assert replay_jobs.snapshot(slot="analytics")["status"] == "running"
    gate.set()


def test_routes_surface(client, monkeypatch):
    monkeypatch.setattr(run_analytics, "reconstruct_path", _fake_path)
    rid = _mk_run()
    # not computed yet → 404
    assert client.get(f"/api/v1/runs/{rid}/analytics").status_code == 404
    # compute synchronously (the job wrapper is threaded; build directly for the test)
    run_analytics.build_bundle(rid)
    r = client.get(f"/api/v1/runs/{rid}/analytics")
    assert r.status_code == 200 and r.json()["kpis"]["trades"] == 2
    # a second compute call short-circuits on the cache
    assert client.post(f"/api/v1/runs/{rid}/analytics/compute").json() == {"status": "cached"}
    # /analysis/runs now carries the basis + underlying
    rows = client.get("/api/v1/analysis/runs").json()
    row = next(x for x in rows if x["run_id"] == rid)
    assert row["data_basis"] == "intraday" and row["underlying"] == "NIFTY"


def test_real_cycle_shape_leg_level_exits(monkeypatch):
    """Run #216 taught us the replay's cycles carry exit_date on the LEGS only, and no
    vix fields — the builder derives the cycle exit (max leg exit) and back-fills VIX
    from the cache lookup."""
    monkeypatch.setattr(run_analytics, "reconstruct_path", _fake_path)
    monkeypatch.setattr(run_analytics, "_ffill_lookup", lambda sd, sym: (lambda d: 15.5))
    import skas_algo.data.provider as provider
    monkeypatch.setattr(provider, "get_data_cache", lambda: object())
    c = _cycle()
    del c["exit_date"], c["vix_entry"], c["vix_exit"]
    c["legs_detail"][0]["exit_date"] = "2026-03-02 15:25"
    c["legs_detail"][1]["exit_date"] = "2026-03-02 15:26"   # later leg wins
    rid = _mk_run(cycles=[c])
    b = run_analytics.build_bundle(rid)
    t = b["trades"][0]
    assert t["exit"] == "2026-03-02 15:26" and t["hold_min"] == 368
    assert t["vix_entry"] == 15.5 and t["vix_exit"] == 15.5
    # an OPEN cycle (a leg without exit_date) is excluded, not crashed on
    c2 = _cycle()
    del c2["exit_date"]
    c2["legs_detail"][0]["exit_date"] = None
    rid2 = _mk_run(cycles=[c, c2])
    assert run_analytics.build_bundle(rid2)["kpis"]["trades"] == 1
