"""live_cycles — read a run's CURRENT cycle off its transaction log.

A cycle is the stretch between the book going non-flat and coming back to flat. The Live
tile needs three things no snapshot carries: WHEN the open cycle began, the underlying's spot
at that moment (every option trade event is stamped ``underlying_spot`` at execution —
``manager._tag_underlying_spot``), and how much was realized BEFORE it, so the cycle's P&L
can be read off the overall series as ``overall − realized_before``. Everything here is
DERIVED from the persisted transaction log, never stored, so a recovered run answers the
same as the one that traded.

The sampled greeks history records UNREALIZED P&L only (``persistence.record_greeks``);
``realized_cumulative`` gives each sample the realized total booked up to it, which is what
turns that series into an overall one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skas_algo.engine.options.instrument import parse as parse_option

IST = timezone(timedelta(hours=5, minutes=30))

_OPENS = {"BUY": 1, "SHORT": -1}
_CLOSES = {"SELL": -1, "COVER": 1}


def _as_dt(v) -> datetime | None:
    """A transaction stamp as an AWARE datetime. In memory it is a datetime; persisted it is an
    ISO string with its +05:30; a naive value is IST (the engine clock)."""
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v))
        except ValueError:
            return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=IST)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _on_underlying(txn: dict, underlying: str | None) -> bool:
    if not underlying:
        return True
    ticker = str(txn.get("ticker") or "")
    inst = parse_option(ticker)
    name = inst.underlying if inst is not None else ticker
    return name.upper() == underlying.upper()


def cycle_info(txns: list[dict], underlying: str | None = None) -> dict:
    """``{open, entry_at, entry_spot, realized_before, last}`` for a run's transaction log.

    ``entry_spot`` is the first stamped spot ON ``underlying`` inside the cycle (a
    NIFTY+SENSEX run opens both books in one decision; the tile shows one index). ``last`` is
    the most recent CLOSED cycle — entry/exit stamps and spots and its P&L — so a flat tile
    can still show what the last cycle did. A SETTLE closes toward flat whichever way the
    lot faced."""
    held: dict[str, int] = {}
    realized = 0.0
    is_open = False
    entry_at: datetime | None = None
    entry_spot: float | None = None
    realized_before = 0.0
    last: dict | None = None
    for t in txns:
        ticker = str(t.get("ticker") or "")
        act = str(t.get("action") or "").upper()
        units = int(t.get("units") or 0)
        was_flat = not any(held.values())
        cur = held.get(ticker, 0)
        if act in _OPENS:
            held[ticker] = cur + _OPENS[act] * units
        elif act in _CLOSES:
            held[ticker] = cur + _CLOSES[act] * units
        elif act == "SETTLE" and cur:
            held[ticker] = cur - (units if cur > 0 else -units)
        if held.get(ticker) == 0:
            held.pop(ticker, None)
        now_flat = not any(held.values())
        if was_flat and not now_flat:
            is_open = True
            entry_at = _as_dt(t.get("date"))
            entry_spot = None
            realized_before = realized
        if is_open and entry_spot is None and _on_underlying(t, underlying):
            spot = t.get("underlying_spot")
            if spot is not None:
                entry_spot = float(spot)
        realized += float(t.get("profit") or 0.0)
        if is_open and now_flat:
            exit_spot = t.get("underlying_spot") if _on_underlying(t, underlying) else None
            last = {
                "entry_at": _iso(entry_at),
                "exit_at": _iso(_as_dt(t.get("date"))),
                "entry_spot": entry_spot,
                "exit_spot": float(exit_spot) if exit_spot is not None else None,
                "pnl": round(realized - realized_before, 2),
                "realized_before": round(realized_before, 2),
            }
            is_open = False
            entry_at = None
            entry_spot = None
    return {
        "open": is_open,
        "entry_at": _iso(entry_at) if is_open else None,
        "entry_spot": entry_spot if is_open else None,
        # While flat this is the whole realized total, so "overall − realized_before" reads 0.
        "realized_before": round(realized_before if is_open else realized, 2),
        "last": last,
    }


def realized_cumulative(txns: list[dict], stamps: list[datetime]) -> list[float]:
    """For each (ascending, AWARE) sample stamp, the realized P&L booked at or before it."""
    events = sorted(
        ((d, float(t.get("profit") or 0.0)) for t in txns if (d := _as_dt(t.get("date")))),
        key=lambda e: e[0],
    )
    out: list[float] = []
    acc = 0.0
    j = 0
    for s in stamps:
        while j < len(events) and events[j][0] <= s:
            acc += events[j][1]
            j += 1
        out.append(round(acc, 2))
    return out


def daily_pnl(txns: list[dict], unrealized_eod: dict[str, float] | None = None) -> list[dict]:
    """One row per IST trading day the run touched: realized (gross — the Live KPI's basis)
    and charges booked that day, the running realized total, the day's LAST sampled
    unrealized (``unrealized_eod``, from the greeks history; 0 = flat at the close), and
    ``overall`` = realized_cum + that. Days the book was merely HELD through (samples, no
    fills) are rows too, so a positional run's line does not jump across them."""
    realized: dict[str, float] = {}
    charges: dict[str, float] = {}
    closes: dict[str, int] = {}
    # Whether the book was FLAT after the day's last fill. The greeks samples stop once a book
    # is flat, so an intraday run's last sample of the day is the moment BEFORE its square-off
    # — carrying that unrealized past the close would count the day's P&L twice (run 248,
    # 2026-08-07: realized 6,796 + a stale 6,815 "open"). A flat close is 0, whatever the sample.
    held: dict[str, int] = {}
    flat_after: dict[str, bool] = {}
    stamped = sorted(((d, t) for t in txns if (d := _as_dt(t.get("date")))), key=lambda e: e[0])
    for d, t in stamped:
        key = d.astimezone(IST).date().isoformat()
        realized[key] = realized.get(key, 0.0) + float(t.get("profit") or 0.0)
        charges[key] = charges.get(key, 0.0) + float(t.get("charge") or 0.0)
        act = str(t.get("action") or "").upper()
        if act in ("SELL", "COVER", "SETTLE"):
            closes[key] = closes.get(key, 0) + 1
        ticker = str(t.get("ticker") or "")
        units = int(t.get("units") or 0)
        cur = held.get(ticker, 0)
        if act in _OPENS:
            held[ticker] = cur + _OPENS[act] * units
        elif act in _CLOSES:
            held[ticker] = cur + _CLOSES[act] * units
        elif act == "SETTLE" and cur:
            held[ticker] = cur - (units if cur > 0 else -units)
        if held.get(ticker) == 0:
            held.pop(ticker, None)
        flat_after[key] = not any(held.values())
    eod = dict(unrealized_eod or {})
    out: list[dict] = []
    cum = 0.0
    flat = True  # a day with samples but no fills inherits the last fill's state
    for key in sorted(set(realized) | set(eod)):
        cum += realized.get(key, 0.0)
        flat = flat_after.get(key, flat)
        u = 0.0 if flat else float(eod.get(key) or 0.0)
        out.append({
            "date": key,
            "realized_day": round(realized.get(key, 0.0), 2),
            "charges_day": round(charges.get(key, 0.0), 2),
            "closes": closes.get(key, 0),
            "realized_cum": round(cum, 2),
            "unrealized_eod": round(u, 2),
            "overall": round(cum + u, 2),
        })
    return out
