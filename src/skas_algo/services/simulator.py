"""The Simulator — manual backtesting, cycle by cycle, stored as an ordinary run.

Owner design (2026-09-11): a Simulator strategy is *a backtest whose decisions are yours*.
It is an `Algo` (name, playbook, underlying, capital; `strategy_id = manual_sim`) with ONE
`AlgoRun` whose `trade_log` grows as cycles are banked and whose `metrics` is the standard
report rebuilt after every bank — so Run detail, Analyze and Compare render it unchanged.
`AlgoRun.state["sim"]` is the LEDGER: every banked cycle's journal slice (the console's
own tape — a cycle is fully reconstructible from it), the open cycle's journal (autosaved
from the console so an evicted session loses nothing), the equity and the next day.

Decisions that shape the rules below (owner, 2026-09-11):
* a cycle runs from the first fill on a flat book to the book going FLAT again (a close by
  hand or an expiry settlement) — not the console's expiry-based "done";
* capital COMPOUNDS: a cycle opens with the equity after the last banked cycle;
* the run is HIDDEN from the Runs list (`routes/backtest.py`) and lives on `/simulator`;
* the Bank sheet prompts automatically the moment the book is flat.

Nothing unrealised enters the stats. The journal rows are the console's (`session._charge`):
{at, symbol, action BUY|SHORT|SELL|COVER|SETTLE, group, units, price, charges, spot}. This
module re-derives a cycle's legs and P&L from those rows with its own FIFO walk, so the
ledger never depends on a live session object. Deliberately outside `options_console/`
(which may not touch the DB) and outside `live/` (no order path anywhere near this).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.data.option_intraday_store import captured_days
from skas_algo.db.enums import InstrumentClass, TradingMode
from skas_algo.db.models import Algo, AlgoRun
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.live.holidays import next_trading_day
from skas_algo.services.intraday_replay import _options_report, _to_report

logger = logging.getLogger(__name__)

STRATEGY_ID = "manual_sim"
_OPENS = {"BUY": 1, "SHORT": -1}
_CLOSES = ("SELL", "COVER", "SETTLE")
_CHARGE_KEYS = ("brokerage", "stt", "exchange", "sebi", "stamp", "gst")


# ----------------------------------------------------------------------------- rows
def _run_of(db: Session, algo_id: int) -> tuple[Algo, AlgoRun]:
    algo = db.get(Algo, algo_id)
    if algo is None or algo.strategy_id != STRATEGY_ID:
        raise KeyError(f"no Simulator strategy #{algo_id}")
    run = db.execute(select(AlgoRun).where(AlgoRun.algo_id == algo_id)
                     .order_by(AlgoRun.id.asc())).scalars().first()
    if run is None:
        raise KeyError(f"Simulator strategy #{algo_id} has no run")
    return algo, run


def _sim(run: AlgoRun) -> dict:
    st = dict(run.state or {})
    sim = dict(st.get("sim") or {})
    sim.setdefault("cycles", [])
    sim.setdefault("open", None)
    sim.setdefault("capital_mode", "compound")
    sim.setdefault("equity", None)
    sim.setdefault("next_day", None)
    return sim


def _put_sim(run: AlgoRun, sim: dict) -> None:
    st = dict(run.state or {})
    st["sim"] = sim
    run.state = st


# ----------------------------------------------------------------------------- create / list
def create(db: Session, *, name: str, underlying: str, capital: float,
           playbook: str | None = None, start_day: str | None = None) -> dict:
    u = underlying.upper()
    days = captured_days()
    if not days:
        raise ValueError("no captured days in the 1-min store")
    first = start_day or days[-1]
    if first not in days:
        raise ValueError(f"{first} is not a captured day")
    params = {"underlying": u, "instrument_class": "DERIV", "playbook": playbook or "",
              "capital_mode": "compound", "data_basis": "intraday", "sim": True,
              "start_day": first}
    algo = Algo(name=name.strip() or f"Simulator {u}", notes=playbook, strategy_id=STRATEGY_ID,
                instrument_class=InstrumentClass.DERIV, mode=TradingMode.BACKTEST,
                capital=float(capital), params=params)
    db.add(algo)
    db.flush()
    now = datetime.now(UTC)
    run = AlgoRun(algo_id=algo.id, mode=TradingMode.BACKTEST, started_at=now, stopped_at=now,
                  params_snapshot=params, metrics=_empty_report(float(capital)), trade_log=[],
                  state={"sim": {"cycles": [], "open": None, "capital_mode": "compound",
                                 "equity": float(capital), "next_day": first}})
    db.add(run)
    db.flush()
    return get(db, algo.id)


def _empty_report(capital: float) -> dict:
    return {"metrics": {"Total Return %": 0.0, "Final Equity": capital, "Max Drawdown %": 0.0,
                        "Total Trades": 0, "Win Rate %": 0.0, "Net Realized P&L": 0.0,
                        "Cash Balance": capital, "Total Charges": 0.0, "Days Replayed": 0},
            "equity_curve": []}


def list_all(db: Session) -> list[dict]:
    rows = db.execute(select(Algo).where(Algo.strategy_id == STRATEGY_ID)
                      .order_by(Algo.id.desc())).scalars().all()
    out = []
    for algo in rows:
        try:
            out.append(summary(db, algo.id))
        except KeyError:
            continue
    return out


def summary(db: Session, algo_id: int) -> dict:
    algo, run = _run_of(db, algo_id)
    sim = _sim(run)
    cycles = sim["cycles"]
    net = round(sum(c["net"] for c in cycles), 2)
    wins = sum(1 for c in cycles if c["net"] > 0)
    return {"id": algo.id, "run_id": run.id, "name": algo.name,
            "underlying": algo.params.get("underlying"),
            "capital": float(algo.capital), "equity": float(sim["equity"] or algo.capital),
            "cycles": len(cycles), "net": net,
            "win_rate": round(100.0 * wins / len(cycles), 1) if cycles else None,
            "last_exit": cycles[-1]["exited"] if cycles else None,
            "open": bool(sim["open"] and sim["open"].get("journal")),
            "next_day": sim["next_day"], "created_at": algo.created_at.isoformat()
            if getattr(algo, "created_at", None) else None}


def get(db: Session, algo_id: int) -> dict:
    algo, run = _run_of(db, algo_id)
    sim = _sim(run)
    return {**summary(db, algo_id), "playbook": algo.notes or "",
            "capital_mode": sim["capital_mode"],
            "cycle_rows": [{k: v for k, v in c.items() if k != "journal"} for c in sim["cycles"]],
            "open_cycle": (
                {"day": sim["open"].get("day"), "clock": sim["open"].get("clock"),
                 "expiry": sim["open"].get("expiry"),
                 "fills": len([r for r in sim["open"].get("journal", [])
                               if r.get("action") in _OPENS or r.get("action") in _CLOSES]),
                 "entered": next((r["at"] for r in sim["open"].get("journal", [])
                                  if r.get("action") in _OPENS), None)}
                if sim["open"] and sim["open"].get("journal") else None),
            "metrics": (run.metrics or {}).get("metrics", {}),
            "equity_curve": (run.metrics or {}).get("equity_curve", [])}


def update(db: Session, algo_id: int, *, name: str | None = None,
           playbook: str | None = None) -> dict:
    algo, _run = _run_of(db, algo_id)
    if name is not None and name.strip():
        algo.name = name.strip()
    if playbook is not None:
        algo.notes = playbook
        algo.params = {**(algo.params or {}), "playbook": playbook}
    db.flush()
    return get(db, algo_id)


def delete(db: Session, algo_id: int) -> None:
    from skas_algo.services.runs import delete_algo_cascade

    _run_of(db, algo_id)
    delete_algo_cascade(db, algo_id)


# ----------------------------------------------------------------------------- the console hand-off
def open_spec(db: Session, algo_id: int) -> dict:
    """What the console needs to open THIS strategy's next session: the open cycle where it
    stands (day/clock/expiry + journal to restore), else a fresh day at 09:30 with the
    equity as capital."""
    algo, run = _run_of(db, algo_id)
    sim = _sim(run)
    u = algo.params.get("underlying", "NIFTY")
    capital = float(sim["equity"] or algo.capital)
    if sim["open"] and sim["open"].get("journal"):
        o = sim["open"]
        return {"id": algo.id, "name": algo.name, "underlying": u, "day": o["day"],
                "at": o.get("clock") or "09:30", "expiry": o.get("expiry"),
                "capital": float(o.get("capital") or capital),
                "restore": {"journal": o["journal"], "alerts": o.get("alerts") or [],
                            "bookmarks": o.get("bookmarks") or []},
                "cycle_no": len(sim["cycles"]) + 1}
    day = sim["next_day"] or algo.params.get("start_day")
    days = captured_days()
    if day not in days:
        later = [d for d in days if d >= (day or "")]
        day = later[0] if later else (days[-1] if days else None)
    return {"id": algo.id, "name": algo.name, "underlying": u, "day": day, "at": "09:30",
            "expiry": None, "capital": capital, "restore": None,
            "cycle_no": len(sim["cycles"]) + 1}


def cycle_journal(db: Session, algo_id: int, n: int) -> dict:
    """A banked cycle's tape + where to open the console to watch it back (read-only)."""
    algo, run = _run_of(db, algo_id)
    sim = _sim(run)
    c = next((x for x in sim["cycles"] if int(x["n"]) == int(n)), None)
    if c is None:
        raise KeyError(f"cycle {n} not banked on Simulator #{algo_id}")
    return {"id": algo.id, "name": algo.name, "underlying": algo.params.get("underlying"),
            "n": c["n"], "day": c["exit_day"], "at": (c["exited"] or "T15:30")[11:16],
            "expiry": c.get("expiry"), "capital": c["capital_before"],
            "restore": {"journal": c["journal"], "alerts": [], "bookmarks": []}}


