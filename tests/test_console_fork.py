"""Fork a deployment's cycle into the console (owner ask 2026-09-18): the run's actual fills
become a console journal on the cycle's entry day, "fork here" drops the actual later
fills, and the state carries the actual outcome for the comparison strip. Synthetic store
in tmp, a paper run in the isolated test DB — no network, no broker."""

from __future__ import annotations

from datetime import date

import pytest

from skas_algo.data import option_intraday_store as store
from skas_algo.services import console_fork
from skas_algo.services.options_console import registry
from skas_algo.services.options_console.session import ConsoleSession
from tests.test_options_console import DAY, EXP, _day

CE = f"NIFTY|{EXP}|24000|CE"
PE = f"NIFTY|{EXP}|24000|PE"
WING = f"NIFTY|{EXP}|24100|CE"
D2, D3 = date(2026, 7, 15), date(2026, 7, 16)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    from skas_algo.config import get_settings
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)
    monkeypatch.setenv("SKAS_CONSOLE_DIR", str(tmp_path / "console"))
    registry.clear()
    for d in (DAY, D2, D3):
        store.write_day(d, _day(d))
    yield
    registry.clear()


def _t(when, ticker, action, units, price, **kw):
    """A trade row as `GET /live/{id}/trades` serves it (naive IST, space-separated)."""
    row = {"date": when, "ticker": ticker, "action": action, "units": units, "price": price,
           "amount": units * price, "profit": kw.pop("profit", 0.0), "pnl_pct": 0.0,
           "lots": units // 65, "tag": "STRATEGY", "charge": kw.pop("charge", 10.0)}
    row.update(kw)
    return row


TRADES = [
    _t("2026-07-14 10:00", CE, "SHORT", 65, 150.0, underlying_spot=24000.0),
    _t("2026-07-14 10:00", PE, "SHORT", 65, 152.0, underlying_spot=24000.0),
    _t("2026-07-15 07:45", WING, "BUY", 65, 105.0, tag="MANUAL"),        # before the open
    _t("2026-07-15 09:20", WING, "AVG_BUY", 65, 106.0, tag="MANUAL"),   # a second buy
    _t("2026-07-15 09:30", "BANKNIFTY|2026-07-30|52000|CE", "SHORT", 30, 300.0),  # not ours
    _t("2026-07-16 11:30", CE, "COVER", 65, 140.0, profit=650.0, underlying_spot=24050.0),
    _t("2026-07-16 11:30", PE, "COVER", 65, 160.0, profit=-520.0, underlying_spot=24050.0),
    _t("2026-07-16 11:30", WING, "SELL", 130, 110.0, profit=585.0),
    _t("2026-07-16 15:30", CE, "SETTLE", 65, 0.0),                      # the market's action
]
CYCLE = {"index": 3, "entry_date": "2026-07-14 10:00", "exit_date": "2026-07-16 11:30",
         "exit_reason": "target", "net_pnl": 715.0, "live": False, "underlying": "NIFTY",
         "expiry": EXP, "legs_detail": [{"symbol": CE}, {"symbol": PE}, {"symbol": WING}]}


def test_the_runs_fills_become_a_console_journal_with_the_rows_it_cannot_take_noted():
    cyc = console_fork.normalise_cycle(CYCLE)
    assert cyc["entered_at"] == "2026-07-14T10:00" and cyc["exited_at"] == "2026-07-16T11:30"
    assert cyc["symbols"] == sorted([CE, PE, WING]) and cyc["net"] == 715.0
    rows, notes = console_fork.cycle_to_journal(TRADES, cyc)
    assert [r["action"] for r in rows] == ["SHORT", "SHORT", "BUY", "BUY", "COVER", "COVER", "SELL"]
    assert all("|" in r["symbol"] and r["symbol"].startswith("NIFTY") for r in rows)
    # a fill before the open is clamped to 09:15 and said so; AVG_BUY is a BUY; SETTLE is gone
    assert rows[2]["at"] == "2026-07-15T09:15" and notes["clamped"] == [
        {"symbol": WING, "from": "2026-07-15T07:45", "to": "2026-07-15T09:15"}]
    assert notes["dropped_settle"] == 1
    # one undo group per distinct minute, so a basket undoes together
    assert [r["group"] for r in rows] == [1, 1, 2, 3, 4, 4, 4]
    assert rows[0]["charges"] == 10.0 and rows[0]["spot"] == 24000.0
    # an ISO stamp with the offset (AlgoRun.state's form) reads the same minute
    assert console_fork._minute("2026-07-14T10:00:07+05:30") == "2026-07-14T10:00"
    with pytest.raises(ValueError, match="unknown trade action"):
        console_fork.cycle_to_journal([_t("2026-07-14 10:00", CE, "FLATTEN", 65, 1.0)], cyc)


def test_the_spec_opens_on_the_entry_day_at_the_entry_minute_with_the_actual_path():
    spec = console_fork.fork_spec(cycle=console_fork.normalise_cycle(CYCLE), trades=TRADES,
                                  capital=500_000, source="local", run_id=7, label="#7 hni")
    assert spec["day"] == DAY.isoformat() and spec["at"] == "10:00" and spec["expiry"] == EXP
    f = spec["fork"]
    assert f["actual"] == {"net": 715.0, "fills": 7, "entered_at": "2026-07-14T10:00",
                           "exited_at": "2026-07-16T11:30", "exit_reason": "target",
                           "live": False}
    assert f["entry_day_uncaptured"] is False and f["uncaptured"] == [] and f["forked_at"] is None
    # the actual per-minute MTM, in order, over the store (the Simulator's walker)
    ms = [m for m, _ in f["actual_series"]]
    assert ms and ms == sorted(ms) and ms[0] >= "2026-07-14T10:00" and ms[-1] <= "2026-07-16T11:30"
    # an entry on an uncaptured day opens on the first captured day at the open, and says so
    early = dict(CYCLE, entry_date="2026-07-13 10:00")
    trades = [dict(t, date=t["date"].replace("2026-07-14", "2026-07-13")) for t in TRADES]
    spec = console_fork.fork_spec(cycle=console_fork.normalise_cycle(early), trades=trades,
                                  capital=1, source="local", run_id=7, label="x")
    assert spec["day"] == DAY.isoformat() and spec["at"] == "09:15"
    assert spec["fork"]["entry_day_uncaptured"] is True
    # nothing captured after the entry → refused, naming the store's range
    late = dict(CYCLE, entry_date="2026-08-01 10:00", exit_date=None)
    trades = [dict(t, date=t["date"].replace("2026-07-14", "2026-08-01")) for t in TRADES[:2]]
    with pytest.raises(ValueError, match="not in the 1-min store"):
        console_fork.fork_spec(cycle=console_fork.normalise_cycle(late), trades=trades,
                               capital=1, source="local", run_id=7, label="x")


def _forked() -> ConsoleSession:
    spec = console_fork.fork_spec(cycle=console_fork.normalise_cycle(CYCLE), trades=TRADES,
                                  capital=500_000, source="local", run_id=7, label="#7 hni")
    s = ConsoleSession(underlying="NIFTY", day=date.fromisoformat(spec["day"]), at=spec["at"],
                       expiry=spec["expiry"], capital=spec["capital"])
    s.restore(spec["restore"]["journal"], fork=spec["fork"])
    return s


def test_fork_here_drops_the_actual_fills_ahead_and_stepping_forward_no_longer_brings_them_back():
    s = _forked()
    st = s.state()
    assert st["fork"]["run_id"] == 7 and st["fork"]["actual"]["net"] == 715.0
    assert {leg["symbol"] for leg in st["legs"]} == {CE, PE}          # the entry, at 10:00
    s.shift_day(1)                                                     # 15 Jul: the wing lands
    s.seek("10:00")
    assert {leg["symbol"] for leg in s.state()["legs"]} == {CE, PE, WING}
    ahead = [f for f in s.journal if f["at"] > "2026-07-15T10:00"]
    assert len(ahead) == 3                                             # the 16 Jul exits
    assert s.truncate_after_cursor() == 3
    assert s.fork["forked_at"] == "2026-07-15T10:00"
    d = s.discarded[-1]
    assert d["reason"] == "fork" and d["group"] is None and len(d["rows"]) == 3
    assert s.truncate_after_cursor() == 0                              # nothing ahead now
    s.shift_day(1)                                                     # 16 Jul, past the exits
    s.seek("11:35")
    assert {leg["symbol"] for leg in s.state()["legs"]} == {CE, PE, WING}   # NOT closed
    # the owner's own action afterwards undoes on its own — never an actual fill
    n = len(s.journal)
    s.stage(kind="add", right="PE", strike=24100, side="B", lots=1)
    assert len(s.journal) == n + 1
    assert s.undo_last() and len(s.journal) == n and s.fork["forked_at"] == "2026-07-15T10:00"


def test_a_fork_survives_the_registry_and_the_save_payload():
    spec = console_fork.fork_spec(cycle=console_fork.normalise_cycle(CYCLE), trades=TRADES,
                                  capital=500_000, source="local", run_id=7, label="#7 hni")
    s = registry.create(underlying="NIFTY", day=date.fromisoformat(spec["day"]), at=spec["at"],
                        expiry=spec["expiry"], capital=spec["capital"])
    s.restore(spec["restore"]["journal"], fork=spec["fork"])
    s.state()                                                          # writes through
    assert s.save_payload()["fork"]["actual"]["net"] == 715.0
    sid = s.id
    registry.clear()
    back = registry.get(sid)                                           # rebuilt from disk
    assert back.fork["run_id"] == 7 and back.state()["fork"]["actual"]["fills"] == 7
    assert {leg["symbol"] for leg in back.state()["legs"]} == {CE, PE}


def _paper_run(db, trades, metrics=None):
    from skas_algo.db.enums import InstrumentClass, TradingMode
    from skas_algo.db.models import Algo, AlgoRun

    algo = Algo(name="hni fork", strategy_id="hni_weekly", mode=TradingMode.PAPER,
                instrument_class=InstrumentClass.DERIV, capital=750_000,
                params={"underlying": "NIFTY"})
    db.add(algo)
    db.flush()
    run = AlgoRun(algo_id=algo.id, mode=TradingMode.PAPER, trade_log=trades,
                  metrics=metrics or {}, params_snapshot={"underlying": "NIFTY"})
    db.add(run)
    db.flush()
    return run.id


def test_the_fork_route_indexes_the_same_list_the_cycle_page_does(client):
    """A RUNNING run's cycles are reconstructed newest-first; a run with a stored report
    serves the report's cycles OLDEST-first. Whichever it is, `GET /runs/{id}/cycles` and
    `POST /console/sessions/fork` read one list (`run_cycles`), so the index the page
    links is the cycle that opens."""
    from skas_algo.db.base import session_scope

    nifty = [t for t in TRADES if t["action"] != "SETTLE" and t["ticker"].startswith("NIFTY")]
    later = [_t("2026-07-16 12:00", CE, "SHORT", 65, 120.0, underlying_spot=24040.0),
             _t("2026-07-16 12:30", CE, "COVER", 65, 118.0, profit=130.0)]
    with session_scope() as db:
        rid = _paper_run(db, nifty + later)
    cyc = client.get(f"/api/v1/runs/{rid}/cycles").json()
    assert cyc["is_deployment"] is True and cyc["capital"] == 750_000
    briefs = cyc["cycles"]
    assert [b["entered_at"] for b in briefs] == ["2026-07-16 12:00", "2026-07-14 10:00"]  # newest first
    assert briefs[1]["symbols"] == sorted([CE, PE, WING]) and briefs[1]["n_legs"] == 3
    r = client.post("/api/v1/console/sessions/fork", json={"run_id": rid, "index": 1})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["fork"]["entered_at"] == "2026-07-14T10:00" and st["fork"]["index"] == 1
    assert st["session"]["date"] == DAY.isoformat() and st["session"]["clock"] == "10:00"
    assert st["session"]["capital"] == 750_000 and st["fork"]["label"].startswith(f"#{rid} ")
    assert {leg["symbol"] for leg in st["legs"]} == {CE, PE}
    # fork here through the route
    r2 = client.post(f"/api/v1/console/sessions/{st['session']['id']}/fork")
    assert r2.status_code == 200 and r2.json()["discarded"][-1]["reason"] == "fork"
    # a stored report (a stopped run) lists its cycles oldest-first — and the fork follows it
    report_cycles = [dict(CYCLE, entry_date="2026-07-14 10:00"),
                     {"entry_date": "2026-07-16 12:00", "exit_date": "2026-07-16 12:30",
                      "underlying": "NIFTY", "expiry": EXP, "net_pnl": 130.0,
                      "legs_detail": [{"symbol": CE}]}]
    with session_scope() as db:
        rid2 = _paper_run(db, nifty + later, metrics={"options": {"cycles": report_cycles}})
    briefs2 = client.get(f"/api/v1/runs/{rid2}/cycles").json()["cycles"]
    assert [b["entered_at"] for b in briefs2] == ["2026-07-14 10:00", "2026-07-16 12:00"]
    st2 = client.post("/api/v1/console/sessions/fork", json={"run_id": rid2, "index": 0}).json()
    assert st2["fork"]["entered_at"] == "2026-07-14T10:00"
    assert client.post("/api/v1/console/sessions/fork",
                       json={"run_id": rid2, "index": 9}).status_code == 404
    # the picker: local runs with cycles; no peer configured
    src = client.get("/api/v1/console/fork-sources").json()
    assert {r["run_id"] for r in src["local"]} >= {rid, rid2}
    assert src["peer"] == {"configured": False, "url": None, "ok": False, "error": None,
                           "runs": []}
