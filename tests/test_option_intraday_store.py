"""option_intraday_store: the duckdb-Parquet round-trip (FIRST — everything builds on it),
capture_day universe filtering / oi capture / skip-if-exists / error tolerance, 1→5-min
resampling, cross-day contract loads, and the manager's daily-capture gates — fake
adapter/kite, no network, store redirected to tmp."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pandas as pd
import pytest

from skas_algo.data import option_intraday_store as store

DAY = date(2026, 7, 15)          # Wednesday
EXP_NEAR = "2026-07-21"          # +6d → in the 40d window
EXP_FAR = "2026-10-27"           # +104d → out


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    monkeypatch.setattr(store, "_THROTTLE_S", 0.0)
    # NEVER let a test mirror into the operator's REAL backup dir (.env leaks into
    # get_settings here — a sweep test once wrote a stray file into Google Drive).
    from skas_algo.config import get_settings
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)


def _df(rows):
    return pd.DataFrame(rows, columns=store.COLUMNS)


def _row(sym, hh, mm, px, vol=100.0, oi=5000.0):
    return {"symbol": sym, "start": datetime(2026, 7, 15, hh, mm), "open": px, "high": px + 1,
            "low": px - 1, "close": px + 0.5, "volume": vol, "oi": oi}


# ------------------------------------------------------------- parquet round-trip
def test_parquet_round_trip_via_duckdb():
    sym = "NIFTY|2026-07-21|24000|CE"
    df = _df([_row(sym, 9, 15, 100.0), _row(sym, 9, 16, 101.0)])
    store.write_day(DAY, df)
    path = store.day_path(DAY)
    assert path.exists() and path.suffix == ".parquet"
    assert not path.with_suffix(".parquet.tmp").exists()  # atomic tmp was renamed away
    back = store.load_day(DAY)
    assert len(back) == 2
    assert list(back["symbol"]) == [sym, sym]
    assert list(back["close"]) == [100.5, 101.5]
    assert list(back["volume"]) == [100.0, 100.0] and list(back["oi"]) == [5000.0, 5000.0]
    assert store.captured_days() == [DAY.isoformat()]


def test_load_missing_day_is_empty():
    assert store.load_day(DAY).empty and store.captured_days() == []


# ------------------------------------------------------------------ resampling
def test_resample_1min_to_5min_sparse():
    sym = "X|2026-07-21|100|CE"
    df = pd.DataFrame([
        # 09:15 bucket: 09:15 + 09:17 traded
        {"start": datetime(2026, 7, 15, 9, 15), "open": 10.0, "high": 12.0, "low": 9.0,
         "close": 11.0, "volume": 100.0, "oi": 500.0},
        {"start": datetime(2026, 7, 15, 9, 17), "open": 11.0, "high": 15.0, "low": 11.0,
         "close": 14.0, "volume": 50.0, "oi": 510.0},
        # 09:20 bucket empty (no trades) → dropped
        # 09:25 bucket: one minute
        {"start": datetime(2026, 7, 15, 9, 26), "open": 13.0, "high": 13.0, "low": 12.0,
         "close": 12.5, "volume": 25.0, "oi": 490.0},
    ])
    out = store.resample_bars(df.assign(symbol=sym)[["start", "open", "high", "low", "close",
                                                     "volume", "oi"]], 5)
    assert len(out) == 2
    b1, b2 = out.iloc[0], out.iloc[1]
    assert b1["start"] == pd.Timestamp(2026, 7, 15, 9, 15)
    assert (b1["open"], b1["high"], b1["low"], b1["close"]) == (10.0, 15.0, 9.0, 14.0)
    assert b1["volume"] == 150.0 and b1["oi"] == 510.0  # vol summed, oi = last
    assert b2["start"] == pd.Timestamp(2026, 7, 15, 9, 25)


# ----------------------------------------------------------------- capture_day
class FakeKite:
    def __init__(self, bars_by_token, raise_tokens=()):
        self.bars_by_token = bars_by_token
        self.raise_tokens = set(raise_tokens)
        self.calls: list[int] = []

    def historical_data(self, token, frm, to, interval, oi=False, continuous=False):
        # Kite's REAL 1-min interval name — "1minute" is invalid and fails every call
        # (the 2026-07-15 first-run bug this fake originally codified).
        assert interval == "minute" and oi is True
        self.calls.append(token)
        if token in self.raise_tokens:
            raise RuntimeError("historical down for this contract")
        return self.bars_by_token.get(token, [])


class FakeAdapter:
    def __init__(self, kite, index, tokens, spot=24000.0):
        self._kite = kite
        self._nfo_index = index
        self._nfo_token = tokens
        self._spot = spot

    def _build_nfo(self):
        pass

    def _kite_client(self):
        return self._kite

    def underlying_ltp(self, _u):
        return self._spot


def _bars(px):
    return [{"date": datetime(2026, 7, 15, 9, 15), "open": px, "high": px + 2, "low": px - 1,
             "close": px + 1, "volume": 111, "oi": 9000}]


def _adapter(raise_tokens=()):
    # NIFTY: near expiry has 24000 (in, 100-mult), 24050 (in — 50s stay: data ≠ trading rule),
    # 30000 (outside ±10%); far expiry (out of the 40d window) has 24000.
    index = {"NIFTY": {
        EXP_NEAR: {24000.0: {}, 24050.0: {}, 30000.0: {}},
        EXP_FAR: {24000.0: {}},
    }}
    tokens = {
        ("NIFTY", EXP_NEAR, 24000.0, "CE"): 1, ("NIFTY", EXP_NEAR, 24000.0, "PE"): 2,
        ("NIFTY", EXP_NEAR, 24050.0, "CE"): 3, ("NIFTY", EXP_NEAR, 24050.0, "PE"): 4,
        ("NIFTY", EXP_NEAR, 30000.0, "CE"): 5, ("NIFTY", EXP_NEAR, 30000.0, "PE"): 6,
        ("NIFTY", EXP_FAR, 24000.0, "CE"): 7, ("NIFTY", EXP_FAR, 24000.0, "PE"): 8,
    }
    kite = FakeKite({t: _bars(100.0 + t) for t in tokens.values()}, raise_tokens)
    return FakeAdapter(kite, index, tokens), kite


def test_capture_day_universe_and_oi():
    adapter, kite = _adapter()
    summary = store.capture_day(adapter, DAY, underlyings=["NIFTY"])
    # In-window: near expiry only, strikes 24000 + 24050 (50s kept), both rights = 4 contracts.
    assert summary["contracts"] == 4 and summary["with_data"] == 4 and summary["errors"] == 0
    assert sorted(kite.calls) == [1, 2, 3, 4]  # far expiry + far strike never fetched
    df = store.load_day(DAY)
    assert len(df) == 4 and set(df["oi"]) == {9000.0}
    assert "NIFTY|2026-07-21|24050|CE" in set(df["symbol"])  # 50-strike present in the DATA


def test_capture_day_reports_progress_with_upfront_total():
    adapter, _ = _adapter()
    calls: list[tuple[int, int]] = []
    store.capture_day(adapter, DAY, underlyings=["NIFTY"],
                      progress=lambda done, total: calls.append((done, total)))
    assert calls[0] == (0, 4)          # denominator known before the first fetch
    assert calls[-1] == (4, 4)
    assert [c[0] for c in calls] == list(range(5))  # monotonic per-contract ticks


def test_capture_day_skips_existing_file():
    adapter, kite = _adapter()
    store.write_day(DAY, _df([_row("NIFTY|2026-07-21|24000|CE", 9, 15, 100.0)]))
    summary = store.capture_day(adapter, DAY, underlyings=["NIFTY"])
    assert summary.get("skipped") == "exists" and kite.calls == []


def test_capture_day_one_contract_error_not_fatal():
    adapter, _ = _adapter(raise_tokens=(2,))
    summary = store.capture_day(adapter, DAY, underlyings=["NIFTY"])
    assert summary["errors"] == 1 and summary["with_data"] == 3
    assert len(store.load_day(DAY)) == 3  # the other contracts still landed


def test_capture_day_all_errors_writes_no_file():
    adapter, _ = _adapter(raise_tokens=(1, 2, 3, 4))
    summary = store.capture_day(adapter, DAY, underlyings=["NIFTY"])
    assert summary["errors"] == 4 and summary["rows"] == 0
    assert not store.day_path(DAY).exists()  # no file → tomorrow's sweep retries this day


# ---------------------------------------------------------- cross-day contract load
def test_load_contract_bars_across_days_and_5min():
    sym = "NIFTY|2026-07-21|24000|CE"
    other = "NIFTY|2026-07-21|24100|CE"
    d1, d2 = date(2026, 7, 14), date(2026, 7, 15)
    store.write_day(d1, _df([
        {"symbol": sym, "start": datetime(2026, 7, 14, 9, 15), "open": 10, "high": 11,
         "low": 9, "close": 10.5, "volume": 10, "oi": 100},
        {"symbol": other, "start": datetime(2026, 7, 14, 9, 15), "open": 99, "high": 99,
         "low": 99, "close": 99, "volume": 1, "oi": 1},
    ]))
    store.write_day(d2, _df([
        {"symbol": sym, "start": datetime(2026, 7, 15, 9, 16), "open": 20, "high": 22,
         "low": 19, "close": 21, "volume": 20, "oi": 200},
        {"symbol": sym, "start": datetime(2026, 7, 15, 9, 18), "open": 21, "high": 25,
         "low": 21, "close": 24, "volume": 5, "oi": 210},
    ]))
    one = store.load_contract_bars("NIFTY", EXP_NEAR, 24000, "CE", d1, d2, minutes=1)
    assert len(one) == 3 and list(one["close"]) == [10.5, 21.0, 24.0]  # other symbol filtered
    five = store.load_contract_bars("NIFTY", EXP_NEAR, 24000, "CE", d2, d2, minutes=5)
    assert len(five) == 1
    assert five.iloc[0]["high"] == 25.0 and five.iloc[0]["volume"] == 25.0
    assert five.iloc[0]["oi"] == 210.0


# -------------------------------------------------------------- store summary
def test_store_summary_totals_and_per_day():
    d1, d2 = date(2026, 7, 14), date(2026, 7, 15)
    store.write_day(d1, _df([
        {"symbol": "NIFTY|2026-07-21|24000|CE", "start": datetime(2026, 7, 14, 9, 15),
         "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "oi": 1},
        {"symbol": "BANKNIFTY|2026-07-30|51000|PE", "start": datetime(2026, 7, 14, 15, 29),
         "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2, "oi": 2},
    ]))
    store.write_day(d2, _df([_row("NIFTY|2026-07-21|24000|CE", 9, 16, 5.0)]))
    s = store.store_summary(days_limit=30)
    assert s["days_total"] == 2 and s["rows_total"] == 3
    assert s["first_day"] == "2026-07-14" and s["last_day"] == "2026-07-15"
    assert s["bytes_total"] > 0
    assert [d["day"] for d in s["days"]] == ["2026-07-15", "2026-07-14"]  # newest first
    d14 = s["days"][1]
    assert d14["rows"] == 2 and d14["contracts"] == 2
    assert d14["underlyings"] == {"BANKNIFTY": 1, "NIFTY": 1}
    assert d14["first_bar"][11:16] == "09:15" and d14["last_bar"][11:16] == "15:29"


# ------------------------------------------------------------------ mirror
def test_mirror_store_copies_never_deletes(tmp_path):
    dest = tmp_path / "drive" / "option_intraday"
    store.write_day(date(2026, 7, 14), _df([_row("NIFTY|2026-07-21|24000|CE", 9, 15, 1.0)]))
    out = store.mirror_store(dest)
    assert out["copied"] == 1 and out["skipped"] == 0
    assert (dest / "2026-07-14.parquet").exists()
    # Unchanged → skipped on re-run.
    out2 = store.mirror_store(dest)
    assert out2["copied"] == 0 and out2["skipped"] == 1
    # A stray file in the backup is NEVER deleted (copy-only semantics).
    stray = dest / "2020-01-01.parquet"
    stray.write_bytes(b"keep me")
    store.write_day(date(2026, 7, 15), _df([_row("NIFTY|2026-07-21|24000|PE", 9, 15, 2.0)]))
    out3 = store.mirror_store(dest)
    assert out3["copied"] == 1 and stray.exists()
    # A changed source file (e.g. GFD merge grew it) is re-copied.
    import time as _t
    _t.sleep(0.02)
    store.write_day(date(2026, 7, 14), _df([
        _row("NIFTY|2026-07-21|24000|CE", 9, 15, 1.0),
        _row("NIFTY|2026-07-21|24000|CE", 9, 16, 1.5),
    ]))
    out4 = store.mirror_store(dest)
    assert out4["copied"] >= 1
    assert not list(dest.glob("*.tmp"))  # tmp names always renamed away


# ------------------------------------------------------------- manager gates
class _FakeDT(datetime):
    _now = datetime(2026, 7, 15, 16, 0)  # Wed 16:00 IST — past the 15:45 gate

    @classmethod
    def now(cls, tz=None):
        return cls._now


def _capture_manager(monkeypatch, *, enabled=True, now=None):
    from skas_algo.config import get_settings
    from skas_algo.live.manager import LiveRunManager

    m = LiveRunManager()
    monkeypatch.setattr(get_settings(), "option_bars_capture_enabled", enabled)
    if now is not None:
        _FakeDT._now = now
    monkeypatch.setattr("skas_algo.live.manager.datetime", _FakeDT)
    calls: list[date] = []

    def fake_run(day):
        calls.append(day)
        return {"account": "t", "days": []}

    monkeypatch.setattr(m, "_run_option_capture", fake_run)
    return m, calls


def test_capture_gate_flag_off(monkeypatch):
    m, calls = _capture_manager(monkeypatch, enabled=False)
    asyncio.run(m._maybe_daily_option_capture())
    assert calls == [] and m._last_option_capture_day is None


def test_capture_gate_before_time(monkeypatch):
    m, calls = _capture_manager(monkeypatch, now=datetime(2026, 7, 15, 12, 0))
    asyncio.run(m._maybe_daily_option_capture())
    assert calls == []


def test_capture_runs_once_and_latches(monkeypatch):
    m, calls = _capture_manager(monkeypatch, now=datetime(2026, 7, 15, 16, 0))
    asyncio.run(m._maybe_daily_option_capture())
    asyncio.run(m._maybe_daily_option_capture())  # same day → latched
    assert calls == [date(2026, 7, 15)]
    assert m._last_option_capture_day == date(2026, 7, 15)
    assert m.last_option_capture and m.last_option_capture["account"] == "t"


def test_capture_no_session_does_not_latch(monkeypatch):
    m, calls = _capture_manager(monkeypatch, now=datetime(2026, 7, 15, 16, 0))
    monkeypatch.setattr(m, "_run_option_capture", lambda _d: None)
    asyncio.run(m._maybe_daily_option_capture())
    assert m._last_option_capture_day is None  # retries next 5-min tick


def test_manual_capture_rejected_before_gate(monkeypatch):
    import pytest as _pytest

    m, calls = _capture_manager(monkeypatch, now=datetime(2026, 7, 15, 12, 0))  # trading day
    with _pytest.raises(ValueError, match="15:45"):
        asyncio.run(m.run_option_capture_now())
    assert calls == []


def test_manual_capture_runs_after_gate_and_latches(monkeypatch):
    m, calls = _capture_manager(monkeypatch, now=datetime(2026, 7, 15, 16, 0))

    async def go():
        out = await m.run_option_capture_now()
        for _ in range(100):
            if not m.option_capture_running:
                break
            await asyncio.sleep(0.01)
        return out

    out = asyncio.run(go())
    assert out["started"] is True and out["target_day"] == "2026-07-15"
    assert calls == [date(2026, 7, 15)]
    assert m.last_option_capture and m.last_option_capture["trigger"] == "manual"
    assert m._last_option_capture_day == date(2026, 7, 15)


def test_manual_capture_weekend_targets_prev_trading_day(monkeypatch):
    # Saturday 18 Jul 2026, 10:00 — allowed anytime; target = Friday 17th.
    m, calls = _capture_manager(monkeypatch, now=datetime(2026, 7, 18, 10, 0))

    async def go():
        out = await m.run_option_capture_now()
        for _ in range(100):
            if not m.option_capture_running:
                break
            await asyncio.sleep(0.01)
        return out

    out = asyncio.run(go())
    assert out["started"] is True and out["target_day"] == "2026-07-17"
    assert calls == [date(2026, 7, 17)]


def test_manual_capture_single_flight(monkeypatch):
    m, _calls = _capture_manager(monkeypatch, now=datetime(2026, 7, 15, 16, 0))
    m.option_capture_running = True
    out = asyncio.run(m.run_option_capture_now())
    assert out["started"] is False and "already running" in out["reason"]


def test_manual_capture_backup_only_without_session(monkeypatch, tmp_path):
    from skas_algo.config import get_settings

    m, _ = _capture_manager(monkeypatch, now=datetime(2026, 7, 15, 16, 0))
    monkeypatch.setattr(m, "_run_option_capture", lambda _d: None)  # no broker session
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", str(tmp_path / "drive"))
    store.write_day(date(2026, 7, 14), _df([_row("NIFTY|2026-07-21|24000|CE", 9, 15, 1.0)]))

    async def go():
        out = await m.run_option_capture_now()
        for _ in range(100):
            if not m.option_capture_running:
                break
            await asyncio.sleep(0.01)
        return out

    out = asyncio.run(go())
    assert out["started"] is True
    assert m.last_option_capture and m.last_option_capture.get("note", "").startswith("no broker")
    assert (tmp_path / "drive" / "2026-07-14.parquet").exists()  # backup half still ran


def test_run_option_capture_sweeps_missing_days(monkeypatch, tmp_path):
    """today + days_back prior TRADING days are attempted; existing day-files are skipped."""
    from skas_algo.config import get_settings
    from skas_algo.live.manager import LiveRunManager

    m = LiveRunManager()
    monkeypatch.setattr(get_settings(), "option_bars_days_back", 2)
    monkeypatch.setattr(m, "_data_account", lambda _db: object())
    monkeypatch.setattr("skas_algo.services.broker.make_adapter", lambda _a: "ADAPTER")
    captured: list[date] = []

    def fake_capture(adapter, day, **kw):
        captured.append(day)
        return {"day": day.isoformat()}

    monkeypatch.setattr(store, "capture_day", fake_capture)
    # Pre-capture Monday so the sweep skips it: today Wed 15th → sweep Tue 14th + Mon 13th.
    store.write_day(date(2026, 7, 13), _df([_row("NIFTY|2026-07-21|24000|CE", 9, 15, 1.0)]))

    class _Acct:
        label = "t"
        broker = "zerodha"

    monkeypatch.setattr(m, "_data_account", lambda _db: _Acct())
    out = m._run_option_capture(date(2026, 7, 15))
    assert out is not None and out["account"] == "t"
    assert captured == [date(2026, 7, 15), date(2026, 7, 14)]  # Mon already on disk
