"""VPS rolling backup + Mac restore of the 1-min option store: prune_store retention, the
per-day download endpoint, and restore_from's gap-fill — store redirected to tmp, no network."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data import option_intraday_store as store
from skas_algo.services import option_restore as restore


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    from skas_algo.config import get_settings

    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)


def _write(day: str) -> None:
    df = pd.DataFrame(
        [
            {
                "symbol": "NIFTY|2026-07-21|24000|CE",
                "start": datetime(2026, 7, 15, 9, 15),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 100.0,
                "oi": 5000.0,
            }
        ],
        columns=store.COLUMNS,
    )
    store.write_day(day, df)


# ------------------------------------------------------------------ prune_store
def test_prune_keeps_newest_n():
    for d in ["2026-07-10", "2026-07-11", "2026-07-14", "2026-07-15", "2026-07-16"]:
        _write(d)
    out = store.prune_store(3)
    assert out["deleted"] == ["2026-07-10", "2026-07-11"]
    assert store.captured_days() == ["2026-07-14", "2026-07-15", "2026-07-16"]


def test_prune_noop_when_zero_or_fewer():
    _write("2026-07-15")
    _write("2026-07-16")
    assert store.prune_store(0)["deleted"] == []  # keep-forever (Mac)
    assert store.prune_store(7)["deleted"] == []  # fewer files than keep
    assert len(store.captured_days()) == 2


# --------------------------------------------------------------- download endpoint
@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_download_day(client):
    _write("2026-07-15")
    r = client.get("/api/v1/data/options/intraday-store/day/2026-07-15")
    assert r.status_code == 200 and r.content.startswith(b"PAR1")  # a real parquet file
    assert client.get("/api/v1/data/options/intraday-store/day/2026-07-14").status_code == 404
    assert client.get("/api/v1/data/options/intraday-store/day/not-a-date").status_code == 422


# ------------------------------------------------------------------ restore_from
class _Resp:
    def __init__(self, status=200, json_data=None, content=b""):
        self.status_code, self._json, self.content = status, json_data, content

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeRequests:
    """Serves option_restore's HTTP calls from an in-memory {day: parquet-bytes} remote."""

    def __init__(self, remote_bytes: dict[str, bytes]):
        self.remote = remote_bytes

    def post(self, url, json=None, headers=None, timeout=None):
        return _Resp(json_data={"access_token": "t"})

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/intraday-store"):
            return _Resp(json_data={"days": [{"day": d} for d in sorted(self.remote)]})
        d = url.rsplit("/", 1)[-1]
        return _Resp(content=self.remote[d]) if d in self.remote else _Resp(status=404)


def test_restore_fills_gaps(tmp_path, monkeypatch):
    # The "remote" (VPS) store: 3 days → capture their raw bytes.
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "remote")
    days = ["2026-07-14", "2026-07-15", "2026-07-16"]
    for d in days:
        _write(d)
    remote_bytes = {d: store.day_path(d).read_bytes() for d in days}
    # The local (Mac) store has only the MIDDLE day.
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "local")
    _write("2026-07-15")
    monkeypatch.setattr(restore, "requests", _FakeRequests(remote_bytes))

    out = restore.restore_from("https://vps.ts.net", days=30)
    assert sorted(out["restored"]) == ["2026-07-14", "2026-07-16"]  # only the gaps
    assert out["already"] == 1
    assert set(store.captured_days()) == set(days)  # all three now local
    for d in days:
        assert store.day_path(d).read_bytes().startswith(b"PAR1")  # valid parquet written

    assert restore.restore_from("https://vps.ts.net", days=30)["restored"] == []  # idempotent
    # --overwrite re-pulls every remote day.
    again = restore.restore_from("https://vps.ts.net", days=30, overwrite=True)
    assert sorted(again["restored"]) == days


def test_restore_skips_pruned_day(tmp_path, monkeypatch):
    # The remote LISTS a day it no longer has (pruned between list + download) → skipped, not fatal.
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "local")

    class _Gone(_FakeRequests):
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/intraday-store"):
                return _Resp(json_data={"days": [{"day": "2026-07-16"}]})
            return _Resp(status=404)

    monkeypatch.setattr(restore, "requests", _Gone({}))
    out = restore.restore_from("https://vps.ts.net", days=30)
    assert out["restored"] == [] and out["skipped"] == ["2026-07-16"]
