"""Cycle-detail model — the position lifecycle of ONE options cycle (entry → adjustments →
exit) for the Cycle Detail page (design_handoff_cycle_detail).

The saved report stores a cycle's legs flat (``legs_detail`` = per-leg entry→exit). This
reassembles them into the EVENT LOG the design is built around: a chronological list of
events, each closing and/or opening legs — E (entry), R1..Rn (rolls / straddle-cap+hedge),
T (exit) — with the reconstructed NET DELTA at each step (the strategies adjust ON delta, so
delta is what makes the story legible). Works for any options cycle: a fixed multi-leg
structure like batman collapses to just entry + exit (no adjustments), a delta_neutral
campaign shows every roll and the iron-fly cap.

Delta is not stored in backtests → reconstructed via Black-Scholes: each leg's IV is backed
out of its OPEN premium (`implied_vol`), then `delta` is evaluated at the spot/time of each
event (constant-per-leg-IV — a faithful reconstruction of the greeks the strategy saw).

Pure over prebuilt inputs (the route supplies the cycle, its trade rows, a spot lookup, and
the run's margin series), mirroring services/donchian_study.py / loss_study.py.
"""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.options import black_scholes as bs

# Event-kind vocabulary the frontend colours by (entry teal / roll purple / hedge amber /
# exit green|danger). Derived from the strategy's own reason tags where present.
_ENTRY_TAGS = {"dnm_entry", "ifm_entry", "batman", "call_ratio", "put_ratio", "hni",
               "STRATEGY", "entry"}
_HEDGE_TAGS = {"dnm_ironfly", "ifm_adjust", "ifm_adjust_roll"}
_ROLL_TAGS = {"dnm_roll"}
_EXIT_TAGS = {"target", "stop", "time", "expiry", "expiry_settle", "ironfly_payoff_neg",
              "manual", "eod"}


def _parse_ts(v) -> datetime:
    s = str(v).replace("T", " ")
    try:
        return datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return datetime.strptime(s[:10], "%Y-%m-%d")


def _years_to(expiry, when: datetime) -> float:
    exp = expiry if isinstance(expiry, date) else _parse_ts(expiry).date()
    return max((exp - when.date()).days, 0) / 365.0


def _leg_greeks(premium: float, spot: float, strike: float, t: float, r: float, right: str):
    """(iv, delta_per_share) backed out of a premium; (None, None) if unsolvable."""
    if not premium or not spot or t <= 0:
        return None, None
    iv = bs.implied_vol(premium, spot, strike, t, r, right)
    if iv is None:
        return None, None
    return iv, bs.delta(spot, strike, t, r, iv, right)


