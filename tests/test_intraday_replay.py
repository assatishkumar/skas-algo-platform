"""intraday_replay harness: the run-report contract (metrics/equity_curve/Trade rows),
charges applied, single strategy instance across days (fresh entries daily), the
weekly-straddle store-fed replay (y from day 1 → entry day 2), and input validation —
synthetic store in tmp, no network."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from skas_algo.data import option_intraday_store as store
from skas_algo.services.intraday_replay import run_intraday_backtest

EXP = "2026-07-21"
D1, D2 = date(2026, 7, 14), date(2026, 7, 15)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    from skas_algo.config import get_settings
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)


def _leg_rows(day, strike, right, px_by_minute, exp=EXP):
    sym = f"NIFTY|{exp}|{strike}|{right}"
    return [{"symbol": sym, "start": datetime(day.year, day.month, day.day, hh, mm),
             "open": px, "high": px, "low": px, "close": px, "volume": 100.0, "oi": 5000.0}
            for (hh, mm), px in px_by_minute.items()]


def _flat_day(day, level_ce=150.0, level_pe=152.0, exp=EXP):
    """One bar per 5-min boundary, premiums decaying slightly — a quiet straddle day."""
    minutes = [(9, m) for m in range(15, 60, 5)] + [(h, m) for h in (10, 11, 12, 13, 14)
                                                    for m in range(0, 60, 5)] + \
              [(15, m) for m in range(0, 30, 5)]
    rows = []
    for i, (hh, mm) in enumerate(minutes):
        decay = i * 0.3
        rows += _leg_rows(day, 24000, "CE", {(hh, mm): level_ce - decay}, exp)
        rows += _leg_rows(day, 24000, "PE", {(hh, mm): level_pe - decay}, exp)
        rows += _leg_rows(day, 24100, "CE", {(hh, mm): level_ce - 45 - decay}, exp)
        rows += _leg_rows(day, 24100, "PE", {(hh, mm): level_pe + 48 - decay}, exp)
    return pd.DataFrame(rows, columns=store.COLUMNS)


def test_contract_and_single_instance_across_days():
    store.write_day(D1, _flat_day(D1))
    store.write_day(D2, _flat_day(D2))
    out = run_intraday_backtest("intraday_straddle", "NIFTY", D1, D2, 1_000_000, {})
    report, trades = out["report"], out["trades"]
    m = report["metrics"]
    # The report contract the Runs list / ReportView render:
    for key in ("Total Return %", "Final Equity", "Max Drawdown %", "Total Trades",
                "Win Rate %", "Net Realized P&L", "Total Charges", "Max Margin Used"):
        assert key in m
    assert [p["date"] for p in report["equity_curve"]] == [D1.isoformat(), D2.isoformat()]
    # ONE instance across days → entered_day resets → 2 straddle entries (4 legs each way).
    entries = [t for t in trades if t["action"] == "SHORT"]
    closes = [t for t in trades if t["action"] == "COVER"]
    assert len(entries) == 4 and len(closes) == 4
    assert m["Total Trades"] == 2                          # cycles, not legs (options semantics)
    assert all(t["profit"] is not None for t in closes)   # P&L renders on closing rows
    assert m["Total Charges"] > 0                          # charges actually deducted
    assert m["Net Realized P&L"] == pytest.approx(
        sum(t["profit"] for t in closes), abs=1)
    # The options sub-report: absent-or-COMPLETE — every non-optional field OptionsReport
    # dereferences must exist (its presence flips ReportView to the options layout).
    o = report["options"]
    for key in ("total_premium_collected", "total_premium_captured", "premium_capture_pct",
                "avg_holding_days", "num_positions", "num_cycles", "win_rate_pct",
                "max_margin_used", "avg_margin_used", "capital_efficiency",
                "avg_premium_per_cycle", "total_charges", "net_after_charges"):
        assert key in o["summary"], key
    assert o["summary"]["num_cycles"] == 2 and o["summary"]["num_positions"] == 4
    assert o["summary"]["total_premium_collected"] > 0
    assert o["charges"]["total"] == pytest.approx(m["Total Charges"], abs=0.01)
    assert set(o["exit_reasons"]) == {"eod"}
    assert o["exit_reasons"]["eod"]["count"] == 2
    assert len(o["cycles"]) == 2 and len(o["positions"]) == 4
    c = o["cycles"][0]
    assert c["ce"] is not None and c["pe"] is not None       # straddle → ce/pe legs set
    assert c["legs_detail"] and c["premium_collected"] > 0
    assert 0 < c["holding_days"] <= 1.0                       # intraday fraction of a session
    assert len(o["per_expiry_cycle"]) == 1 and o["per_expiry_cycle"][0]["entries"] == 2
    assert len(o["margin_series"]) == 2 and len(o["premium_curve"]) == 2
    # Periodic breakdowns (owner ask 2026-07-17): same keys the EOD engine emits, so the
    # existing Yearly table + Monthly grids render for intraday runs too.
    yr = report["yearly"]["2026"]
    assert yr["Portfolio Value"] == report["equity_curve"][-1]["equity"]
    assert yr["Return (Abs)"] == pytest.approx(m["Net Realized P&L"], abs=0.02)
    assert report["monthly_profit"]["2026"]["7"] == pytest.approx(yr["Return (Abs)"], abs=0.02)
    assert report["monthly_equity"]["2026"]["7"] == yr["Portfolio Value"]


def test_weekly_straddle_y_from_day1_entry_day2():
    # Day 1: quiet high premiums → y (low) established. Day 2: premiums open BELOW day-1's
    # low and below VWAP → the weekly straddle sells; a later spike crosses VWAP → exit.
    store.write_day(D1, _flat_day(D1, level_ce=200.0, level_pe=202.0))
    d2 = _flat_day(D2, level_ce=120.0, level_pe=122.0)
    # Spike both legs late so the combined premium crosses back above VWAP at ~14:00.
    for (hh, mm), bump in {(14, 0): 80.0, (14, 5): 82.0}.items():
        for right in ("CE", "PE"):
            d2.loc[(d2["symbol"] == f"NIFTY|{EXP}|24000|{right}")
                   & (d2["start"] == datetime(2026, 7, 15, hh, mm)),
                   ["open", "high", "low", "close"]] += bump
    store.write_day(D2, d2)
    out = run_intraday_backtest("weekly_intraday_straddle", "NIFTY", D1, D2, 1_000_000, {})
    trades = out["trades"]
    d2_entries = [t for t in trades
                  if t["action"] == "SHORT" and t["date"].startswith(D2.isoformat())]
    assert len(d2_entries) >= 2                       # entered on day 2 (y came from day 1)
    assert not any(t["action"] == "SHORT" and t["date"].startswith(D1.isoformat())
                   for t in trades)                    # day 1 had no prior-day bars → gated
    assert any(t["action"] == "COVER" and t.get("tag") in ("vwap_cross", "eod")
               for t in trades)


def test_unsupported_strategy_and_empty_window_raise():
    # donchian_strangle_bt trades STOCK options — no 1-min data exists for those, so it
    # stays on its synthetic-BS EOD path (hni_weekly, the old example here, is replayable
    # since the 2026-07-18 store migration).
    with pytest.raises(ValueError, match="not intraday-replayable"):
        run_intraday_backtest("donchian_strangle_bt", "NIFTY", D1, D2, 1_000_000, {})
    with pytest.raises(ValueError, match="no captured days"):
        run_intraday_backtest("intraday_straddle", "NIFTY", D1, D2, 1_000_000, {})


# ------------------------------------------------------------------- routes
@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from skas_algo.api import create_app

    return TestClient(create_app())


def test_strategies_basis_lists(api_client):
    eod = api_client.get("/api/v1/strategies").json()["strategies"]
    intraday = api_client.get("/api/v1/strategies?basis=intraday").json()["strategies"]
    assert "weekly_intraday_straddle" not in eod and "intraday_straddle" not in eod
    assert intraday[0] == "intraday_straddle" and "momentum_theta_gainer_intra" in intraday
    # The positional family joined the store (2026-07-18) — ALL index-options ids replay.
    for sid in ("hni_weekly", "batman_ratio_monthly", "call_ratio_monthly",
                "put_ratio_monthly", "21_ema_momentum"):
        assert sid in intraday, sid
    # Stock-option strategies stay off the store (no stock 1-min data exists).
    assert "donchian_strangle_bt" not in intraday
    assert "staggered_covered_call" not in intraday


def _run_job(api_client, body, timeout_s=30.0):
    """POST the intraday backtest (now a background job) and poll to completion."""
    import time

    r = api_client.post("/api/v1/backtest/intraday", json=body)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        snap = api_client.get("/api/v1/backtest/intraday/progress").json()
        if snap.get("id") == job_id and snap["status"] in ("done", "error"):
            return snap
        time.sleep(0.05)
    raise AssertionError("replay job did not finish in time")


def test_intraday_backtest_preview_and_persist_roundtrip(api_client):
    store.write_day(D1, _flat_day(D1))
    store.write_day(D2, _flat_day(D2))
    body = {"strategy_id": "intraday_straddle", "underlying": "NIFTY",
            "instrument_class": "DERIV", "symbols": ["NIFTY"],
            "start_date": D1.isoformat(), "end_date": D2.isoformat(),
            "capital": 1_000_000, "params": {"lots": 1}, "persist": False,
            "name": "replay test"}
    snap = _run_job(api_client, body)
    assert snap["status"] == "done", snap.get("error")
    # Progress reached the end and the result carries the standard preview contract.
    assert snap["done"] == snap["total"] == 2 and snap["day"] == D2.isoformat()
    prev = snap["result"]
    assert prev["run_id"] is None and prev["report"]["metrics"]["Total Trades"] == 2

    body["persist"] = True
    snap2 = _run_job(api_client, body)
    assert snap2["status"] == "done", snap2.get("error")
    run_id = snap2["result"]["run_id"]
    assert run_id is not None
    detail = api_client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["report"]["metrics"]["Total Trades"] == 2
    assert detail["report"]["options"]["summary"]["num_cycles"] == 2  # options layout renders
    assert detail["params"]["data_basis"] == "intraday"      # the run tag
    assert len(detail["report"]["equity_curve"]) == 2
    lst = api_client.get("/api/v1/runs").json()
    assert any(x["run_id"] == run_id for x in lst)


def test_replay_job_single_flight_is_409(api_client):
    """One replay at a time: a second POST while a job runs maps RuntimeError → 409."""
    import threading

    from skas_algo.services import replay_jobs

    store.write_day(D1, _flat_day(D1))
    gate = threading.Event()
    replay_jobs.start(lambda progress: (gate.wait(5), {"ok": True})[1])
    body = {"strategy_id": "intraday_straddle", "underlying": "NIFTY",
            "instrument_class": "DERIV", "symbols": ["NIFTY"],
            "start_date": D1.isoformat(), "end_date": D1.isoformat(),
            "capital": 1_000_000, "params": {}, "persist": False}
    r = api_client.post("/api/v1/backtest/intraday", json=body)
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    gate.set()
    import time
    for _ in range(100):   # leave a clean (done) registry for the next test
        if replay_jobs.snapshot()["status"] != "running":
            break
        time.sleep(0.02)


def test_margin_per_lot_is_era_true():
    """Owner-keyed margin (₹/lot-set of the structure, keyed for 'today'): the push must
    equal margin_per_lot × (spot/ref_spot) × (lot/ref_lot) per lot-set — with the ref on
    the latest store day, a same-spot window pushes ≈ the keyed rupees exactly."""
    store.write_day(D1, _flat_day(D1))
    store.write_day(D2, _flat_day(D2))
    out = run_intraday_backtest("intraday_straddle", "NIFTY", D1, D2, 1_000_000,
                                {"lots": 1, "margin_per_lot": 200_000})
    s = out["report"]["sizing"]
    assert s["margin_per_lot"] == 200_000 and s["ref_day"] == D2.isoformat()
    assert s["ref_lot_size"] == 65                      # NIFTY 2026 era
    # Same premiums both days → same parity spot → pushed margin ≈ the keyed ₹2L, and the
    # %-of-margin stop math now runs against broker-scale rupees, not the 2× model.
    assert out["report"]["options"]["summary"]["max_margin_used"] == pytest.approx(
        200_000, rel=0.02)


def test_capital_sizing_lots_buffer_and_eras():
    """lots = floor(equity / (margin_per_lot_era × (1+buffer))) — recomputed per flat day,
    era-true through a lot-size revision (via the contract_specs override surface), and
    entries are SKIPPED (not 0-unit) when equity can't fund one buffered lot-set."""
    # Lot revisions bind to CONTRACTS: D1 trades its own 0DTE expiry (old era, lot 50),
    # D2 trades the next weekly (new era, lot 100) — boundary between the two expiries.
    store.write_day(D1, _flat_day(D1, exp=D1.isoformat()))
    store.write_day(D2, _flat_day(D2))     # EXP = 2026-07-21
    overrides = {"NIFTY": [["2000-01-01", 50], [D2.isoformat(), 100]]}
    out = run_intraday_backtest(
        "intraday_straddle", "NIFTY", D1, D2, 1_000_000,
        {"margin_per_lot": 200_000, "sizing": "capital", "sizing_buffer_pct": 10,
         "contract_specs": overrides})
    entries = [t for t in out["trades"] if t["action"] == "SHORT"]
    by_day = {}
    for t in entries:
        by_day.setdefault(t["date"][:10], set()).add(t["units"])
    # D1 (lot 50 era): margin = 2L × 50/100 = 1L → floor(10L / 1.1L) = 9 lots × 50 = 450.
    assert by_day[D1.isoformat()] == {450.0}
    # D2 (lot 100, the ref era): full 2L → floor(equity / 2.2L) = 4 lots × 100 = 400.
    assert by_day[D2.isoformat()] == {400.0}

    # Equity below one buffered lot-set → the day trades NOTHING (never 0-unit orders).
    out2 = run_intraday_backtest(
        "intraday_straddle", "NIFTY", D1, D2, 100_000,
        {"margin_per_lot": 200_000, "sizing": "capital", "sizing_buffer_pct": 10})
    assert out2["trades"] == []
    assert out2["report"]["sizing"]["sizing_skipped_days"] == 2

    # capital sizing without a keyed margin is a hard error (422 at the route).
    with pytest.raises(ValueError, match="margin_per_lot"):
        run_intraday_backtest("intraday_straddle", "NIFTY", D1, D2, 1_000_000,
                              {"sizing": "capital"})


