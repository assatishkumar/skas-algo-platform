"""Official NSE index constituents — fetched, dated, kept.

The static lists in ``universes.py`` are SNAPSHOTS, and a snapshot rots: by 2026-09-08 the
Nifty 500 in this repo had 95 names the index no longer held (ISEC, PEL, SWANENERGY …
delisted or renamed) and lacked 96 it did (SWIGGY, HYUNDAI, ETERNAL …); even the Nifty 50
was six names out. A universe deploy trades the list it is given, so a stale list means
trading names that left the index and never seeing the ones that joined.

This module fetches the index's own constituent files from niftyindices.com (the
``ind_nifty500list.csv`` family — the same source the Momentum-50 snapshot was captured
from), validates them, and stores one dated JSON per CHANGE under
``~/.skas_data/universes/<name>/<YYYY-MM-DD>.json`` (``SKAS_UNIVERSE_DIR``). ``latest``
is what ``universes.resolve`` trades today; ``history`` is a point-in-time membership
table in the shape ``MembershipGate`` already reads. Dates are OBSERVED dates — when we
first saw the list — not NSE's effective dates, so a rebalance shows up on the next
fetch after it lands (daily from the maintenance loop), never before.

Three rules. A download that parses to far fewer names than the index size is REJECTED,
never stored — a truncated or blocked response must not shrink a universe. ``DUMMY*``
placeholders (NSE's demerger stand-ins, e.g. DUMMYHEG) are dropped: they are not
tradable symbols. Nothing here is ever called from an order path.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import urllib.request
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("skas_algo.data")

_BASE = "https://niftyindices.com/IndexConstituent/"
# name -> (file, nominal size)
INDEX_FILES: dict[str, tuple[str, int]] = {
    "nifty50": ("ind_nifty50list.csv", 50),
    "nifty100": ("ind_nifty100list.csv", 100),
    "nifty200": ("ind_nifty200list.csv", 200),
    "nifty500": ("ind_nifty500list.csv", 500),
    "nifty500mom50": ("ind_nifty500Momentum50_list.csv", 50),
}
# The site answers a browser; a bare urllib UA gets a 403.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MIN_FRACTION = 0.9      # reject a list smaller than this share of the nominal size


def store_dir() -> Path:
    return Path(os.environ.get("SKAS_UNIVERSE_DIR", "~/.skas_data/universes")).expanduser()


def _http_get(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed host
        return resp.read().decode("utf-8-sig", errors="replace")


def parse_constituents(text: str) -> list[str]:
    """Symbols from an NSE constituent CSV (header carries a ``Symbol`` column), in the
    file's order, upper-cased, ``DUMMY*`` placeholders removed. Raises on a file with no
    Symbol column — an HTML block page must not parse to an empty universe."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Symbol" not in (rows[0].keys() or {}):
        raise ValueError("no 'Symbol' column — not an NSE constituent file")
    out: list[str] = []
    for r in rows:
        sym = (r.get("Symbol") or "").strip().upper()
        if not sym or sym.startswith("DUMMY"):
            continue
        if sym not in out:
            out.append(sym)
    return out


def fetch(name: str, http=None) -> list[str]:
    """Download + parse + validate one index's list. ``http`` is injectable for tests."""
    file, nominal = INDEX_FILES[name]
    text = (http or _http_get)(_BASE + file)
    symbols = parse_constituents(text)
    if len(symbols) < nominal * MIN_FRACTION:
        raise ValueError(
            f"{name}: only {len(symbols)} symbols parsed against a nominal {nominal} — "
            f"refusing a truncated list")
    return symbols


# ------------------------------------------------------------------ store
def _dir(name: str) -> Path:
    return store_dir() / name


def _files(name: str) -> list[Path]:
    d = _dir(name)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if len(p.stem) == 10)


def latest(name: str) -> tuple[date, list[str]] | None:
    """The newest stored list for ``name`` as (observed date, symbols), or None."""
    files = _files(name)
    if not files:
        return None
    doc = json.loads(files[-1].read_text())
    return date.fromisoformat(files[-1].stem), [str(s) for s in doc.get("symbols", [])]


def history(name: str) -> dict[str, list[str]]:
    """Every stored list keyed by observed ISO date — the ``membership`` table shape."""
    out: dict[str, list[str]] = {}
    for p in _files(name):
        doc = json.loads(p.read_text())
        out[p.stem] = [str(s) for s in doc.get("symbols", [])]
    return out


def save(name: str, symbols: list[str], day: date | None = None) -> bool:
    """Store ``symbols`` under today's date if they differ from the latest stored list.
    Returns True when a new file was written (the list CHANGED or was first seen)."""
    day = day or date.today()
    prev = latest(name)
    if prev is not None and prev[1] == list(symbols):
        return False
    d = _dir(name)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"name": name, "as_of": day.isoformat(), "count": len(symbols),
           "fetched_at": datetime.now().isoformat(timespec="seconds"),
           "symbols": list(symbols)}
    (d / f"{day.isoformat()}.json").write_text(json.dumps(doc, indent=0))
    return True


def refresh(name: str, *, http=None, day: date | None = None,
            baseline: list[str] | None = None) -> dict:
    """Fetch one index and store it if it changed. The result names what moved:
    ``added``/``dropped`` against the previous stored list (or ``baseline`` — the static
    snapshot — when nothing was stored yet), so the log can say "+3 −2" and a running
    universe run can act on it. A failure is reported, never raised."""
    prev = latest(name)
    before = prev[1] if prev is not None else (baseline or [])
    try:
        symbols = fetch(name, http=http)
    except Exception as exc:
        logger.warning("universe refresh %s failed: %s", name, exc)
        return {"name": name, "ok": False, "error": str(exc)[:200],
                "count": len(before), "as_of": prev[0].isoformat() if prev else None}
    changed = save(name, symbols, day)
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
            for n in (names or list(INDEX_FILES))}
