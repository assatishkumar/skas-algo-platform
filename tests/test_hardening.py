"""P0 hardening tests: NSE holidays, market-open gate, DB backup + retention,
rate-governor timing, and the loop watchdog."""

from __future__ import annotations

import sqlite3
import time as _time
from datetime import datetime, time
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


def test_market_close_is_per_segment_since_cas():
    """SEBI's Closing Auction Session (2026-08-03) pushed index F&O to 15:40 while equity
    cash stayed at 15:30 — so this can't be one number. Measured off our own 1-min option
    store: the last minute-bar starts 15:29 through 2026-07-31 and 15:39 from 08-03 on.

    The EQUITY default is deliberate: any caller that doesn't opt in keeps the pre-CAS
    behaviour rather than silently gaining ten minutes of order window."""
    from skas_algo.live.quotes import session_close

    _clear_holiday_cache()
    assert session_close("EQUITY") == time(15, 30)
    assert session_close("DERIV") == time(15, 40)
    assert session_close() == session_close("EQUITY")  # default = the narrow window

    mon = datetime(2026, 1, 5, 15, 35, tzinfo=IST)  # inside F&O, past equity cash
    assert is_market_open(mon, segment="DERIV")
    assert not is_market_open(mon, segment="EQUITY")
    assert not is_market_open(mon)  # unqualified callers keep the 15:30 close

    # 15:30 is the last equity minute; 15:40 the last F&O one; a second later, closed.
    assert is_market_open(mon.replace(hour=15, minute=30), segment="EQUITY")
    assert is_market_open(mon.replace(hour=15, minute=40), segment="DERIV")
    assert not is_market_open(mon.replace(hour=15, minute=40, second=1), segment="DERIV")

    # Segment never overrides the calendar.
    holiday = datetime(2026, 1, 26, 15, 35, tzinfo=IST)
    assert not is_market_open(holiday, segment="DERIV")


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


def test_backup_offbox_dir_copies_and_prunes_per_series(tmp_path, monkeypatch):
    """The native directory destination (Google Drive folder): the nightly snapshot lands
    GZIPPED under its FINAL name (no tmp+rename — Drive started uploading the .tmp and the
    rename orphaned every nightly into lost_and_found, fixed 2026-07-30), then EACH series
    is pruned to backup_offbox_keep — per-series, so the VPS's scp'd ``vps-*`` snapshots
    get their own retention instead of competing with the Mac's; pre-gzip raw ``.db``
    history prunes within the same series (keep=2 here for brevity)."""
    import gzip

    from skas_algo.config import get_settings
    from skas_algo.services.backup import backup_db

    db = tmp_path / "s.db"
    _make_sqlite(db)
    offbox = tmp_path / "drive"
    offbox.mkdir()
    # Pre-existing history: 3 old raw Mac-series snapshots + 2 VPS-series ones.
    for stamp in ("20260701-163000-000001", "20260702-163000-000001", "20260703-163000-000001"):
        (offbox / f"s-{stamp}.db").write_bytes(b"old")
    for stamp in ("20260701-163000-000002", "20260702-163000-000002"):
        (offbox / f"vps-s-{stamp}.db").write_bytes(b"vps")
    (offbox / "not-a-snapshot.db").write_bytes(b"keep me")  # no stamp → never touched

    monkeypatch.setattr(get_settings(), "backup_remote_cmd", None)
    monkeypatch.setattr(get_settings(), "backup_offbox_dir", str(offbox))
    monkeypatch.setattr(get_settings(), "backup_offbox_keep", 2)

    p = backup_db(database_url=f"sqlite:///{db}", keep=3, offbox=True)
    assert p is not None
    mac = sorted(f.name for f in offbox.glob("s-*.db*"))
    assert len(mac) == 2 and mac[-1] == p.name + ".gz"  # newest 2 kept, fresh gz included
    # The shipped copy is a faithful gzip of the snapshot (restore = gunzip).
    assert gzip.decompress((offbox / (p.name + ".gz")).read_bytes()) == p.read_bytes()
    assert len(list(offbox.glob("vps-s-*.db"))) == 2    # other series untouched by our prune
    assert (offbox / "not-a-snapshot.db").exists()      # non-snapshot .db never pruned
    assert not list(offbox.glob("*.tmp"))               # nothing staged in the synced folder

    # keep=0 → append-only (the pre-2026-07-27 behaviour).
    monkeypatch.setattr(get_settings(), "backup_offbox_keep", 0)
    backup_db(database_url=f"sqlite:///{db}", keep=3, offbox=True)
    assert len(list(offbox.glob("s-*.db*"))) == 3       # 2 kept + 1 new, nothing pruned


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


def test_last_backup_at_reads_newest_stamp(tmp_path):
    """The nightly latch survives restarts by trusting on-disk stamps (an evening
    restart used to re-fire the nightly + offbox ship on every boot, 2026-07-30)."""
    from skas_algo.services.backup import last_backup_at

    db = tmp_path / "s.db"
    _make_sqlite(db)
    url = f"sqlite:///{db}"
    assert last_backup_at(url) is None  # no backups dir yet

    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "s-20260728-083012-000001.db").write_bytes(b"startup")
    (backups / "s-20260728-163025-000001.db").write_bytes(b"nightly")
    (backups / "s-20260727-163025-000001.db").write_bytes(b"older day")
    (backups / "not-a-snapshot.db").write_bytes(b"ignored")

    at = last_backup_at(url)
    assert at is not None
    assert (at.year, at.month, at.day, at.hour, at.minute) == (2026, 7, 28, 16, 30)