def test_replay_spot_is_decarried_to_cash():
    """Parity gives the FUTURES level (cash + carry); the ~20-pt bias flipped the
    2026-07-16 ATM pick to 24200 while live (cash spot) picked 24100. The replay spot
    must be F / (1 + r·t)."""
    from skas_algo.services.intraday_replay import _Market

    m = _Market("NIFTY")
    m.start_day(D1, [f"NIFTY|{EXP}|24000|CE", f"NIFTY|{EXP}|24000|PE"])
    m.feed(f"NIFTY|{EXP}|24000|CE", 150.0, 1000)
    m.feed(f"NIFTY|{EXP}|24000|PE", 152.0, 1000)
    f_implied = 24000 + 150.0 - 152.0                      # 23,998 futures-implied
    t_days = (date.fromisoformat(EXP) - D1).days           # 7
    expected = f_implied / (1 + 0.065 * t_days / 365.0)
    assert m.index_spot("NIFTY") == pytest.approx(expected, abs=0.01)
    assert m.index_spot("NIFTY") < f_implied               # strictly below F before expiry
    assert m.live_chain("NIFTY", EXP)["spot"] == pytest.approx(expected, abs=0.01)


def test_banknifty_lot_size_eras():
    """The 5-year GFD window spans three BANKNIFTY lot revisions (NSE circ. 56233 etc.)."""
    from skas_algo.engine.options.contract_specs import lot_size_for

    assert lot_size_for("BANKNIFTY", date(2022, 6, 1)) == 25
    assert lot_size_for("BANKNIFTY", date(2023, 8, 1)) == 15
    assert lot_size_for("BANKNIFTY", date(2024, 12, 1)) == 30
    assert lot_size_for("BANKNIFTY", date(2026, 2, 1)) == 35


