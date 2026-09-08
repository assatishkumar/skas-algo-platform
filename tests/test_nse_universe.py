"""The official NSE constituent fetcher: parse, validate, dated store, change detection."""

from __future__ import annotations

from datetime import date

import pytest

from skas_algo.data import nse_universe as nu

HEADER = "Company Name,Industry,Symbol,Series,ISIN Code\n"


def _csv(symbols):
    return HEADER + "".join(f"Co {s},Ind,{s},EQ,INE{i:03d}\n" for i, s in enumerate(symbols))


def test_parse_keeps_order_uppercases_and_drops_dummy_placeholders():
    text = _csv(["reliance", "TCS", "DUMMYHEG", "TCS", "GVT&D"])
    assert nu.parse_constituents(text) == ["RELIANCE", "TCS", "GVT&D"]


def test_a_block_page_does_not_parse_to_an_empty_universe():
    with pytest.raises(ValueError, match="Symbol"):
        nu.parse_constituents("<html><body>Access denied</body></html>")


def test_a_truncated_download_is_refused(monkeypatch, tmp_path):
    """A half-received file must never shrink a universe: 40 names for a 50-name index."""
    monkeypatch.setenv("SKAS_UNIVERSE_DIR", str(tmp_path))
    short = _csv([f"S{i}" for i in range(40)])
    with pytest.raises(ValueError, match="truncated"):
        nu.fetch("nifty50", http=lambda url: short)
    res = nu.refresh("nifty50", http=lambda url: short, day=date(2026, 9, 8))
    assert res["ok"] is False and nu.latest("nifty50") is None


def test_refresh_stores_only_on_change_and_names_what_moved(monkeypatch, tmp_path):
    monkeypatch.setenv("SKAS_UNIVERSE_DIR", str(tmp_path))
    base = [f"S{i}" for i in range(50)]
    # first sight: diffed against the static baseline the caller passes
    r1 = nu.refresh("nifty50", http=lambda u: _csv(base), day=date(2026, 9, 8),
                    baseline=base[:48] + ["OLD1", "OLD2"])
    assert r1["ok"] and r1["changed"] and r1["count"] == 50
    assert r1["added"] == ["S48", "S49"] and r1["dropped"] == ["OLD1", "OLD2"]
    assert nu.latest("nifty50") == (date(2026, 9, 8), base)
    # same list next day: nothing written, nothing moved
    r2 = nu.refresh("nifty50", http=lambda u: _csv(base), day=date(2026, 9, 9))
    assert r2["changed"] is False and r2["added"] == [] and r2["dropped"] == []
    assert list(nu.history("nifty50")) == ["2026-09-08"]
    # a rebalance: one out, one in → a second dated file, the history grows
    new = base[1:] + ["JOINER"]
    r3 = nu.refresh("nifty50", http=lambda u: _csv(new), day=date(2026, 9, 30))
    assert r3["added"] == ["JOINER"] and r3["dropped"] == ["S0"]
    assert list(nu.history("nifty50")) == ["2026-09-08", "2026-09-30"]
    assert nu.latest("nifty50") == (date(2026, 9, 30), new)


def test_refresh_all_reports_per_index_and_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("SKAS_UNIVERSE_DIR", str(tmp_path))

    def http(url):
        if "nifty50list" in url:
            return _csv([f"S{i}" for i in range(50)])
        raise OSError("403 Forbidden")

    out = nu.refresh_all(["nifty50", "nifty500"], http=http, day=date(2026, 9, 8))
    assert out["nifty50"]["ok"] and out["nifty500"]["ok"] is False
    assert "403" in out["nifty500"]["error"]
