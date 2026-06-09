"""Data screen API: cache coverage views over a faked skas-data cache."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data.provider import get_data_cache


class _FakeStorage:
    db_path = "/tmp/fake_cache.db"

    def __init__(self, latest):
        self._latest = latest

    def get_latest_date(self, symbol):
        return self._latest.get(symbol)


class _FakeCache:
    def __init__(self):
        today = datetime.now(UTC).date()
        self._latest = {
            "FRESH": today,
            "STALE": today - timedelta(days=40),
        }
        self.storage = _FakeStorage(self._latest)

    def list_cached_symbols(self, asset_type="stock"):
        return ["FRESH", "STALE"]

    def get_coverage_stats(self, symbol):
        if symbol not in self._latest:
            raise KeyError(symbol)
        return {
            "start_date": date(2015, 1, 1),
            "end_date": self._latest[symbol],
            "total_records": 1000,
            "yearly_stats": [
                {"year": 2015, "count": 248, "min_date": date(2015, 1, 1), "max_date": date(2015, 12, 31)},
                {"year": 2016, "count": 247, "min_date": date(2016, 1, 1), "max_date": date(2016, 12, 30)},
            ],
        }

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock", use_cache=True):
        d = pd.bdate_range(end=self._latest[symbol], periods=5)
        return pd.DataFrame({"date": d, "close": [100.0, 101, 102, 103, 104]})


@pytest.fixture
def data_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_data_cache] = lambda: _FakeCache()
    return TestClient(app)


def test_data_summary(data_client: TestClient):
    body = data_client.get("/api/v1/data/summary").json()
    assert body["symbol_count"] == 2
    assert body["db_path"].endswith("fake_cache.db")


def test_data_symbols_freshness(data_client: TestClient):
    rows = {r["symbol"]: r for r in data_client.get("/api/v1/data/symbols").json()}
    assert rows["FRESH"]["stale"] is False and rows["FRESH"]["stale_days"] <= 5
    assert rows["STALE"]["stale"] is True and rows["STALE"]["stale_days"] >= 40


def test_data_symbol_detail(data_client: TestClient):
    body = data_client.get("/api/v1/data/symbols/FRESH").json()
    assert body["total_records"] == 1000
    assert [y["year"] for y in body["yearly"]] == [2015, 2016]
    assert len(body["recent"]) == 5 and body["recent"][-1]["close"] == 104.0

    assert data_client.get("/api/v1/data/symbols/NOPE").status_code == 404