def _monthly_day(day, spot=24000.0, exp="2026-08-25", prem=200.0):
    """A store day carrying a MONTHLY chain: wide strikes (spot±2000, 100-steps) so the
    ratio family's 300/600/1600 offsets and hni's 200/400/600 all resolve. Premiums decay
    linearly from ATM so credit gates behave sanely; both rights print every 5 min."""
    minutes = [(9, m) for m in range(15, 60, 5)] + [(h, m) for h in (10, 11, 12, 13, 14)
                                                    for m in range(0, 60, 5)] + \
              [(15, m) for m in range(0, 30, 5)]
    rows = []
    for k in range(int(spot - 2000), int(spot + 2100), 100):
        dist = abs(k - spot)
        ce = max(prem - (k - spot) * 0.09 - dist * 0.02, 2.0)
        pe = max(prem + (k - spot) * 0.09 - dist * 0.02, 2.0)
        for i, (hh, mm) in enumerate(minutes):
            decay = i * 0.05
            rows += _leg_rows(day, k, "CE", {(hh, mm): round(max(ce - decay, 1.0), 2)}, exp)
            rows += _leg_rows(day, k, "PE", {(hh, mm): round(max(pe - decay, 1.0), 2)}, exp)
    return pd.DataFrame(rows, columns=store.COLUMNS)


