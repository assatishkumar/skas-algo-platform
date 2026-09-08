"""Universe resolution: list contents, cache intersection, order preservation."""

from __future__ import annotations

import pytest

from skas_algo.data import universes


def test_lists_present_and_sized():
    assert len(universes.NIFTY_25) == 25
    assert len(universes.NIFTY_50) == 50
    assert len(universes.NIFTY_100) == 100  # official NSE snapshot, 2026-09-08
    assert len(universes.NIFTY_200) == 200
    assert len(universes.NIFTY_500) == 500
    # No duplicates in any universe.
    for name in universes.UNIVERSES:
        symbols = universes.UNIVERSES[name][1]
        assert len(symbols) == len(set(symbols)), f"{name} has duplicates"
    assert len(universes.NIFTY500_MOMENTUM_50) == 50  # official NSE snapshot, 2026-08-18
    assert set(universes.UNIVERSES) == {"nifty25", "nifty50", "nifty100", "nifty200",
                                        "nifty500", "nifty500mom50"}
    # the top-25-by-weight basket is a strict subset of the Nifty 50, and the index
    # family nests (a broken nesting means one snapshot was regenerated and another not)
    assert set(universes.NIFTY_25) <= set(universes.NIFTY_50)
    assert set(universes.NIFTY_50) <= set(universes.NIFTY_100) <= set(universes.NIFTY_200)
    assert set(universes.NIFTY_200) <= set(universes.NIFTY_500)


def test_a_stored_official_list_outranks_the_snapshot(tmp_path, monkeypatch):
    """The snapshot is the baseline; what the fetcher stored on this box is the universe."""
    from datetime import date

    from skas_algo.data import nse_universe

    monkeypatch.setenv("SKAS_UNIVERSE_DIR", str(tmp_path))
    assert universes.as_of("nifty50")["source"] == "snapshot"
    nse_universe.save("nifty50", ["RELIANCE", "TCS", "NEWNAME"], date(2026, 9, 8))
    assert universes.current("nifty50") == ["RELIANCE", "TCS", "NEWNAME"]
    assert universes.resolve("nifty50", {"TCS", "NEWNAME", "INFY"}) == ["TCS", "NEWNAME"]
    assert universes.as_of("nifty50") == {"source": "official", "date": "2026-09-08"}
    # a universe the fetcher does not know keeps its snapshot
    assert universes.current("nifty25") == universes.NIFTY_25


def test_resolve_without_cache_returns_full_list():
    assert universes.resolve("nifty50") == universes.NIFTY_50


def test_resolve_intersects_and_preserves_order():
    available = {"RELIANCE", "TCS", "INFY"}  # only 3 of Nifty 50 present
    resolved = universes.resolve("nifty50", available)
    assert resolved == ["INFY", "RELIANCE", "TCS"]  # list order (alphabetical), not set order
    # Every resolved symbol is both in the universe and available.
    assert all(s in available and s in universes.NIFTY_50 for s in resolved)


def test_resolve_drops_missing_symbols():
    gone = set(universes.NIFTY_100[:2])
    available = set(universes.NIFTY_100) - gone
    resolved = universes.resolve("nifty100", available)
    assert not (gone & set(resolved))
    assert len(resolved) == len(universes.NIFTY_100) - 2


def test_resolve_unknown_name_raises():
    with pytest.raises(KeyError):
        universes.resolve("nifty1000", {"RELIANCE"})
