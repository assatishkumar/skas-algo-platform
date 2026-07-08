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


def test_previous_trading_day_skips_weekends_and_holidays():
    _clear_holiday_cache()
    # 2026-07-08 (Wed) → 07-07 (Tue, ordinary)
    assert holidays.previous_trading_day(datetime(2026, 7, 8).date()) == datetime(2026, 7, 7).date()
    # Monday 2026-01-05 → previous Friday 2026-01-02 (skips the weekend)
    assert holidays.previous_trading_day(datetime(2026, 1, 5).date()) == datetime(2026, 1, 2).date()
    # 2026-06-29 (Mon) → 06-25 (Thu): skips 06-26 (Muharram) + the weekend
    assert holidays.previous_trading_day(datetime(2026, 6, 29).date()) == datetime(2026, 6, 25).date()


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


def _make_sqlite(path):
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE t (id INTEGER)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()


def test_backup_offbox_push_ships_snapshot(tmp_path, monkeypatch):
    from skas_algo.config import get_settings
    from skas_algo.services.backup import backup_db

    db = tmp_path / "s.db"
    _make_sqlite(db)
    offbox = tmp_path / "offbox"
    offbox.mkdir()
    monkeypatch.setattr(get_settings(), "backup_remote_cmd", f"cp {{path}} {offbox}/")

    # offbox=True → the fresh snapshot is shipped by the configured command.
    p = backup_db(database_url=f"sqlite:///{db}", keep=3, offbox=True)
    shipped = list(offbox.glob("s-*.db"))
    assert p is not None and len(shipped) == 1 and shipped[0].name == p.name

    # offbox=False (startup path) → not shipped even when the command is set.
    backup_db(database_url=f"sqlite:///{db}", keep=3, offbox=False)
    assert len(list(offbox.glob("*.db"))) == 1


def test_backup_offbox_failure_is_best_effort(tmp_path, monkeypatch):
    from skas_algo.config import get_settings
    from skas_algo.services.backup import backup_db

    db = tmp_path / "s.db"
    _make_sqlite(db)
    monkeypatch.setattr(get_settings(), "backup_remote_cmd", "false")  # command exits nonzero

    # The local snapshot still succeeds despite the off-box command failing.
    p = backup_db(database_url=f"sqlite:///{db}", keep=3, offbox=True)
    assert p is not None and p.exists()


def test_backup_no_offbox_when_unconfigured(tmp_path, monkeypatch):
    from skas_algo.config import get_settings
    from skas_algo.services.backup import backup_db

    db = tmp_path / "s.db"
    _make_sqlite(db)
    monkeypatch.setattr(get_settings(), "backup_remote_cmd", None)
    assert backup_db(database_url=f"sqlite:///{db}", keep=3, offbox=True) is not None  # no-op push


# --- Part 3: daily background cache refresh + quiet indication ---

def test_daily_refresh_symbols_indices_plus_equity():
    from skas_algo.live.manager import LiveRunManager

    m = LiveRunManager()
    m.runs[1] = SimpleNamespace(config=SimpleNamespace(instrument_class="STOCK",
                                                       symbols=["RELIANCE", "TCS"]))
    m.runs[2] = SimpleNamespace(config=SimpleNamespace(instrument_class="DERIV",
                                                       symbols=["NIFTY"]))
    syms = m._daily_refresh_symbols()
    assert "NIFTY 50" in syms and "NIFTY BANK" in syms   # index spots, always
    assert "RELIANCE" in syms and "TCS" in syms          # the equity run's universe
    assert "NIFTY" not in syms                            # a DERIV underlying isn't a daily series


def _freeze_now(monkeypatch, when):
    from skas_algo.live import manager as mgr
    monkeypatch.setattr(mgr, "datetime",
                        type("_D", (), {"now": staticmethod(lambda tz=None: when)}))


def test_daily_cache_refresh_runs_once_and_broadcasts(monkeypatch):
    import asyncio
    from datetime import datetime

    from skas_algo.live.manager import IST, LiveRunManager

    m = LiveRunManager()
    _freeze_now(monkeypatch, datetime(2026, 7, 8, 10, 0, tzinfo=IST))  # Wed, trading day
    monkeypatch.setattr(m, "_run_cache_refresh",
                        lambda s: {"NIFTY 50": {"rows": 5}, "NIFTY BANK": {"error": "x"}})
    published: list = []
    monkeypatch.setattr(m.broadcaster, "publish", lambda msg: published.append(msg))

    asyncio.run(m._maybe_daily_cache_refresh())
    assert m.last_cache_refresh["ok"] == 1 and m.last_cache_refresh["errors"] == 1
    assert published[-1]["type"] == "cache_refreshed"
    assert m._last_cache_refresh_day == datetime(2026, 7, 8).date()

    published.clear()                                     # second call same day → no-op
    asyncio.run(m._maybe_daily_cache_refresh())
    assert published == []


def test_daily_cache_refresh_skips_weekend_and_retries_without_session(monkeypatch):
    import asyncio
    from datetime import datetime

    from skas_algo.live.manager import IST, LiveRunManager

    # Saturday → never even attempts
    m = LiveRunManager()
    _freeze_now(monkeypatch, datetime(2026, 7, 11, 10, 0, tzinfo=IST))
    called: list = []
    monkeypatch.setattr(m, "_run_cache_refresh", lambda s: called.append(1) or {})
    asyncio.run(m._maybe_daily_cache_refresh())
    assert called == [] and m.last_cache_refresh is None

    # trading day but no valid session (→ None) → no broadcast, flag stays unset (retries)
    m2 = LiveRunManager()
    _freeze_now(monkeypatch, datetime(2026, 7, 8, 10, 0, tzinfo=IST))
    monkeypatch.setattr(m2, "_run_cache_refresh", lambda s: None)
    pub: list = []
    monkeypatch.setattr(m2.broadcaster, "publish", lambda msg: pub.append(msg))
    asyncio.run(m2._maybe_daily_cache_refresh())
    assert pub == [] and m2._last_cache_refresh_day is None