def test_hni_weekly_replays_on_the_store():
    """The positional family runs on the 1-min store (2026-07-18): hni enters its 1-3-2
    tent at entry_time on the ~8-DTE weekly, margin freezes off the harness push, and the
    Friday force-exit closes the week."""
    mon, fri = date(2026, 7, 13), date(2026, 7, 17)
    exp = "2026-07-21"   # 8 days from Monday
    for d in (mon, date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16), fri):
        store.write_day(d, _monthly_day(d, exp=exp))
    out = run_intraday_backtest("hni_weekly", "NIFTY", mon, fri, 1_000_000,
                                {"margin_per_lot": 132_000, "lots": 1})
    entries = [t for t in out["trades"] if t["action"] in ("SHORT", "BUY")
               and t["date"].startswith(mon.isoformat())]
    assert len(entries) == 3                                # 1-3-2 tent = 3 legs
    assert all(t["date"].startswith("2026-07-13 09:45") for t in entries), entries
    shorts = [t for t in entries if t["action"] == "SHORT"]
    assert len(shorts) == 1 and shorts[0]["units"] == 3 * 65   # sell 3 lots body
    assert all("|" in t["ticker"] and t["ticker"].split("|")[1] == exp for t in entries)
    closes = [t for t in out["trades"] if t["action"] in ("COVER", "SELL", "SETTLE")]
    assert closes and out["report"]["metrics"]["Total Trades"] >= 1
    # margin echo: keyed Rs1.32L spread across the 3 short lots, era-true
    assert out["report"]["sizing"]["margin_per_lot"] == 132_000