def autosave(db: Session, algo_id: int, payload: dict) -> dict:
    """The console's `save_payload()` for the OPEN cycle, stored as-is (journal + where the
    cursor stands). Called after every action in SIM mode."""
    _algo, run = _run_of(db, algo_id)
    sim = _sim(run)
    journal = [r for r in (payload.get("journal") or []) if r.get("action") != "NOOP"]
    sim["open"] = {"day": payload.get("day"), "clock": payload.get("clock"),
                   "expiry": payload.get("expiry"), "capital": payload.get("capital"),
                   "journal": journal, "alerts": payload.get("alerts") or [],
                   "bookmarks": payload.get("bookmarks") or [],
                   "saved_at": datetime.now(UTC).isoformat(timespec="seconds")}
    _put_sim(run, sim)
    db.flush()
    return {"fills": len(journal), "flat": is_flat(journal), "traded": has_traded(journal)}


def set_next_day(db: Session, algo_id: int, day: str) -> dict:
    _algo, run = _run_of(db, algo_id)
    if day not in captured_days():
        raise ValueError(f"{day} is not a captured day")
    sim = _sim(run)
    sim["next_day"] = day
    _put_sim(run, sim)
    db.flush()
    return get(db, algo_id)


# ----------------------------------------------------------------------------- the cycle
def has_traded(journal: list[dict]) -> bool:
    return any(r.get("action") in _OPENS for r in journal)


