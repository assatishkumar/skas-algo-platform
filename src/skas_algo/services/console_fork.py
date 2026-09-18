"""Fork a deployment's cycle into the Options Console replay (owner ask, 2026-09-18).

"Take any PAPER or LIVE position — even a past cycle — go back in time, make different
adjustments, and see whether the end result changes." The console already replays any
captured day, restores a book from a journal at each fill's OWN recorded price across many
days (`ConsoleSession.restore`), and settles expiries; the Simulator already walks a
journal's per-minute MTM over the 1-min store (`simulator._minute_series`). A deployment's
fills use the store's exact symbol format. So a fork is: the run's ACTUAL fills for one
cycle, converted to a console journal and restored on the cycle's entry day at the entry
minute — the owner steps forward watching what really happened, presses "fork here"
(`ConsoleSession.truncate_after_cursor` drops the actual fills after the cursor), trades
differently, and the strip compares the actual MTM at the cursor with the fork's.

Lives OUTSIDE `services/options_console/` (the `console_live` / `console_margin`
precedent): `simulator` imports the console session, so the package must never import
this module. It touches no order path — the run's trades are read, never written.

Honesty rules, said on screen rather than hidden: the actuals are the run's real fills
(bid/ask, at the broker); the fork's NEW fills are the store's last trade with no spread.
A contract outside the capture window (strike beyond ±10% of that day's spot, expiry >40d
at capture) is restored but never priced — listed as `uncaptured`. A fill outside
09:15–15:40 (a manual paper fill at 07:45) is clamped to the session and listed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from skas_algo.data.option_intraday_store import captured_days, load_day
from skas_algo.services.options_console.session import SESSION_CLOSE, SESSION_OPEN

logger = logging.getLogger("skas_algo.console")

# the run's action vocabulary → the console's. AVG_BUY is a second BUY on a held symbol
# (`execution.py`); SETTLE is the market's action and the session re-derives it.
_ACTIONS = {"BUY": "BUY", "AVG_BUY": "BUY", "SHORT": "SHORT", "SELL": "SELL", "COVER": "COVER"}
_OPEN_HHMM = SESSION_OPEN.strftime("%H:%M")
_CLOSE_HHMM = SESSION_CLOSE.strftime("%H:%M")


def _minute(value) -> str:
    """A trade's ``date`` → ``YYYY-MM-DDTHH:MM``. Over the API it is ``"YYYY-MM-DD HH:MM"``
    (naive IST); persisted in ``AlgoRun.state`` it is ISO, possibly with ``+05:30``; a
    datetime when read in-process. The offset is dropped, never converted — the engine's
    clock IS IST."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M")
    s = str(value or "").strip()
    if not s:
        raise ValueError("a trade without a date")
    if len(s) >= 16:
        return s[:10] + "T" + s[11:16]
    return s[:10] + "T00:00"        # a backtest's date-only stamp


def normalise_cycle(cycle: dict) -> dict:
    """One shape for a cycle from `run_cycles` (a stored-report cycle or a reconstruction),
    a `cycle_briefs` row (local or peer) or a cycle-detail model."""
    legs = cycle.get("legs_detail") or cycle.get("legs") or []
    symbols = sorted({str(lg.get("symbol")) for lg in legs if lg.get("symbol")}
                     | set(cycle.get("symbols") or []))
    entered = cycle.get("entered_at") or cycle.get("entry_date")
    exited = cycle.get("exited_at") or cycle.get("exit_date")
    net = cycle.get("net") if cycle.get("net") is not None else cycle.get("net_pnl")
    if net is None and cycle.get("pnl") is not None:
        net = cycle.get("pnl")
    return {
        "underlying": str(cycle.get("underlying") or (symbols[0].split("|")[0] if symbols
                                                      else "NIFTY")).upper(),
        "expiry": cycle.get("expiry"),
        "entered_at": _minute(entered) if entered else None,
        "exited_at": _minute(exited) if exited else None,
        "exit_reason": cycle.get("exit_reason"),
        "live": bool(cycle.get("live")),
        "net": float(net) if net is not None else None,
        "symbols": symbols,
        "index": cycle.get("index"),
    }