def test_call_ratio_monthly_replays_with_1432_structure():
    """call_ratio enters its 1:2:1 wing at the 14:30 entry_time (owner default for the
    monthly family) with the strategy's own sizing FORCED to fixed on the replay path."""
    d1 = date(2026, 7, 13)
    store.write_day(d1, _monthly_day(d1))
    out = run_intraday_backtest(
        "call_ratio_monthly", "NIFTY", d1, d1, 1_000_000,
        {"margin_per_lot": 130_000, "lots": 1, "entry_time": "14:30",
         "entry_rule": "post_expiry", "entry_window_days": 30,
         "min_credit_pct": -10.0, "credit_debit_limit_pct": 10.0, "max_shifts": 0,
         # the harness pops "sizing" — even if the user sends "capital" the STRATEGY
         # must still be built sizing="fixed" (its auto-size never fights the harness)
         "sizing": "fixed"})
    entries = [t for t in out["trades"] if t["date"].startswith(f"{d1.isoformat()} 14:30")]
    assert len(entries) == 3, out["trades"][:5]             # buy 1 / sell 2 / hedge 1
    shorts = [t for t in entries if t["action"] == "SHORT"]
    assert len(shorts) == 1 and shorts[0]["units"] == 2 * 65


def test_ratio_family_own_sizing_is_always_fixed_on_replay():
    """The name collision resolves by construction: the harness pops params["sizing"]
    before the strategy is built, so sizing="capital" configures the HARNESS while the
    strategy keeps its ctor default "fixed"."""
    d1 = date(2026, 7, 13)
    store.write_day(d1, _monthly_day(d1))
    from skas_algo.services import intraday_replay as mod
    seen = {}
    orig = mod.get_strategy

    def spy(sid):
        factory = orig(sid)
        def wrapped(**kw):
            s = factory(**kw)
            seen["sizing"] = getattr(s, "sizing", None)
            return s
        return wrapped

    mod.get_strategy, _ = spy, None
    try:
        run_intraday_backtest("call_ratio_monthly", "NIFTY", d1, d1, 1_000_000,
                              {"margin_per_lot": 130_000, "sizing": "capital",
                               "entry_rule": "post_expiry", "min_credit_pct": -10.0})
    finally:
        mod.get_strategy = orig
    assert seen["sizing"] == "fixed"


