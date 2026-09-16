"""US index constituents — the S&P 500 and the Nasdaq-100 from Wikipedia (2026-09-16).

The `data/nse_universe.py` twin for the second market: fetch, validate (a list under 90%
of the nominal size is a truncated page, never a smaller universe), store as a dated JSON
under the SAME store (`nse_universe.store_dir()/<name>/<date>.json`, written only on
change) so `universes.current()`/`as_of()` and the membership-table shape all work
unchanged. The venv has no lxml/bs4, so the table comes out through `html.parser`.

TODAY'S constituents only: a run over them carries survivorship bias (today's winners were
not all in the index in 2018). A point-in-time table from the S&P 500 change log is the
phase-2 fix, the `mom50_membership` template.
"""

from __future__ import annotations

import logging
import urllib.request
from datetime import date
from html.parser import HTMLParser

from skas_algo.data import nse_universe

logger = logging.getLogger("skas_algo.data")

# name -> (page, the column that holds the ticker, nominal size)
PAGES: dict[str, tuple[str, str, int]] = {
    "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", 500),
    # the index page dropped its components table in 2026; the list has its own page
    "nasdaq100": ("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", "Ticker", 100),
}
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MIN_FRACTION = 0.9


def _http_get(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed host
        return resp.read().decode("utf-8", errors="replace")


class _Tables(HTMLParser):
    """Every <table> as a list of rows of cell texts (tags stripped, whitespace collapsed)."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_constituents(html: str, column: str) -> list[str]:
    """Tickers from the FIRST table whose header has ``column``, in page order, upper-cased,
    de-duplicated. Raises when no such table exists — a block page must not parse to an
    empty universe."""
    p = _Tables()
    p.feed(html)
    for table in p.tables:
        if not table:
            continue
        header = [h.strip() for h in table[0]]
        if column not in header:
            continue
        idx = header.index(column)
        out: list[str] = []
        for row in table[1:]:
            if idx >= len(row):
                continue
            sym = row[idx].strip().upper().split(" ")[0]
            if sym and sym not in out:
                out.append(sym)
        if out:
            return out
    raise ValueError(f"no table with a {column!r} column — not the constituents page")


def fetch(name: str, http=None) -> list[str]:
    url, column, nominal = PAGES[name]
    symbols = parse_constituents((http or _http_get)(url), column)
    if len(symbols) < nominal * MIN_FRACTION:
        raise ValueError(f"{name}: only {len(symbols)} symbols parsed against a nominal "
                         f"{nominal} — refusing a truncated list")
    return symbols


def refresh(name: str, *, http=None, day: date | None = None,
            baseline: list[str] | None = None) -> dict:
    """Fetch one index and store it if it changed (the `nse_universe.refresh` contract)."""
    prev = nse_universe.latest(name)
    before = prev[1] if prev is not None else (baseline or [])
    try:
        symbols = fetch(name, http=http)
    except Exception as exc:
        logger.warning("universe refresh %s failed: %s", name, exc)
        return {"name": name, "ok": False, "error": str(exc)[:200],
                "count": len(before), "as_of": prev[0].isoformat() if prev else None}
    changed = nse_universe.save(name, symbols, day)
    added = [s for s in symbols if s not in before]
    dropped = [s for s in before if s not in symbols]
    if changed:
        logger.info("universe %s: %d names, +%d %s −%d %s", name, len(symbols),
                    len(added), added[:12], len(dropped), dropped[:12])
    return {"name": name, "ok": True, "count": len(symbols), "changed": changed,
            "added": added, "dropped": dropped,
            "as_of": (day or date.today()).isoformat()}


def refresh_all(names=None, *, http=None, day: date | None = None,
                baselines: dict[str, list[str]] | None = None) -> dict[str, dict]:
    return {n: refresh(n, http=http, day=day, baseline=(baselines or {}).get(n))
            for n in (names or list(PAGES))}