def cycle_to_journal(trades: list[dict], cycle: dict) -> tuple[list[dict], dict]:
    """The run's fills for ONE cycle → console journal rows, plus what was adjusted.

    Rows are the cycle's symbols inside its window. Each distinct minute is one undo
    group, so a basket the run placed in one decision undoes together. Charges are the
    run's own (`charge`), the spot the stamped `underlying_spot`."""
    symbols = set(cycle["symbols"])
    lo = (cycle.get("entered_at") or "")[:10]
    hi = (cycle.get("exited_at") or "9999")[:10]
    notes: dict = {"clamped": [], "dropped_settle": 0, "skipped": []}
    rows: list[dict] = []
    for t in trades:
        sym = str(t.get("ticker") or "")
        if sym not in symbols:
            continue
        when = _minute(t.get("date"))
        if not (lo <= when[:10] <= hi):
            continue
        act = str(t.get("action") or "").upper()
        if act == "SETTLE":
            notes["dropped_settle"] += 1
            continue
        if act not in _ACTIONS:
            raise ValueError(f"unknown trade action {act!r} on {sym}")
        hhmm = when[11:16]
        if hhmm < _OPEN_HHMM or hhmm > _CLOSE_HHMM:
            to = _OPEN_HHMM if hhmm < _OPEN_HHMM else _CLOSE_HHMM
            notes["clamped"].append({"symbol": sym, "from": when, "to": when[:11] + to})
            when = when[:11] + to
        rows.append({
            "at": when, "symbol": sym, "action": _ACTIONS[act],
            "units": float(t.get("units") or 0), "price": float(t.get("price") or 0),
            "charges": float(t.get("charge") or 0.0),
            "spot": t.get("underlying_spot"),
        })
    rows.sort(key=lambda r: r["at"])
    groups: dict[str, int] = {}
    for r in rows:
        r["group"] = groups.setdefault(r["at"], len(groups) + 1)
    return rows, notes


def _uncaptured(day: date, underlying: str, symbols: list[str]) -> list[str]:
    try:
        df = load_day(day, underlying=underlying, columns=["symbol"])
        have = set(df["symbol"].unique().tolist()) if df is not None and len(df) else set()
    except Exception:  # pragma: no cover - a torn file must not block a fork
        logger.exception("console_fork: could not read %s", day)
        return []
    return [s for s in symbols if s not in have]


def fork_spec(*, cycle: dict, trades: list[dict], capital: float, source: str,
              run_id: int, label: str, run_name: str | None = None) -> dict:
    """Everything a console session needs to open AS the cycle: where to open, the tape to
    restore, and the `fork` record the comparison strip reads."""
    from skas_algo.services.simulator import _minute_series   # local: simulator imports the console

    if not cycle.get("entered_at"):
        raise ValueError("the cycle has no entry fill")
    journal, notes = cycle_to_journal(trades, cycle)
    if not journal:
        raise ValueError("no fills of this cycle in the run's trade log")
    entry_day = cycle["entered_at"][:10]
    days = captured_days()
    later = [d for d in days if d >= entry_day]
    if not later:
        if entry_day >= date.today().isoformat():
            raise ValueError(f"{entry_day} is today — its bars land in the store after 16:00")
        raise ValueError(f"{entry_day} is not in the 1-min store (it holds "
                         f"{days[0] if days else 'nothing'} → {days[-1] if days else '—'})")
    day = later[0]
    moved = day != entry_day
    at = _OPEN_HHMM if moved else cycle["entered_at"][11:16]
    symbols = list(cycle["symbols"])
    expiry = min(s.split("|")[1] for s in symbols) if symbols else cycle.get("expiry")
    uncaptured = _uncaptured(date.fromisoformat(day), cycle["underlying"], symbols)
    try:
        pts = _minute_series(journal, cycle["underlying"], until=cycle.get("exited_at"))
        actual_series = [[p["m"], round(float(p["mtm"]), 2)] for p in pts]
    except Exception:  # pragma: no cover - a torn tape must not block a fork
        logger.exception("console_fork: actual series failed for run %s", run_id)
        actual_series = []
    fork = {
        "source": source, "run_id": run_id, "index": cycle.get("index"), "label": label,
        "run_name": run_name or label, "underlying": cycle["underlying"],
        "entered_at": cycle["entered_at"], "exited_at": cycle.get("exited_at"),
        "exit_reason": cycle.get("exit_reason"), "live": cycle.get("live", False),
        "actual": {"net": cycle.get("net"), "fills": len(journal),
                   "entered_at": cycle["entered_at"], "exited_at": cycle.get("exited_at"),
                   "exit_reason": cycle.get("exit_reason"), "live": cycle.get("live", False)},
        "actual_series": actual_series,
        "entry_day_uncaptured": moved, "opened_day": day,
        "clamped": notes["clamped"], "uncaptured": uncaptured,
        "forked_at": None,
    }
    return {
        "label": label, "underlying": cycle["underlying"], "day": day, "at": at,
        "expiry": expiry, "capital": float(capital or 500_000),
        "restore": {"journal": journal, "alerts": [], "bookmarks": [], "discarded": []},
        "fork": fork,
    }
