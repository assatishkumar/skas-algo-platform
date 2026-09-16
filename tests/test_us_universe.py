"""US constituent lists (2026-09-16): the Wikipedia table parse without lxml, the 90% rule,
the shared dated store, and the market registry."""

from __future__ import annotations

from datetime import date

import pytest

from skas_algo.data import nse_universe, universes, us_universe

_PAGE = """<html><body>
<table class="wikitable"><tr><th>Rank</th><th>Name</th></tr><tr><td>1</td><td>x</td></tr></table>
<table id="constituents" class="wikitable sortable">
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>
{rows}
</table></body></html>"""


def _page(symbols, col="Symbol"):
    rows = "\n".join(f"<tr><td><a href='#'>{s}</a></td><td>Co {s}</td><td>IT</td></tr>" for s in symbols)
    return _PAGE.replace("Symbol", col).format(rows=rows)


def test_parse_takes_the_first_table_with_the_ticker_column():
    html = _page(["aapl", "MSFT", "BRK.B", "MSFT", "BF.B"])
    assert us_universe.parse_constituents(html, "Symbol") == ["AAPL", "MSFT", "BRK.B", "BF.B"]
    with pytest.raises(ValueError, match="constituents page"):
        us_universe.parse_constituents("<html><body>Access denied</body></html>", "Symbol")


def test_a_truncated_page_is_refused_and_a_good_one_is_stored_dated(monkeypatch, tmp_path):
    monkeypatch.setenv("SKAS_UNIVERSE_DIR", str(tmp_path))
    short = _page([f"S{i}" for i in range(80)], col="Ticker")
    with pytest.raises(ValueError, match="truncated"):
        us_universe.fetch("nasdaq100", http=lambda url: short)
    full = _page([f"S{i}" for i in range(101)], col="Ticker")
    res = us_universe.refresh("nasdaq100", http=lambda url: full, day=date(2026, 9, 16))
    assert res["ok"] and res["count"] == 101 and res["changed"]
    got = nse_universe.latest("nasdaq100")                       # the SAME dated store
    assert got and got[0] == date(2026, 9, 16) and len(got[1]) == 101
    # the registry reads it back as "official"; the static snapshot is the fallback
    assert universes.current("nasdaq100") == got[1]
    assert universes.as_of("nasdaq100") == {"source": "official", "date": "2026-09-16"}
    assert universes.as_of("sp500")["source"] == "snapshot"


def test_the_market_registry():
    assert universes.market_of("sp500") == "US" and universes.market_of("nasdaq100") == "US"
    assert universes.market_of("nifty50") == "IN" and universes.market_of(None) == "IN"
    assert len(universes.SP500) >= 500 and len(universes.NASDAQ_100) >= 100
    assert len(set(universes.SP500)) == len(universes.SP500)