def build_cycle_detail(cycle: dict, trade_rows: list[dict], spot_fn, margin_series: list[dict],
                       *, index: int, run_id: int, strategy_id: str, name: str,
                       r: float = 0.065) -> dict:
    """Assemble the cycle-detail model. ``spot_fn(date) -> underlying close`` (cache, ffilled);
    ``trade_rows`` = the run trade-log rows belonging to this cycle (for the entry-side tag /
    event kind); ``margin_series`` = the run's per-day [{date, margin/value}] (for max margin)."""
    underlying = cycle.get("underlying") or "NIFTY"
    expiry = cycle.get("expiry")
    entry_ts = _parse_ts(cycle["entry_date"])
    exit_ts = _parse_ts(cycle["exit_date"]) if cycle.get("exit_date") else None
    entry_spot = cycle.get("underlying_entry")
    exit_spot = cycle.get("underlying_exit")

    # ---- legs (normalised) ----
    legs = []
    for i, d in enumerate(cycle.get("legs_detail") or []):
        side = d.get("side") or ("long" if d.get("dir", -1) > 0 else "short")
        legs.append({
            "ref": i, "symbol": d.get("symbol"), "right": d.get("right"),
            "strike": float(d.get("strike")), "units": int(d.get("units") or 0),
            "side": side, "dir": 1 if side == "long" else -1,
            "open_ts": _parse_ts(d["entry_date"]), "open_price": float(d.get("entry_premium") or 0),
            "close_ts": _parse_ts(d["exit_date"]) if d.get("exit_date") else None,
            "close_price": d.get("exit_price"), "pnl": float(d.get("pnl") or 0),
            "days": d.get("holding_days"), "exit_reason": d.get("exit_reason"),
        })

    # Per-leg constant IV backed out at its own open (spot at open ≈ entry_spot for the entry
    # legs; spot_fn(open date) for legs opened by a later adjustment).
    for lg in legs:
        s0 = (entry_spot if lg["open_ts"] == entry_ts
              else (spot_fn(lg["open_ts"].date()) or entry_spot))
        iv, dlt = _leg_greeks(lg["open_price"], s0, lg["strike"],
                              _years_to(expiry, lg["open_ts"]), r, lg["right"])
        lg["iv"], lg["open_delta"] = iv, dlt

    # tag lookup: (timestamp, symbol) -> reason tag, from the trade log (entry side included).
    tag_at = {}
    for t in trade_rows:
        tag_at[(_parse_ts(t["date"]), t.get("ticker"))] = t.get("tag")

    # ---- events: distinct timestamps where legs opened and/or closed ----
    stamps = sorted({lg["open_ts"] for lg in legs}
                    | {lg["close_ts"] for lg in legs if lg["close_ts"]})
    events = []
    roll_n = 0
    realized = 0.0
    for j, ts in enumerate(stamps):
        opened = [lg for lg in legs if lg["open_ts"] == ts]
        closed = [lg for lg in legs if lg["close_ts"] == ts]
        realized += sum(lg["pnl"] for lg in closed)
        tags = {tag_at.get((ts, lg["symbol"])) for lg in opened + closed} - {None}
        is_first, is_last = j == 0, j == len(stamps) - 1
        kind = _classify(is_first, is_last, opened, closed, tags)
        if kind in ("roll", "hedge"):
            roll_n += 1
            eid = f"R{roll_n}"
        else:
            eid = "E" if kind == "entry" else "T"
        spot = (entry_spot if is_first else exit_spot if is_last
                else (spot_fn(ts.date()) or entry_spot))
        # net delta of the OPEN book right after this event
        open_now = [lg for lg in legs if lg["open_ts"] <= ts
                    and (lg["close_ts"] is None or lg["close_ts"] > ts)]
        net_delta = _net_delta(open_now, spot, expiry, ts, r)
        events.append({
            "id": eid, "kind": kind, "at": ts.strftime("%Y-%m-%d %H:%M"),
            "spot": round(spot) if spot else None,
            "net_delta": round(net_delta, 2) if net_delta is not None else None,
            "reason": _reason(kind, tags, cycle),
            "realized_so_far": round(realized, 2),
            "closed": [_event_leg(lg, "close") for lg in closed],
            "opened": [_event_leg(lg, "open") for lg in opened],
        })

    ev_of_open = {lg["ref"]: _event_id_at(events, lg["open_ts"]) for lg in legs}
    ev_of_close = {lg["ref"]: _event_id_at(events, lg["close_ts"]) if lg["close_ts"] else None
                   for lg in legs}

    # ---- KPIs ----
    daily = cycle.get("daily_pnl") or []
    premium_traded = sum(abs(lg["units"] * lg["open_price"]) for lg in legs)
    n_roll = sum(1 for e in events if e["kind"] == "roll")
    n_hedge = sum(1 for e in events if e["kind"] == "hedge")
    max_margin = _max_margin(margin_series, entry_ts.date(), (exit_ts or entry_ts).date())

    return {
        "run_id": run_id, "index": index, "strategy_id": strategy_id, "run_name": name,
        "underlying": underlying, "expiry": str(expiry)[:10],
        "entered_at": cycle["entry_date"], "exited_at": cycle.get("exit_date"),
        "exit_reason": cycle.get("exit_reason"),
        "entry_spot": round(entry_spot) if entry_spot else None,
        "exit_spot": round(exit_spot) if exit_spot else None,
        "entry_vix": cycle.get("vix_entry"), "exit_vix": cycle.get("vix_exit"),
        "underlying_pct": cycle.get("underlying_pct"),
        "pnl": cycle.get("net_pnl"), "premium_traded": round(premium_traded),
        "days_held": cycle.get("holding_days"),
        "n_rolls": n_roll, "n_hedges": n_hedge,
        "max_margin": round(max_margin) if max_margin else None,
        "worst_mtm": round(min((d["pnl"] for d in daily), default=0.0)),
        "events": events,
        "legs": [_leg_row(lg, ev_of_open[lg["ref"]], ev_of_close[lg["ref"]]) for lg in legs],
        "mtm_series": [{"date": d["date"], "value": d["pnl"]} for d in daily],
        "spot_path": _spot_path(spot_fn, entry_ts.date(), expiry, entry_spot, exit_spot,
                                exit_ts.date() if exit_ts else None),
    }


