"""Point-in-time Nifty500 Momentum 50: the membership table's integrity, the as-of lookup,
and nifty_shop's scan filter."""

from __future__ import annotations

from datetime import date

from skas_algo.data import mom50_membership as m


def test_membership_table_shape_and_sources():
    t = m.membership_table()
    assert len(t) == 21                                  # 2016-06-30 … 2026-06-30, semi-annual
    assert min(t) == "2016-06-30" and max(t) == "2026-06-30"
    assert all(len(v) == 50 for v in t.values())
    src = m.load()["sources"]
    # the three published checkpoints stay OFFICIAL — replication must never overwrite them
    for k in ("2024-06-30", "2024-12-31", "2026-06-30"):
        assert src[k] == "official"
    assert sum(1 for v in src.values() if v == "computed") == 18


def test_union_covers_every_rebalance():
    u = set(m.union_members())
    for syms in m.membership_table().values():
        assert set(syms) <= u


def test_nifty_shop_scans_only_members_asof_that_day():
    from skas_algo.strategies.nifty_shop import NiftyShopStrategy

    table = {"2024-06-30": ["AAA", "BBB"], "2024-12-31": ["BBB", "CCC"]}
    st = NiftyShopStrategy(universe=["AAA", "BBB", "CCC"], membership=table)
    assert st._members_asof("2024-07-01") == frozenset({"AAA", "BBB"})
    assert st._members_asof("2025-01-15") == frozenset({"BBB", "CCC"})
    # exactly ON the effective date the new list applies
    assert st._members_asof("2024-12-31") == frozenset({"BBB", "CCC"})
    # before the first entry: the first list (documented fallback), not an empty scan
    assert st._members_asof("2023-01-01") == frozenset({"AAA", "BBB"})
    # default None keeps the old behavior — no filter object at all
    assert NiftyShopStrategy(universe=["AAA"]).membership is None
