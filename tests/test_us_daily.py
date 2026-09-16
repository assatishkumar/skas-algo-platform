"""The US daily store (2026-09-16): Yahoo's chart JSON → a csv.gz per symbol, the tail
top-up merge, the PriceLoader contract (EMPTY on a miss), symbol quirks, coverage."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from skas_algo.data import us_daily


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("SKAS_US_DAILY_DIR", str(tmp_path / "us_daily"))


def _chart(start: date, n: int, base: float = 100.0, split_at: int | None = None) -> dict:
    ts, o, h, lo, c, v, adj = [], [], [], [], [], [], []
    d = start
    i = 0
    while i < n:
        if d.weekday() < 5:
            px = base + i
            ts.append(int(datetime(d.year, d.month, d.day, 14, 30).timestamp()))
            o.append(px); h.append(px + 1); lo.append(px - 1); c.append(px + 0.5)
            v.append(1000.0); adj.append(px + 0.5 - 0.1)
            i += 1
        d += timedelta(days=1)
    if split_at is not None:                      # a null row Yahoo sometimes emits
        c[split_at] = None
    return {"chart": {"result": [{"timestamp": ts,
                                  "indicators": {"quote": [{"open": o, "high": h, "low": lo,
                                                            "close": c, "volume": v}],
                                                 "adjclose": [{"adjclose": adj}]}}],
                      "error": None}}


def test_parse_drops_null_rows_and_keeps_split_adjusted_ohlc_beside_adjclose():
    df = us_daily.parse_chart(_chart(date(2026, 1, 5), 5, split_at=2), "AAPL")
    assert list(df.columns) == us_daily.COLUMNS and len(df) == 4
    assert df["date"].iloc[0] == "2026-01-05" and df["close"].iloc[0] == 100.5
    assert df["adjclose"].iloc[0] == pytest.approx(100.4)      # kept, never used by the loader
    with pytest.raises(ValueError, match="denied"):
        us_daily.parse_chart({"chart": {"result": None, "error": {"description": "denied"}}}, "X")


def test_refresh_fetches_ten_years_first_then_only_the_tail_and_never_shrinks():
    calls: list[tuple[str, str]] = []

    def http(sym, rng):
        calls.append((sym, rng))
        if rng == "10y":
            return _chart(date(2026, 1, 5), 20)
        return _chart(date(2026, 1, 26), 10)                   # overlaps the last 5 bars
    out = us_daily.refresh(["aapl"], http=http, throttle=0, today=date(2026, 2, 6))
    assert out["AAPL"]["ok"] and out["AAPL"]["rows"] == 20 and calls == [("AAPL", "10y")]
    # a top-up: the gap is ~5 days → 1mo; overlapping dates are replaced, not duplicated
    out = us_daily.refresh(["AAPL"], http=http, throttle=0, today=date(2026, 2, 10))
    assert calls[-1] == ("AAPL", "1mo") and out["AAPL"]["rows"] == 25 and out["AAPL"]["added"] == 5
    assert us_daily.last_date("AAPL") == date(2026, 2, 6)
    # a failure keeps the file and is reported, never raised
    def boom(sym, rng):
        raise RuntimeError("429 Too Many Requests")
    out = us_daily.refresh(["AAPL", "NEW"], http=boom, throttle=0)
    assert not out["AAPL"]["ok"] and "429" in out["AAPL"]["error"]
    assert us_daily.last_date("AAPL") == date(2026, 2, 6) and "NEW" not in us_daily.cached_symbols()


def test_the_loader_honours_the_price_loader_contract():
    us_daily.refresh(["MSFT"], http=lambda s, r: _chart(date(2026, 1, 5), 20), throttle=0)
    df = us_daily.load("MSFT", date(2026, 1, 7), date(2026, 1, 13))
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 5 and str(df["date"].iloc[0].date()) == "2026-01-07"
    assert str(df["date"].iloc[-1].date()) == "2026-01-13"          # inclusive both ends
    assert pd.api.types.is_datetime64_any_dtype(df["date"])
    missing = us_daily.load("NOPE", date(2026, 1, 1), date(2026, 2, 1))
    assert missing is not None and missing.empty                     # EMPTY, never None
    assert us_daily.cached_symbols() == {"MSFT"}


def test_yahoo_symbol_mapping_and_coverage():
    assert us_daily.yahoo_symbol("BRK.B") == "BRK-B" and us_daily.yahoo_symbol("aapl") == "AAPL"
    seen = []
    us_daily.refresh(["BRK.B"], http=lambda s, r: (seen.append(s), _chart(date(2026, 1, 5), 3))[1],
                     throttle=0)
    assert seen == ["BRK.B"] and us_daily.cached_symbols() == {"BRK.B"}
    cov = us_daily.coverage(stale_days=5)
    assert cov["symbols"] == 1 and cov["first"] == "2026-01-05" and cov["stale_count"] == 1


def test_the_range_grows_with_the_gap():
    t = date(2026, 9, 16)
    assert us_daily._range_for_gap(None, t) == "10y"
    assert us_daily._range_for_gap(t - timedelta(days=3), t) == "1mo"
    assert us_daily._range_for_gap(t - timedelta(days=60), t) == "3mo"
    assert us_daily._range_for_gap(t - timedelta(days=400), t) == "2y"
    assert us_daily._range_for_gap(t - timedelta(days=900), t) == "10y"
