"""AMFI daily NAV feed — the price source for mutual-fund holdings.

AMFI publishes every scheme's NAV once a day as one semicolon-delimited text file at
https://www.amfiindia.com/spages/NAVAll.txt. No key, no auth, no rate limit worth the name.
It is the only free feed that covers Indian MFs, so it is what /portfolio syncs funds from.

Three things about it are load-bearing:

* **ISIN is the join key, not the scheme name.** Names carry plan/option suffixes that get
  edited ("Value Fund (erstwhile Value Discovery Fund)"), and two rows differ only by
  "- Direct Plan -". The file gives both ISINs per scheme — growth/payout and reinvestment —
  and either may be the one the owner holds, so BOTH are indexed.
* **The NAV is a day behind during market hours.** Funds strike NAV after the close and AMFI
  publishes late evening, so a fetch at 15:52 on a Monday returns FRIDAY's NAV. That is the
  feed, not a bug — every NAV therefore carries its own ``as_of`` date and callers surface it
  rather than implying the number is live.
* **Day change needs yesterday's file.** A NAV move is only computable against the previous
  publication, so each day's raw file is cached on disk and the prior one is read back. Nothing
  is inferred when the prior file is missing — the change is simply unknown (None).

Cache: ``~/.skas_data/amfi/NAVAll-<date>.txt``, pruned to ``_KEEP_DAYS``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
# The dated historical report — used ONLY to seed a prior day so the first sync can show
# a day change instead of waiting a night for a second file to exist.
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
_KEEP_DAYS = 10
_TIMEOUT = 30

# AMFI dates read "28-Aug-2026".
_DATE_RE = re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4}$")


@dataclass(frozen=True)
class NavRow:
    isin: str
    scheme_code: str
    name: str
    nav: float
    as_of: date


def _cache_dir() -> Path:
    root = os.environ.get("SKAS_DATA_HOME") or str(Path.home() / ".skas_data")
    d = Path(root) / "amfi"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_date(s: str) -> date | None:
    s = s.strip()
    if not _DATE_RE.match(s):
        return None
    try:
        return datetime.strptime(s, "%d-%b-%Y").date()
    except ValueError:
        return None


# Column aliases across AMFI's layouts. Matched case-insensitively as substrings, so a
# renamed header ("NAV Name" vs "Scheme Name") still lands.
_COL_ALIASES = {
    "code": ("scheme code",),
    "isin_a": ("isin div payout", "isin growth"),
    "isin_b": ("isin div reinvestment",),
    "name": ("scheme name", "nav name"),
    "plan": ("plan",),
    "option": ("option",),
    "nav": ("net asset value", "nav"),
    "date": ("date",),
}


def _header_map(line: str) -> dict[str, int] | None:
    """Map an AMFI header row to column indexes, or None if this isn't a header."""
    cells = [c.strip().lower() for c in line.split(";")]
    if len(cells) < 5 or "scheme code" not in cells[0]:
        return None
    out: dict[str, int] = {}
    for field, aliases in _COL_ALIASES.items():
        for i, cell in enumerate(cells):
            if any(a in cell for a in aliases) and i not in out.values():
                out[field] = i
                break
    return out if {"isin_a", "nav", "date"} <= out.keys() else None


def _cell(parts: list[str], cols: dict[str, int], field: str) -> str:
    i = cols.get(field)
    return parts[i] if i is not None and i < len(parts) else ""


def _scheme_name(parts: list[str], cols: dict[str, int]) -> str:
    """Join name + plan + option, skipping any part the name already contains — the historical
    report's "NAV Name" bakes the plan and option in, so appending them again yields
    "… - Direct Plan - Growth - Direct Plan"."""
    base = _cell(parts, cols, "name")
    out = [base] if base else []
    for field in ("plan", "option"):
        piece = _cell(parts, cols, field)
        if piece and piece != "-" and piece.lower() not in base.lower():
            out.append(piece)
    return " - ".join(out)


def parse_navall(text: str) -> dict[str, NavRow]:
    """Index an AMFI NAV payload by ISIN (upper-cased). Both ISIN columns map to the same row.

    **Columns are located from the HEADER, never by fixed position.** AMFI ships at least
    three layouts and they do not agree on where anything is:

      * six columns  — ``code;isin;isin;name;nav;date`` (what its docs describe)
      * eight        — ``code;isin;isin;name;plan;option;nav;date`` (today's NAVAll.txt)
      * historical   — ``code;name;plan;option;isin;isin;nav;date`` (the dated report)

    The ISINs move from index 1-2 to index 4-5 between the last two, so a positional parser
    reads a scheme NAME as an ISIN and silently indexes nothing. Falling back to reading from
    the ENDS (code first, date last, NAV second-to-last) covers a payload with no header at
    all, which is how the first two layouts were handled before the third turned up.

    Lines with too few semicolons are AMC headings and blanks; suspended schemes publish
    "N.A." for a NAV and are skipped rather than priced at zero.
    """
    out: dict[str, NavRow] = {}
    cols: dict[str, int] | None = None

    for line in text.splitlines():
        if cols is None:
            cols = _header_map(line)
            if cols is not None:
                continue

        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 6:
            continue
        if not parts[0].isdigit():          # the header row, and any stray prose
            continue

        if cols is not None:
            code = _cell(parts, cols, "code")
            isins = (_cell(parts, cols, "isin_a"), _cell(parts, cols, "isin_b"))
            nav_s, date_s = _cell(parts, cols, "nav"), _cell(parts, cols, "date")
            name = _scheme_name(parts, cols)
        else:
            code, isins = parts[0], (parts[1], parts[2])
            nav_s, date_s = parts[-2], parts[-1]
            name = " - ".join(p for p in parts[3:-2] if p and p != "-")

        as_of = _parse_date(date_s)
        if as_of is None:
            continue
        try:
            nav = float(nav_s)
        except ValueError:
            continue                        # suspended schemes publish "N.A."
        for isin in (i.upper() for i in isins):
            if isin and isin != "-":
                out[isin] = NavRow(isin=isin, scheme_code=code, name=name, nav=nav, as_of=as_of)
    return out


