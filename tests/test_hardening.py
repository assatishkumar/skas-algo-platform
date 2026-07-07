"""P0 hardening tests: NSE holidays, market-open gate, DB backup + retention,
rate-governor timing, and the loop watchdog."""

from __future__ import annotations

import sqlite3
import time as _time
from datetime import datetime
from types import SimpleNamespace

from skas_algo.live import holidays
from skas_algo.live.quotes import IST, is_market_open


def _clear_holiday_cache():
    holidays._holidays_for.cache_clear()


def test_is_nse_holiday_builtin_and_non_holiday():
    _clear_holiday_cache()
    assert holidays.is_nse_holiday(datetime(2026, 1, 26).date())   # Republic Day
    assert holidays.is_nse_holiday(datetime(2026, 4, 3).date())    # Good Friday
    assert not holidays.is_nse_holiday(datetime(2026, 1, 5).date())   # ordinary Monday
    assert not holidays.is_nse_holiday(datetime(2026, 7, 7).date())   # ordinary Tuesday
    assert holidays.holiday_name(datetime(2026, 1, 26).date()) == "Republic Day"


def test_holiday_env_overrides(monkeypatch):
    monkeypatch.setenv("NSE_HOLIDAYS_ADD", "2026-07-08")
    monkeypatch.setenv("NSE_HOLIDAYS_REMOVE", "2026-01-26")
    _clear_holiday_cache()
    try:
        assert holidays.is_nse_holiday(datetime(2026, 7, 8).date())      # added
        assert not holidays.is_nse_holiday(datetime(2026, 1, 26).date())  # force-opened
    finally:
        _clear_holiday_cache()


def test_is_market_open_excludes_holidays():
    _clear_holiday_cache()
    open_day = datetime(2026, 1, 5, 11, 0, tzinfo=IST)     # Mon, session hours
    holiday = datetime(2026, 1, 26, 11, 0, tzinfo=IST)     # Republic Day (a Monday)
    weekend = datetime(2026, 1, 3, 11, 0, tzinfo=IST)      # Saturday
    before = datetime(2026, 1, 5, 9, 0, tzinfo=IST)        # pre-open
    assert is_market_open(open_day)
    assert not is_market_open(holiday)
    assert not is_market_open(weekend)
    assert not is_market_open(before)


def test_backup_writes_and_prunes(tmp_path):
    from skas_algo.services.backup import backup_db

    db = tmp_path / "sample.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    url = f"sqlite:///{db}"
    made = [backup_db(database_url=url, keep=2) for _ in range(3)]
    assert all(p is not None for p in made)          # every call produced a snapshot
    assert not made[0].exists()                       # ...and the oldest was pruned

    snaps = sorted((tmp_path / "backups").glob("sample-*.db"))
    assert len(snaps) == 2  # retention kept the newest 2

    # A snapshot is a real, queryable copy.
    c2 = sqlite3.connect(str(snaps[-1]))
    assert c2.execute("SELECT id FROM t").fetchone()[0] == 1
    c2.close()


def test_backup_skips_non_sqlite():
    from skas_algo.services.backup import backup_db

    assert backup_db(database_url="postgresql+psycopg://u:p@h/db") is None


def test_rate_governor_paces_without_stacking_sleeps():
    from skas_algo.brokers.live_broker import _RateGovernor

    gov = _RateGovernor(min_interval_s=0.05)
    t0 = _time.monotonic()
    gov.wait()          # first call: no wait
    gov.wait()          # second: ~one interval later
    gov.wait()          # third: ~two intervals
    elapsed = _time.monotonic() - t0
    # Three paced slots ≈ 2 × interval; generous upper bound guards against regressions
    # where the sleep-under-lock bug would stack to ≥ 3 × interval under contention.
    assert 0.09 <= elapsed <= 0.20


def test_watchdog_restarts_dead_auto_run(monkeypatch):
    from skas_algo.live.manager import LiveRunManager

    m = LiveRunManager()
    live = SimpleNamespace(config=SimpleNamespace(auto=True, name="RunX"))
    m.runs[42] = live  # type: ignore[assignment]
    m._tasks[42] = SimpleNamespace(done=lambda: True)  # a dead task

    restarted: list[int] = []
    monkeypatch.setattr(m, "_start_loop_on_loop", lambda rid: restarted.append(rid))
    monkeypatch.setattr(m, "_notify_watchdog", lambda _live: None)

    m._watchdog_scan()
    assert restarted == [42]

    # A non-auto run is never auto-restarted by the watchdog.
    restarted.clear()
    live.config.auto = False
    m._watchdog_scan()
    assert restarted == []
