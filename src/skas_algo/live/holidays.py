"""NSE trading-holiday calendar for the live loop.

``is_market_open`` (quotes.py) and ``LiveBroker._check_rails`` only ever *close* the
market on a holiday — they never open one — so a wrong entry here is fail-safe in the
conservative direction: a spurious holiday makes the platform sit out a day (visible, an
owner notices), while a MISSED holiday just means orders get rejected by the closed
exchange → the run's ``order_error`` halt. Neither can place a wrong-priced real trade.

⚠️  VERIFY THE FESTIVAL DATES against the official NSE circular for the year. The
fixed-Gregorian national holidays below are certain; the festival/lunar dates
(Holi, the two Ids, Diwali, etc.) are PROVISIONAL and move year to year — reconcile them
with https://www.nseindia.com (Resources → Holidays) before relying on them.

Correct WITHOUT a code change via env (comma-separated ISO dates, ``SKAS_`` unprefixed):
  * ``NSE_HOLIDAYS_ADD``   — extra holidays to union in (a date the circular lists that we
    missed);
  * ``NSE_HOLIDAYS_REMOVE`` — dates to force-OPEN (we over-listed, or a special session
    such as Muhurat trading is actually open).
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache

# NSE equity/F&O trading holidays. Weekends are handled separately (they're closed anyway;
# listing a Saturday/Sunday here is harmless). Dates that fall on a weekend are annotated.
_BUILTIN: dict[int, tuple[tuple[date, str], ...]] = {
    2026: (
        (date(2026, 1, 26), "Republic Day"),
        (date(2026, 3, 4), "Holi"),                    # PROVISIONAL (lunar)
        (date(2026, 3, 21), "Id-ul-Fitr / Ramzan Id"),  # PROVISIONAL (lunar) — Saturday
        (date(2026, 4, 1), "Annual bank closing"),      # PROVISIONAL
        (date(2026, 4, 3), "Good Friday"),
        (date(2026, 4, 14), "Dr. Ambedkar Jayanti"),
        (date(2026, 5, 1), "Maharashtra Day"),
        (date(2026, 5, 27), "Bakri Id / Id-ul-Adha"),   # PROVISIONAL (lunar)
        (date(2026, 6, 26), "Muharram"),                # PROVISIONAL — Fri; bhavcopy-confirmed closed
        (date(2026, 8, 15), "Independence Day"),        # Saturday
        (date(2026, 9, 14), "Ganesh Chaturthi"),        # PROVISIONAL (lunar)
        (date(2026, 10, 2), "Gandhi Jayanti"),
        (date(2026, 10, 20), "Dussehra"),               # PROVISIONAL (lunar)
        (date(2026, 11, 9), "Diwali (Laxmi Pujan)"),    # PROVISIONAL (lunar) — Muhurat may open
        (date(2026, 11, 24), "Guru Nanak Jayanti"),     # PROVISIONAL (lunar)
        (date(2026, 12, 25), "Christmas"),
    ),
}


def _env_dates(var: str) -> frozenset[date]:
    raw = os.environ.get(var, "")
    out: set[date] = set()
    for tok in raw.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(date.fromisoformat(tok))
        except ValueError:  # pragma: no cover - a typo shouldn't crash the loop
            continue
    return frozenset(out)


@lru_cache(maxsize=8)
def _holidays_for(year: int) -> frozenset[date]:
    builtin = {d for d, _ in _BUILTIN.get(year, ())}
    builtin |= {d for d in _env_dates("NSE_HOLIDAYS_ADD") if d.year == year}
    builtin -= _env_dates("NSE_HOLIDAYS_REMOVE")
    return frozenset(builtin)


def is_nse_holiday(d: date) -> bool:
    """True if ``d`` is a listed NSE trading holiday (weekends are NOT holidays here —
    the caller checks weekday separately)."""
    return d in _holidays_for(d.year)


def holiday_name(d: date) -> str | None:
    """Human label for a holiday date (built-in list only), else None."""
    for hd, name in _BUILTIN.get(d.year, ()):
        if hd == d:
            return name
    return "NSE holiday" if is_nse_holiday(d) else None
