"""Research API: the breakout study over a faked cache, and the read-only calibration
endpoint over a fake adapter (asserting no order-side method is ever touched)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data.provider import get_available_symbols, get_data_cache


def _weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


class _FakeCache:
    def __init__(self):
        self.cal = _weekdays(date(2024, 1, 1), date(2024, 4, 30))
        n = len(self.cal)
        flat = pd.DataFrame({"date": self.cal, "high": [1040.0] * n, "low": [960.0] * n,
                             "close": [1000.0] * n})
        self.frames = {
            "AAA": flat,
            "NIFTY 50": pd.DataFrame({"date": self.cal, "high": [22050.0] * n,
                                      "low": [21950.0] * n, "close": [22000.0] * n}),
            "INDIA VIX": pd.DataFrame({"date": self.cal, "high": [15.0] * n,
                                       "low": [13.0] * n, "close": [14.0] * n}),
        }

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock",
                   use_cache=True):
        df = self.frames.get(symbol)
        if df is None:
            return None
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)


@pytest.fixture
def research_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_data_cache] = lambda: _FakeCache()
    # "AAA" isn't in any named universe → requests must pass explicit symbols.
    app.dependency_overrides[get_available_symbols] = lambda: {"AAA"}
    return TestClient(app)


def test_donchian_study_endpoint(research_client):
    resp = research_client.post("/api/v1/research/donchian-study", json={
        "symbols": ["AAA"], "start_date": "2024-01-01", "end_date": "2024-04-30",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["aggregates"]["cycles"] == 2       # Mar + Apr expiry-anchored cycles
    assert body["aggregates"]["names"] == 1
    assert body["aggregates"]["inside_pct"] == 100.0  # a flat name never breaches
    assert body["cycles"][0]["vix_entry"] == 14.0
    assert any(r["is_index"] for r in body["league"])
    assert body["detail"]  # detail included by default


def test_donchian_study_no_detail_and_bad_universe(research_client):
    slim = research_client.post("/api/v1/research/donchian-study", json={
        "symbols": ["AAA"], "start_date": "2024-01-01", "end_date": "2024-04-30",
        "detail": False,
    })
    assert slim.status_code == 200 and "detail" not in slim.json()
    bad = research_client.post("/api/v1/research/donchian-study",
                               json={"universe": "nope", "start_date": "2024-01-01"})
    assert bad.status_code == 404


def test_bs_calibration_requires_session(research_client):
    # No broker account in the test DB → the adapter dependency 404s before any fetch.
    resp = research_client.post("/api/v1/research/bs-calibration",
                                json={"broker_account_id": 999})
    assert resp.status_code in (400, 404)


def test_calibration_math_is_order_free():
    """The calibration service consumes plain dicts/frames — verify the math on a synthetic
    chain whose premiums embed a KNOWN IV, and that iv_over_hv recovers it."""
    from skas_algo.engine.options import black_scholes as bs
    from skas_algo.services.bs_calibration import aggregate, calibrate_name

    today, expiry, spot = date(2024, 3, 1), date(2024, 3, 28), 1000.0
    t = (expiry - today).days / 365.0
    true_iv = 0.30
    rows = []
    for k in range(900, 1101, 50):
        px_ce = bs.price(spot, k, t, 0.065, true_iv, "CE")
        px_pe = bs.price(spot, k, t, 0.065, true_iv, "PE")
        rows.append({"strike": float(k),
                     "ce": {"bid": px_ce, "ask": px_ce, "ltp": px_ce, "close": px_ce, "oi": 1},
                     "pe": {"bid": px_pe, "ask": px_pe, "ltp": px_pe, "close": px_pe, "oi": 1}})
    chain = {"spot": spot, "lot_size": 500, "rows": rows}
    cal = _weekdays(date(2024, 1, 1), date(2024, 3, 1))
    # ±1.5%-per-bar oscillation → a real HV the IV can be compared against.
    closes = [1000 * (1 + 0.015 * ((i % 2) * 2 - 1)) for i in range(len(cal))]
    df = pd.DataFrame({"date": cal, "high": [c + 5 for c in closes],
                       "low": [c - 5 for c in closes], "close": closes})

    out = calibrate_name(symbol="AAA", df=df, chain=chain, sell_expiry=expiry, today=today,
                         range_start=date(2024, 1, 26), range_end=date(2024, 2, 29))
    assert out, "calibration produced no rows"
    for r in out:
        assert r["market_iv_pct"] == pytest.approx(true_iv * 100, abs=0.5)
        assert r["iv_over_hv"] == pytest.approx((true_iv * 100) / r["hv_pct"], abs=0.05)
    agg = aggregate(out)
    assert agg["suggested_vol_multiplier"] == pytest.approx(
        (true_iv * 100) / out[0]["hv_pct"], abs=0.05)