def is_flat(journal: list[dict]) -> bool:
    held: dict[str, float] = {}
    for r in sorted(journal, key=lambda x: x["at"]):
        a, s, u = r.get("action"), r["symbol"], float(r.get("units") or 0)
        if a in _OPENS:
            held[s] = held.get(s, 0.0) + _OPENS[a] * u
        elif a in _CLOSES:
            cur = held.get(s, 0.0)
            held[s] = cur - (u if cur > 0 else -u) if a != "SETTLE" else 0.0
            if a != "SETTLE" and abs(held[s]) < 1e-9:
                held[s] = 0.0
    return all(abs(v) < 1e-9 for v in held.values())


def reconstruct(journal: list[dict], underlying: str) -> dict:
    """A cycle from its rows: per-leg entry/exit (FIFO, one record per symbol per open —
    the console opens one record per click), gross realized, charges, stamps and spots."""
    rows = sorted((r for r in journal if r.get("action") != "NOOP"), key=lambda x: x["at"])
    open_legs: dict[str, list[dict]] = {}
    legs: list[dict] = []
    realized = 0.0
    charges = 0.0
    entered = None
    entry_spot = None
    last_spot = None
    for r in rows:
        a, s = r.get("action"), r["symbol"]
        u = float(r.get("units") or 0)
        px = float(r.get("price") or 0)
        charges += float(r.get("charges") or 0)
        if r.get("spot"):
            last_spot = float(r["spot"])
        if a in _OPENS:
            if entered is None:
                entered, entry_spot = r["at"], (float(r["spot"]) if r.get("spot") else None)
            open_legs.setdefault(s, []).append({"units": u, "price": px, "at": r["at"],
                                                "dir": _OPENS[a]})
        elif a in _CLOSES:
            left = u
            q = open_legs.get(s, [])
            while left > 1e-9 and q:
                lot = q[0]
                take = min(left, lot["units"])
                pnl = lot["dir"] * (px - lot["price"]) * take
                realized += pnl
                _u, e_iso, k, right = s.split("|")
                try:
                    per_lot = lot_size_for(_u, date.fromisoformat(e_iso))
                except Exception:
                    per_lot = 0
                legs.append({"symbol": s, "underlying": _u, "strike": float(k), "right": right,
                             "side": "short" if lot["dir"] < 0 else "long", "expiry": e_iso,
                             "entry_date": lot["at"], "entry_premium": lot["price"],
                             "exit_date": r["at"], "exit_price": px,
                             "exit_action": a,
                             "exit_reason": "settle" if a == "SETTLE" else "manual",
                             "units": take, "lots": int(take // per_lot) if per_lot else 0,
                             "multiplier": 1,
                             "holding_days": _holding_days(lot["at"], r["at"]),
                             "pnl": round(pnl, 2)})
                lot["units"] -= take
                left -= take
                if lot["units"] <= 1e-9:
                    q.pop(0)
    exited = rows[-1]["at"] if rows else None
    exit_spot = last_spot
    return {"legs": legs, "realized": round(realized, 2), "charges": round(charges, 2),
            "net": round(realized - charges, 2), "entered": entered, "exited": exited,
            "entry_spot": entry_spot, "exit_spot": exit_spot,
            "symbols": sorted({leg["symbol"] for leg in legs}),
            "expiry": max((leg["expiry"] for leg in legs), default=None),
            "premium": round(sum(-leg["entry_premium"] * leg["units"]
                                 * (1 if leg["side"] == "long" else -1) for leg in legs), 2)}


def _holding_days(entry_minute: str, exit_minute: str) -> float:
    e = datetime.fromisoformat(entry_minute.replace(" ", "T"))
    x = datetime.fromisoformat(exit_minute.replace(" ", "T"))
    whole = (x.date() - e.date()).days
    intraday = (x - datetime.combine(x.date(), e.time())).total_seconds() / (6.25 * 3600)
    return round(whole + max(-0.99, min(0.99, intraday)), 2)


def bank(db: Session, algo_id: int, *, note: str | None = None, tags: list[str] | None = None,
         margin: float | None = None, margin_source: str | None = None,
         payload: dict | None = None) -> dict:
    """Bank the OPEN cycle: its journal must have traded and be FLAT. Appends the cycle to
    the ledger, the fills to the run's trade_log, rebuilds the report, advances the equity
    (compounding) and the next day, and clears the open cycle."""
    algo, run = _run_of(db, algo_id)
    sim = _sim(run)
    if payload is not None:
        autosave(db, algo_id, payload)
        sim = _sim(run)
    o = sim.get("open") or {}
    journal = [r for r in o.get("journal", []) if r.get("action") != "NOOP"]
    if not has_traded(journal):
        raise ValueError("nothing to bank — the cycle has no fills")
    if not is_flat(journal):
        raise ValueError("the book is not flat — close or settle every leg before banking")
    u = algo.params.get("underlying", "NIFTY")
    rec = reconstruct(journal, u)
    n = len(sim["cycles"]) + 1
    capital_before = float(sim["equity"] or algo.capital)
    cycle = {"n": n, "entered": rec["entered"], "exited": rec["exited"],
             "entry_day": rec["entered"][:10] if rec["entered"] else o.get("day"),
             "exit_day": rec["exited"][:10] if rec["exited"] else o.get("day"),
             "realized": rec["realized"], "charges": rec["charges"], "net": rec["net"],
             "entry_spot": rec["entry_spot"], "exit_spot": rec["exit_spot"],
             "premium": rec["premium"], "expiry": rec["expiry"], "symbols": rec["symbols"],
             "legs": rec["legs"], "margin": float(margin) if margin else None,
             "margin_source": margin_source if margin else None,
             "rom_pct": (round(100.0 * rec["net"] / float(margin), 2) if margin else None),
             "note": (note or "").strip(), "tags": [t for t in (tags or []) if t],
             "capital_before": capital_before,
             "capital_after": round(capital_before + rec["net"], 2),
             "journal": journal, "banked_at": datetime.now(UTC).isoformat(timespec="seconds")}
    sim["cycles"].append(cycle)
    sim["equity"] = cycle["capital_after"]
    sim["open"] = None
    days = captured_days()
    nxt = next_trading_day(date.fromisoformat(cycle["exit_day"])).isoformat()
    later = [d for d in days if d >= nxt]
    sim["next_day"] = later[0] if later else None
    _put_sim(run, sim)
    run.trade_log = list(run.trade_log or []) + _trade_rows(journal, rec, n)
    run.metrics = build_report(sim, float(algo.capital), run.trade_log)
    run.stopped_at = datetime.now(UTC)
    db.flush()
    logger.info("SIM %s banked cycle %s: net %s over %s..%s", algo_id, n, rec["net"],
                rec["entered"], rec["exited"])
    return {**get(db, algo_id), "banked": {k: v for k, v in cycle.items() if k != "journal"}}


def _trade_rows(journal: list[dict], rec: dict, n: int) -> list[dict]:
    """The run's trade_log rows in the standard shape (intraday_replay's), profit on closes
    from the reconstructed legs (FIFO, so a partial close carries its own share)."""
    pnl_by_close: dict[tuple[str, str], float] = {}
    for leg in rec["legs"]:
        key = (leg["exit_date"], leg["symbol"])
        pnl_by_close[key] = pnl_by_close.get(key, 0.0) + leg["pnl"]
    out = []
    for r in sorted(journal, key=lambda x: x["at"]):
        a = r.get("action")
        if a not in _OPENS and a not in _CLOSES:
            continue
        row = {"date": r["at"], "ticker": r["symbol"], "action": a,
               "units": float(r.get("units") or 0), "price": float(r.get("price") or 0),
               "profit": (round(pnl_by_close.get((r["at"], r["symbol"]), 0.0), 2)
                          if a in _CLOSES else None),
               "charge": float(r.get("charges") or 0), "tag": f"cycle {n}",
               "underlying_spot": r.get("spot")}
        out.append(row)
    return out


def build_report(sim: dict, capital: float, trades: list[dict]) -> dict:
    """The standard report from the ledger: a daily equity curve of BANKED net (flat days
    carry; nothing unrealised), the cycles in the options sub-report's shape, and the
    per-cycle margins. Built whole or not at all (the ReportView footgun)."""
    cycles = sim.get("cycles") or []
    if not cycles:
        return _empty_report(capital)
    first = min(c["entry_day"] for c in cycles)
    last = max(c["exit_day"] for c in cycles)
    by_exit: dict[str, float] = {}
    for c in cycles:
        by_exit[c["exit_day"]] = by_exit.get(c["exit_day"], 0.0) + c["net"]
    curve = []
    d = date.fromisoformat(first)
    end = date.fromisoformat(last)
    equity = capital
    while d <= end:
        equity += by_exit.get(d.isoformat(), 0.0)
        curve.append({"date": d.isoformat(), "equity": round(equity, 2)})
        d = next_trading_day(d)
    rows = []
    positions = []
    margin_by_day = {}
    for c in cycles:
        legs = c["legs"]
        positions.extend(legs)
        ce = next((x for x in legs if x["right"] == "CE"), None)
        pe = next((x for x in legs if x["right"] == "PE"), None)
        rows.append({"underlying": legs[0]["underlying"] if legs else "",
                     "entry_date": c["entered"],
                     "expiry": c["expiry"], "legs": c["symbols"], "legs_detail": legs,
                     "premium_collected": c["premium"], "realized_pnl": c["net"],
                     "net_pnl": c["net"], "holding_days": _holding_days(c["entered"], c["exited"]),
                     "exit_date": c["exited"], "daily_pnl": [],
                     "underlying_entry": c["entry_spot"], "underlying_exit": c["exit_spot"],
                     "underlying_pct": (100.0 * (c["exit_spot"] - c["entry_spot"]) / c["entry_spot"]
                                        if c["entry_spot"] and c["exit_spot"] else None),
                     "exit_reason": "settle" if any(x["exit_action"] == "SETTLE" for x in legs)
                     else "manual", "ce": ce if len(legs) == 2 else None,
                     "pe": pe if len(legs) == 2 else None,
                     "note": c.get("note"), "tags": c.get("tags"), "n": c["n"]})
        if c.get("margin"):
            margin_by_day[c["exit_day"]] = max(margin_by_day.get(c["exit_day"], 0.0), c["margin"])
    charges_bd = {k: 0.0 for k in _CHARGE_KEYS}
    charges_bd["total"] = round(sum(c["charges"] for c in cycles), 2)
    options = _options_report(rows, positions, charges_bd, margin_by_day, {},
                              round(sum(c["net"] for c in cycles), 2))
    days = len({c["entry_day"] for c in cycles} | {c["exit_day"] for c in cycles})
    return _to_report(curve, trades, capital, charges_bd["total"], days, cycles=rows,
                      options=options)
