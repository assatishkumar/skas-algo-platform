"""LiveChainView: a live deployment's coded options strategy must pick its expiry/strikes from the
broker chain for TODAY (not the stale bhavcopy cache), and fall back to the cache for other dates."""

from __future__ import annotations

from datetime import date, timedelta

from skas_algo.engine.options.chain import OptionChainView
from skas_algo.engine.options.live_chain import LiveChainView

EXP = date.today() + timedelta(days=8)


class FakeAdapter:
    def option_expiries(self, underlying):
        return [EXP.isoformat()]

    def underlying_ltp(self, underlying):
        return 23950.0

    def live_option_chain(self, underlying, expiry, window=40):
        return {"spot": 23950.0, "atm_strike": 23950.0, "lot_size": 65, "rows": [
            {"strike": 23900.0, "ce": {"ltp": 120.0, "oi": 1000}, "pe": {"ltp": 80.0, "oi": 900}},
            {"strike": 23950.0, "ce": {"ltp": 95.0, "oi": 1500}, "pe": {"ltp": 95.0, "oi": 1500}},
            {"strike": 24000.0, "ce": {"ltp": 70.0, "oi": 1100}, "pe": {"ltp": 130.0, "oi": 800}},
        ]}


def _stale_cache():
    # Cache that has nothing for any date (mimics bhavcopy lagging behind today).
    return OptionChainView(lambda u, on: None, lambda u, on: None)


def test_live_chain_uses_broker_for_today():
    lv = LiveChainView(_stale_cache(), FakeAdapter(), "NIFTY")
    today = date.today()
    assert lv.expiries("NIFTY", today) == [EXP]            # cache would be [] → live wins
    assert lv.expiry_for_dte("NIFTY", today, 8) == EXP
    assert lv.spot("NIFTY", today) == 23950.0
    rows = lv.chain("NIFTY", today, EXP)
    assert len(rows) == 6  # 3 strikes × CE/PE
    ce = next(r for r in rows if r.strike == 23900.0 and r.right == "CE")
    assert ce.close == 120.0 and ce.symbol.endswith("|23900|CE")
    assert lv.atm_strike("NIFTY", today, EXP) == 23950.0


def test_live_chain_falls_back_to_cache_for_other_dates():
    lv = LiveChainView(_stale_cache(), FakeAdapter(), "NIFTY")
    past = date.today() - timedelta(days=30)
    assert lv.expiries("NIFTY", past) == []      # delegates to the (empty) cache, not the adapter
    assert lv.spot("NIFTY", past) is None