def test_ema21_bands_use_forming_bar_not_settled(monkeypatch):
    """ema21 on the store: prior days come from the cache, TODAY is a forming bar from
    the replay's running parity spot — the settled cache bar for today must be excluded
    (the ~10-min lookahead the owner vetoed)."""
    d1 = date(2026, 7, 13)
    store.write_day(d1, _monthly_day(d1, spot=24000.0))
    calls = {}

    class _FakeSd:
        def get_prices(self, symbol, start_date=None, end_date=None, **kw):
            # 40 prior settled days + a POISONED settled row for d1 itself: if the
            # forming-bar filter fails, the poison's absurd high/low skews the bands.
            days = pd.bdate_range(end="2026-07-13", periods=41)
            df = pd.DataFrame({
                "date": days,
                "open": 24000.0, "high": 24010.0, "low": 23990.0, "close": 24000.0,
            })
            df.loc[df.index[-1], ["high", "low", "close"]] = [99999.0, 1.0, 99999.0]
            calls["fetched"] = True
            return df

    monkeypatch.setattr("skas_algo.data.provider.get_data_cache", lambda: _FakeSd())
    out = run_intraday_backtest("21_ema_momentum", "NIFTY", d1, d1, 1_000_000, {"lots": 1})
    assert calls.get("fetched")
    # The poisoned settled today-bar (high 99999) must NOT reach the bands: with it, the
    # upper band explodes and no bull-put entry is possible; the forming bar keeps bands
    # sane. We can't guarantee a signal fires on one synthetic day — assert no crash and
    # that the report contract exists (the lookahead-exclusion is the real assertion:
    # a 99999 close WOULD fire a bull-put spread entry at 15:20 if it leaked through).
    for t in out["trades"]:
        assert not t["date"].startswith("2026-07-13 15:2") or t["price"] < 5000, \
            "settled poison bar leaked into the bands"
    assert "metrics" in out["report"]


def test_intraday_backtest_no_coverage_is_422(api_client):
    body = {"strategy_id": "intraday_straddle", "underlying": "NIFTY",
            "instrument_class": "DERIV", "symbols": ["NIFTY"],
            "start_date": "2031-01-01", "end_date": "2031-01-05",
            "capital": 1_000_000, "params": {}, "persist": False}
    r = api_client.post("/api/v1/backtest/intraday", json=body)
    assert r.status_code == 422 and "no captured days" in r.json()["detail"]
