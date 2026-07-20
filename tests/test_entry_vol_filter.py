"""EntryVolFilterMixin — the generic vol-premium entry gate for option sellers.

Pins the §1-critical default (OFF → behaviour unchanged), the premium comparison, fail-open
on missing data, and the realized-vol provider's no-lookahead contract. No network.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.data.options_provider import make_realized_vol_fn
from skas_algo.strategies._options_common import EntryVolFilterMixin


class _Strat(EntryVolFilterMixin):
    def __init__(self, vol_premium_min=0.0, hv=None):
        self.vol_premium_min = vol_premium_min
        self._realized_vol_fn = (lambda u, d: hv) if hv is not None else None


def test_filter_off_is_always_ok():
    # §1: the default (0) must never block — a recovered deploy stays byte-identical.
    s = _Strat(vol_premium_min=0.0, hv=10.0)
    assert s._vol_premium_ok("NIFTY", date(2026, 2, 2), 5.0) is True
    assert s._last_vol_premium is None


def test_premium_threshold():
    s = _Strat(vol_premium_min=2.0, hv=10.0)
    assert s._vol_premium_ok("NIFTY", date(2026, 2, 2), 14.0) is True    # 14-10=4 >= 2
    assert s._last_vol_premium == 4.0
    assert s._vol_premium_ok("NIFTY", date(2026, 2, 2), 11.0) is False   # 11-10=1 < 2
    assert s._last_vol_premium == 1.0
    assert s._vol_premium_ok("NIFTY", date(2026, 2, 2), 12.0) is True    # 12-10=2 >= 2 (boundary)


def test_fail_open_on_missing_data():
    # Missing implied OR realized → do NOT block (a data hiccup must not freeze trading).
    d = date(2026, 2, 2)
    assert _Strat(vol_premium_min=2.0, hv=10.0)._vol_premium_ok("NIFTY", d, None) is True
    assert _Strat(vol_premium_min=2.0, hv=None)._vol_premium_ok("NIFTY", d, 14.0) is True


def test_ratio_family_default_is_off():
    from skas_algo.strategies.registry import get_strategy
    for sid in ("batman_ratio_monthly", "call_ratio_monthly", "put_ratio_monthly", "hni_weekly"):
        s = get_strategy(sid)(universe=["NIFTY"], initial_capital=1_000_000)
        assert s.vol_premium_min == 0.0 and s.hv_window == 20
        assert hasattr(s, "set_realized_vol_fn")


def test_realized_vol_provider_no_lookahead():
    # A synthetic index series; HV at on_date must use only SETTLED closes BEFORE on_date.
    days = pd.bdate_range("2026-01-01", periods=45)
    closes = [25000 + (i % 5 - 2) * 50 for i in range(45)]   # a small oscillation
    df = pd.DataFrame({"date": days, "close": closes})
    fn = make_realized_vol_fn(lambda u: df, window=20)
    on_date = days[30].date()                                # 30 settled closes before → enough
    hv = fn("NIFTY", on_date)
    assert hv is not None and hv > 0
    # Poisoning a FUTURE close (after on_date) must not change the answer — proves no lookahead.
    df2 = df.copy()
    df2.loc[38, "close"] = 40000
    assert make_realized_vol_fn(lambda u: df2, window=20)("NIFTY", on_date) == hv
    # Too little history before the date (window+1=21 needed) → None, not a garbage number.
    assert fn("NIFTY", days[10].date()) is None
