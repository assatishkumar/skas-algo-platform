"""Universe resolution: list contents, cache intersection, order preservation."""

from __future__ import annotations

import pytest

from skas_algo.data import universes


def test_lists_present_and_sized():
    assert len(universes.NIFTY_50) == 50
    assert len(universes.NIFTY_100) == 109  # user-provided (2025/26 index revisions)
    assert len(universes.NIFTY_200) == 199  # user-provided list (1 short of 200)
    assert len(universes.NIFTY_500) == 500  # user-provided full constituent list
    # No duplicates in any universe.
    for name in universes.UNIVERSES:
        symbols = universes.UNIVERSES[name][1]
        assert len(symbols) == len(set(symbols)), f"{name} has duplicates"
    assert set(universes.UNIVERSES) == {"nifty50", "nifty100", "nifty200", "nifty500"}


def test_resolve_without_cache_returns_full_list():
    assert universes.resolve("nifty50") == universes.NIFTY_50


def test_resolve_intersects_and_preserves_order():
    available = {"RELIANCE", "TCS", "INFY"}  # only 3 of Nifty 50 present
    resolved = universes.resolve("nifty50", available)
    assert resolved == ["INFY", "RELIANCE", "TCS"]  # list order (alphabetical), not set order
    # Every resolved symbol is both in the universe and available.
    assert all(s in available and s in universes.NIFTY_50 for s in resolved)


def test_resolve_drops_missing_symbols():
    available = set(universes.NIFTY_100) - {"YESBANK", "ABB"}
    resolved = universes.resolve("nifty100", available)
    assert "YESBANK" not in resolved and "ABB" not in resolved
    assert len(resolved) == len(universes.NIFTY_100) - 2


def test_resolve_unknown_name_raises():
    with pytest.raises(KeyError):
        universes.resolve("nifty1000", {"RELIANCE"})
