"""Universe resolution: list contents, cache intersection, order preservation."""

from __future__ import annotations

import pytest

from skas_algo.data import universes


def test_lists_present_and_sized():
    assert len(universes.NIFTY_50) == 50
    assert len(universes.NIFTY_100) == 105
    assert len(universes.NIFTY_200) == 195  # user-provided list (5 short of 200)
    # No duplicates in any universe.
    for name in universes.UNIVERSES:
        symbols = universes.UNIVERSES[name][1]
        assert len(symbols) == len(set(symbols)), f"{name} has duplicates"
    assert set(universes.UNIVERSES) == {"nifty50", "nifty100", "nifty200"}


def test_resolve_without_cache_returns_full_list():
    assert universes.resolve("nifty50") == universes.NIFTY_50


def test_resolve_intersects_and_preserves_order():
    available = {"RELIANCE", "TCS", "INFY"}  # only 3 of Nifty 50 present
    resolved = universes.resolve("nifty50", available)
    assert resolved == ["RELIANCE", "TCS", "INFY"]  # list order, not set order
    # Every resolved symbol is both in the universe and available.
    assert all(s in available and s in universes.NIFTY_50 for s in resolved)


def test_resolve_drops_missing_symbols():
    available = set(universes.NIFTY_100) - {"ZOMATO", "IDFC"}
    resolved = universes.resolve("nifty100", available)
    assert "ZOMATO" not in resolved and "IDFC" not in resolved
    assert len(resolved) == len(universes.NIFTY_100) - 2


def test_resolve_unknown_name_raises():
    with pytest.raises(KeyError):
        universes.resolve("nifty500", {"RELIANCE"})