def _fetch(session: requests.Session | None = None) -> str:
    http = session or requests
    resp = http.get(NAV_URL, timeout=_TIMEOUT)
    resp.raise_for_status()
    # AMFI serves the file without a charset, so requests guesses ISO-8859-1 and mangles the
    # apostrophes in names like "Children's Fund". The payload is UTF-8; say so.
    resp.encoding = "utf-8"
    return resp.text


def _prune(dirpath: Path) -> None:
    files = sorted(dirpath.glob("NAVAll-*.txt"))
    for stale in files[:-_KEEP_DAYS]:
        try:
            stale.unlink()
        except OSError:  # pragma: no cover - best effort
            pass


def refresh(*, today: date | None = None, session: requests.Session | None = None) -> Path:
    """Download today's NAVAll and cache it. Returns the cached path.

    Named for the DAY WE FETCHED, not the NAV date inside — two fetch days can carry the same
    NAV date (a weekend), and the previous-file lookup wants fetch order."""
    day = today or date.today()
    path = _cache_dir() / f"NAVAll-{day.isoformat()}.txt"
    if path.exists() and path.stat().st_size > 0:
        return path
    text = _fetch(session)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)  # local dir, never a Drive-synced one — atomic rename is safe here
    _prune(_cache_dir())
    return path


def backfill(day: date, *, session: requests.Session | None = None) -> Path | None:
    """Cache one PAST day's NAVs from the dated report. Returns the path, or None if that day
    published nothing (a weekend or a holiday).

    Day change needs two publications, so on the first day of tracking there is nothing to
    compare against and every fund reads 0.00 until tomorrow. One backfill removes that wait.
    """
    path = _cache_dir() / f"NAVAll-{day.isoformat()}.txt"
    if path.exists() and path.stat().st_size > 0:
        return path
    http = session or requests
    stamp = day.strftime("%d-%b-%Y")
    resp = http.get(HISTORY_URL, params={"frmdt": stamp, "todt": stamp}, timeout=_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    if not parse_navall(resp.text):
        return None
    tmp = path.with_suffix(".tmp")
    tmp.write_text(resp.text, encoding="utf-8")
    tmp.replace(path)
    _prune(_cache_dir())
    return path


def _cached_files() -> list[Path]:
    return sorted(_cache_dir().glob("NAVAll-*.txt"))


def load(
    *, today: date | None = None, session: requests.Session | None = None, fetch: bool = True
) -> tuple[dict[str, NavRow], dict[str, NavRow]]:
    """``(latest_by_isin, previous_by_isin)`` — the current NAVs and the prior publication.

    ``previous`` is assembled PER ISIN across every older cached file, newest first, rather
    than taken wholesale from one of them. AMFI publishes on weekends too, but only for
    overnight and liquid schemes — the Sunday file carries ~630 of 14,137 — so treating the
    newest older file as *the* prior leaves every equity fund with no comparison and a day
    change of zero. Falling through per ISIN lets a fund missing from Sunday find its Friday
    NAV, while a liquid fund still gets Sunday's.

    Empty when nothing older is cached; callers must treat a missing prior as unknown."""
    if fetch:
        try:
            refresh(today=today, session=session)
        except Exception:  # pragma: no cover - a stale cache still beats no NAVs
            logger.warning("AMFI NAV fetch failed; falling back to the cache", exc_info=True)

    files = _cached_files()
    if not files:
        return {}, {}
    latest = parse_navall(files[-1].read_text(encoding="utf-8"))
    if not latest:
        return {}, {}
    latest_date = next(iter(latest.values())).as_of

    previous: dict[str, NavRow] = {}
    for older in reversed(files[:-1]):
        rows = parse_navall(older.read_text(encoding="utf-8"))
        if not rows or next(iter(rows.values())).as_of >= latest_date:
            continue
        for isin, row in rows.items():
            previous.setdefault(isin, row)   # the newest older file wins, per ISIN
    return latest, previous


def search(query: str, limit: int = 20, *, rows: dict[str, NavRow] | None = None) -> list[NavRow]:
    """Substring match over scheme names — how a fund gets picked in the Add-holding modal.
    All query words must appear, so "parag flexi direct" finds the one row that matters."""
    table = rows if rows is not None else load(fetch=False)[0]
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    seen: set[str] = set()
    hits: list[NavRow] = []
    for row in table.values():
        if row.scheme_code in seen:
            continue
        low = row.name.lower()
        if all(w in low for w in words):
            seen.add(row.scheme_code)
            hits.append(row)
    hits.sort(key=lambda r: r.name)
    return hits[:limit]


def stale_days(as_of: date, *, today: date | None = None) -> int:
    """Calendar days between a NAV's date and today — what the UI badges as staleness.
    One is normal (see the module docstring); three-plus means the feed or the fetch is stuck."""
    return max((today or date.today()) - as_of, timedelta(0)).days