def _classify(is_first, is_last, opened, closed, tags) -> str:
    if is_first or (tags & _ENTRY_TAGS and opened and not closed):
        return "entry"
    if tags & _HEDGE_TAGS or any(lg["dir"] > 0 for lg in opened):
        return "hedge"          # adds a long (breakeven/iron-fly hedge)
    if opened and closed:
        return "roll"           # closed one leg, opened another at the same instant
    if is_last or (closed and not opened):
        return "exit"
    return "roll"


def _reason(kind: str, tags: set, cycle: dict) -> str:
    if kind == "entry":
        return "Opened the initial position."
    if kind == "roll":
        return ("Premium imbalance passed the threshold — rolled the cheap side to the "
                "strike whose premium matches the rich side.")
    if kind == "hedge":
        return ("Rolled onto the opposite strike — capped at a straddle and hedged at "
                "breakeven (iron fly).")
    er = cycle.get("exit_reason") or ""
    return {"target": "MTM crossed the profit target — all legs booked.",
            "stop": "MTM hit the stop — all legs booked.",
            "time": "Held to the max-holding / time exit — all legs booked.",
            "expiry": "Held to expiry — settled to intrinsic.",
            }.get(er, "Position closed.")


def _net_delta(open_legs, spot, expiry, when, r) -> float | None:
    if not spot:
        return None
    total = 0.0
    for lg in open_legs:
        iv = lg.get("iv")
        if iv is None:
            continue
        d = bs.delta(spot, lg["strike"], _years_to(expiry, when), r, iv, lg["right"])
        total += lg["dir"] * lg["units"] * d
    return total


def _event_leg(lg, which: str) -> dict:
    price = lg["open_price"] if which == "open" else lg["close_price"]
    cash = (lg["units"] * lg["open_price"]) if which == "open" else None
    return {"ref": lg["ref"], "symbol": lg["symbol"], "side": lg["side"],
            "right": lg["right"], "strike": lg["strike"], "units": lg["units"],
            "price": price, "cashflow": round(cash) if cash else None,
            "realized": round(lg["pnl"]) if which == "close" else None}


def _leg_row(lg, open_ev, close_ev) -> dict:
    return {"ref": lg["ref"], "symbol": lg["symbol"], "side": lg["side"], "right": lg["right"],
            "strike": lg["strike"], "units": lg["units"],
            "open_event": open_ev, "close_event": close_ev,
            "open_ts": lg["open_ts"].strftime("%Y-%m-%d %H:%M"),
            "close_ts": lg["close_ts"].strftime("%Y-%m-%d %H:%M") if lg["close_ts"] else None,
            "open_price": lg["open_price"], "close_price": lg["close_price"],
            "open_delta": round(lg["open_delta"], 3) if lg.get("open_delta") is not None else None,
            "days": lg["days"], "pnl": round(lg["pnl"])}


def _event_id_at(events, ts):
    if ts is None:
        return None
    key = ts.strftime("%Y-%m-%d %H:%M")
    return next((e["id"] for e in events if e["at"] == key), None)


def _max_margin(margin_series, d1: date, d2: date) -> float | None:
    vals = []
    for m in margin_series or []:
        try:
            md = _parse_ts(m.get("date")).date()
        except (ValueError, TypeError):
            continue
        if d1 <= md <= d2:
            vals.append(float(m.get("margin", m.get("value", 0)) or 0))
    return max(vals) if vals else None


def _spot_path(spot_fn, d1: date, expiry, entry_spot, exit_spot, exit_date):
    """Daily underlying closes across the cycle window (entry → expiry) for the ladder's spot
    line. Endpoints pinned to the cycle's minute-accurate entry/exit spot."""
    exp = expiry if isinstance(expiry, date) else _parse_ts(expiry).date()
    pts, cur = [], d1
    from datetime import timedelta as _td
    while cur <= exp:
        s = entry_spot if cur == d1 else (exit_spot if exit_date and cur == exit_date
                                          else spot_fn(cur))
        if s:
            pts.append({"date": cur.isoformat(), "spot": round(float(s))})
        cur += _td(days=1)
    return pts
