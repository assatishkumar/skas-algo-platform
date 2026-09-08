"""Live SuperTrend needs YEARS of cached bars; a fresh box has none (2026-09-08: the VPS cache
held 17 symbols). The seed reports thin symbols, the refresh backfills them deep, and a held
name with no direction is said out loud instead of silently never exiting."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd

from skas_algo.services import market_data


def _df(days: int, end: date = date(2026, 9, 5)) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=days)
    px = [100 + i * 0.1 for i in range(len(dates))]
    return pd.DataFrame({"date": dates, "open": px, "high": [p + 1 for p in px],
                         "low": [p - 1 for p in px], "close": px})


def test_thin_symbols_are_the_ones_without_years_of_bars():
    end = date(2026, 9, 5)
    depth = {"DEEP": 1000, "SHORT": 30, "NEW": 0}

    def loader(sym, start, e):
        n = depth[sym]
        return _df(n) if n else pd.DataFrame(columns=["date", "close"])

    assert market_data.thin_symbols(["DEEP", "SHORT", "NEW"], end, loader=loader) == {"SHORT", "NEW"}


def test_refresh_backfills_a_thin_symbol_years_back_and_a_deep_one_a_month(monkeypatch):
    calls: dict[str, date] = {}

    class FakeSession:
        def get_prices(self, sym, start_date, end_date, asset_type="stock", use_cache=True):
            calls[sym] = start_date
            return _df(5, end_date)

    monkeypatch.setattr(market_data.broker_svc, "make_data_session", lambda account: FakeSession())
    monkeypatch.setattr(market_data, "thin_symbols", lambda syms, end, **kw: {"NEW"})
    out = market_data.refresh_cache(SimpleNamespace(broker="zerodha"), ["DEEP", "NEW"])
    assert out["NEW"]["backfilled"] is True and out["DEEP"]["backfilled"] is False
    assert (calls["DEEP"] - calls["NEW"]).days > 1000          # years back vs a month back
    # an explicit start disables the check and applies to everyone
    calls.clear()
    market_data.refresh_cache(SimpleNamespace(broker="zerodha"), ["DEEP", "NEW"], start=date(2026, 8, 1))
    assert calls["DEEP"] == calls["NEW"] == date(2026, 8, 1)


def test_the_seed_names_the_symbols_whose_history_is_too_thin_to_trust():
    from skas_algo.live.manager import SUPERTREND_MIN_BARS, _seed_supertrend

    dirs: dict[str, object] = {}
    market = SimpleNamespace(set_supertrend_dir=lambda s, d, line=None: dirs.__setitem__(s, d))
    session = SimpleNamespace(market=market)
    strategy = SimpleNamespace(needs_supertrend=True,
                               supertrend_config=lambda: {"period": 10, "multiplier": 3.0, "timeframe": "daily"})
    depth = {"DEEP": SUPERTREND_MIN_BARS + 50, "SHORT": 40, "NONE": 0}

    def loader(sym, start, end):
        return _df(depth[sym]) if depth[sym] else None

    missing = _seed_supertrend(session, strategy, loader, ["DEEP", "SHORT", "NONE"])
    assert missing == ["SHORT", "NONE"] and session.supertrend_missing == ["SHORT", "NONE"]
    assert dirs["DEEP"] in (1.0, -1.0)
    assert dirs["SHORT"] in (1.0, -1.0)     # thin still yields a direction — an exit can use it
    assert dirs["NONE"] is None


def test_a_held_name_with_no_direction_is_reported_not_ignored():
    from tests.test_supertrend_funding import Ctx, strat, tick

    st = strat()
    ctx = Ctx(cash=1_000_000)
    ctx.add_lot("AAA", 10, 100.0)
    ctx.add_lot("BBB", 10, 100.0)
    st.prev_dir = {"AAA": 1, "BBB": 1}
    tick(st, ctx, {"AAA": 100.0, "BBB": 100.0}, {"AAA": 1})          # BBB has no direction
    assert st.strategy_alert and "BBB" in st.strategy_alert and "cannot exit" in st.strategy_alert
    tick(st, ctx, {"AAA": 100.0, "BBB": 100.0}, {"AAA": 1, "BBB": 1})
    assert st.strategy_alert is None                                  # cleared once it prices
