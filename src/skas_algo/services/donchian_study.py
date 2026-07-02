"""Donchian breakout study — how the strangle's underlying channel actually behaves.

Pure-price research layer for the Donchian strangle: for every expiry-anchored monthly cycle
(range = the PREVIOUS full expiry→expiry window, trade window = entry after the last monthly
expiry → the next one), walk each name's daily bars and record what the channel did — stayed
inside, broke out (which side / when / how far), re-entered, whipsawed — plus a simulation of
the LIVE flip rules (breach must clear the edge by ``buffer_pct``; post-flip level = that
day's close on the opposite side, the ATM proxy; one flip per day by daily-bar construction;
``max_flips`` breaches close the name for the cycle — mirrors ``DonchianStrangleMonthlyStrategy``).

Two trackers run in parallel per name-cycle and answer different questions:
  * the CHANNEL tracker uses the ORIGINAL range edges throughout — breakout / re-entry /
    whipsaw / both-edges-breached, i.e. "how trustworthy is the channel itself";
  * the FLIP simulator moves its levels the way the live strategy rolls (opposite-side ATM
    after each breach) — "what would the deployed rules have done".

Pure functions over prebuilt DataFrames — no I/O, no broker. The route
(api/routes/research.py) fetches cached bars and wires these together.

Known approximations (returned in ``caveats`` and rendered by the UI):
  * survivorship bias — the CURRENT Nifty-50 membership is applied to all of history;
  * daily bars — intraday ordering is invisible: a bar that clears BOTH edges is resolved
    to the side with the larger excursion, and "touch" uses the day's high/low;
  * calendar expiries — ``expected_monthly_expiry`` snapped back to actual trading days
    (holiday-correct for the index calendar, but not circular-verified per month).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median

import numpy as np
import pandas as pd

from skas_algo.engine.options.contract_specs import expected_monthly_expiry
from skas_algo.services.donchian_strangle import _next_trading_day, _snap_back

INDEX_NAME = "NIFTY 50"  # the index row rides along but is EXCLUDED from stock aggregates


@dataclass
class StudyParams:
    buffer_pct: float = 0.5   # breach = clear the edge by this % (live breach_buffer_pct)
    basis: str = "touch"      # "touch" (day high/low) | "close" (day close) — live breach_basis
    max_flips: int = 3        # live deploy default: two rolls, then close the name
    include_index: bool = True


# ------------------------------------------------------------------ cycle anchors

def monthly_cycles(trading_days: list[date], start: date, end: date,
                   underlying: str = "NIFTY",
                   real_expiries: list[date] | None = None) -> list[dict]:
    """Expiry-anchored cycles in [start, end]: each trades entry(=day after expiry i-1) →
    expiry i, with the Donchian range window = the PREVIOUS full cycle (day after expiry
    i-2 → expiry i-1). Anchors are ``expected_monthly_expiry`` snapped back to actual
    trading days, so the first tradeable cycle needs two prior anchors inside the window.

    ``real_expiries`` (optional; the cached chain's actual listed expiries) overrides the
    calendar anchors where it has coverage: the month's LAST listed expiry IS the monthly
    (weeklies fall earlier) — exact, circular/holiday-proof dates for the option backtest."""
    tds = sorted(trading_days)
    if not tds:
        return []
    tset = set(tds)
    real_by_month: dict[tuple[int, int], date] = {}
    for e in real_expiries or []:
        key = (e.year, e.month)
        if key not in real_by_month or e > real_by_month[key]:
            real_by_month[key] = e
    anchors: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        a = real_by_month.get((y, m)) or expected_monthly_expiry(underlying, y, m)
        if a is not None:
            snapped = _snap_back(a, tds)
            # Only anchors that land on a real trading day inside the data window count —
            # a future-month anchor would "snap back" to the last cached day and fabricate
            # a bogus short cycle at the end of the series.
            if snapped in tset and start <= snapped <= end:
                anchors.append(snapped)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    anchors = sorted(set(anchors))

    cycles: list[dict] = []
    for i in range(2, len(anchors)):
        prev2, prev1, expiry = anchors[i - 2], anchors[i - 1], anchors[i]
        range_start = _next_trading_day(prev2, tds)
        entry = _next_trading_day(prev1, tds)
        if range_start is None or entry is None or entry > expiry:
            continue
        cycles.append({
            "cycle_id": expiry.strftime("%Y-%m"),  # labelled by the expiry it settles at
            "range_start": range_start, "range_end": prev1,
            "entry_date": entry, "expiry": expiry,
        })
    return cycles


# ------------------------------------------------------------------ one name-cycle

def analyze_name_cycle(bars: pd.DataFrame, range_high: float, range_low: float,
                       p: StudyParams) -> dict:
    """Walk one name's trade-window daily bars (pre-sliced, sorted, columns
    date/high/low/close). Returns the channel-tracker + flip-simulator record."""
    buf = p.buffer_pct / 100.0
    touch = p.basis == "touch"

    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    dates = [d.date() if hasattr(d, "date") else d for d in pd.to_datetime(bars["date"])]
    n = len(closes)

    # float() everywhere below: numpy scalars (np.bool_/np.float64) must not leak into the
    # returned dicts — np.bool_ is not JSON-serializable and 500s the route.
    def up_ref(i: int) -> float:   # the price that tests an upper level this bar
        return float(highs[i] if touch else closes[i])

    def dn_ref(i: int) -> float:   # the price that tests a lower level this bar
        return float(lows[i] if touch else closes[i])

    # Entry-day gap: live's breakout_atm — spot already beyond an edge at entry (no buffer)
    # means the CE (or PE) was never sold; the flip simulator starts single-sided at the
    # entry close (the ATM proxy), exactly like the screener sells the ATM opposite leg.
    breakout_at_entry: str | None = None
    levels: dict[str, float] = {"CE": range_high, "PE": range_low}
    if closes[0] >= range_high:
        breakout_at_entry, levels = "up", {"PE": float(closes[0])}
    elif closes[0] <= range_low:
        breakout_at_entry, levels = "down", {"CE": float(closes[0])}

    # Channel tracker state (original edges throughout).
    first_side: str | None = None
    first_day: int | None = None
    re_entered = False
    re_entry_day: int | None = None
    whipsaw = False
    whipsaw_side: str | None = None
    hit_up = hit_dn = False
    max_up = max_dn = 0.0

    # Flip simulator state (levels roll like the live strategy).
    flips: list[dict] = []
    flip_count = 0
    closed = False
    closed_day: int | None = None

    for i in range(n):
        # ---- channel tracker (raw edges, buffered breach) ----
        up_breach = up_ref(i) >= range_high * (1 + buf)
        dn_breach = dn_ref(i) <= range_low * (1 - buf)
        hit_up, hit_dn = hit_up or up_breach, hit_dn or dn_breach
        max_up = max(max_up, (up_ref(i) - range_high) / range_high * 100.0)
        max_dn = max(max_dn, (range_low - dn_ref(i)) / range_low * 100.0)
        if first_side is None:
            if up_breach and dn_breach:  # daily-bar ambiguity: resolve to the larger excursion
                first_side = "up" if (up_ref(i) - range_high) / range_high >= \
                    (range_low - dn_ref(i)) / range_low else "down"
                first_day = i + 1
            elif up_breach or dn_breach:
                first_side, first_day = ("up" if up_breach else "down"), i + 1
        elif not re_entered:
            if range_low < closes[i] < range_high:  # close strictly back inside the channel
                re_entered, re_entry_day = True, i + 1
        elif not whipsaw and (up_breach or dn_breach):
            whipsaw, whipsaw_side = True, ("up" if up_breach else "down")

        # ---- flip simulator (live rules; levels move) ----
        if not closed:
            side = None
            ce, pe = levels.get("CE"), levels.get("PE")
            ce_hit = ce is not None and up_ref(i) >= ce * (1 + buf)
            pe_hit = pe is not None and dn_ref(i) <= pe * (1 - buf)
            if ce_hit and pe_hit and ce is not None and pe is not None:
                # same daily-bar ambiguity as above — the larger excursion wins
                side = "CE" if (up_ref(i) - ce) / ce >= (pe - dn_ref(i)) / pe else "PE"
            elif ce_hit or pe_hit:
                side = "CE" if ce_hit else "PE"
            if side is not None:
                flip_count += 1
                will_close = flip_count >= p.max_flips  # live: Nth breach closes the name
                flips.append({"day": i + 1, "date": dates[i].isoformat(), "side": side,
                              "action": "close" if will_close else "roll"})
                if will_close:
                    closed, closed_day = True, i + 1
                else:
                    # Roll: the fresh short goes ATM on the OPPOSITE side; with daily bars
                    # the day's close is the ATM proxy. One flip/bar = one flip/day (live cap).
                    levels = ({"PE": float(closes[i])} if side == "CE"
                              else {"CE": float(closes[i])})

    status = ("inside" if first_side is None else
              "whipsaw" if whipsaw else
              "re-entered" if re_entered else "breakout")
    return {
        "status": status, "days": n,
        "breakout_at_entry": breakout_at_entry,
        "first_breach_side": first_side, "first_breach_day": first_day,
        "re_entered": re_entered, "re_entry_day": re_entry_day,
        "whipsaw": whipsaw, "whipsaw_side": whipsaw_side,
        "both_sides_breached": hit_up and hit_dn,
        "max_excursion_up_pct": round(max_up, 3), "max_excursion_down_pct": round(max_dn, 3),
        "flips": flips, "flip_count": flip_count,
        "closed_by_flips": closed, "closed_day": closed_day,
    }


# ------------------------------------------------------------------ full study

def _prep(df: pd.DataFrame) -> tuple[np.ndarray, pd.DataFrame]:
    """(sorted datetime64 array, sorted frame) for fast per-cycle window slicing."""
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    return d["date"].to_numpy(), d


def _slice(dates: np.ndarray, frame: pd.DataFrame, lo: date, hi: date) -> pd.DataFrame:
    a = int(np.searchsorted(dates, np.datetime64(lo)))
    b = int(np.searchsorted(dates, np.datetime64(hi), side="right"))
    return frame.iloc[a:b]


def run_study(price_frames: dict[str, pd.DataFrame], cycles: list[dict],
              vix_lookup, p: StudyParams) -> dict:
    """The whole study: every name × every cycle. ``price_frames`` maps symbol → full-history
    OHLC frame (must include ``INDEX_NAME`` when ``p.include_index``); ``vix_lookup(date)``
    returns the India VIX close on/before a date (None outside coverage)."""
    prepped = {sym: _prep(df) for sym, df in price_frames.items()
               if df is not None and len(df) > 0}
    stock_names = sorted(s for s in prepped if s != INDEX_NAME)
    names = stock_names + ([INDEX_NAME] if (p.include_index and INDEX_NAME in prepped) else [])

    detail: list[dict] = []
    cycle_rows: list[dict] = []
    league_acc: dict[str, list[dict]] = {s: [] for s in names}
    breach_days: list[int] = []       # stocks only — days to first breach
    excursions: list[float] = []      # stocks only — max excursion of breached name-cycles

    for cyc in cycles:
        counts = {"inside": 0, "breakout": 0, "re-entered": 0, "whipsaw": 0}
        up = dn = both = closed = gap = 0
        n_names = 0
        index_status: str | None = None
        for sym in names:
            dates, frame = prepped[sym]
            rng_bars = _slice(dates, frame, cyc["range_start"], cyc["range_end"])
            trade_bars = _slice(dates, frame, cyc["entry_date"], cyc["expiry"])
            # A name needs a full prior-cycle range AND bars to trade — a late listing or
            # data gap is "no data", excluded from the stats rather than counted as inside.
            if len(rng_bars) < 5 or len(trade_bars) < 2:
                continue
            rec = analyze_name_cycle(
                trade_bars, float(rng_bars["high"].max()), float(rng_bars["low"].min()), p)
            rec.update({"cycle_id": cyc["cycle_id"], "symbol": sym,
                        "range_high": round(float(rng_bars["high"].max()), 2),
                        "range_low": round(float(rng_bars["low"].min()), 2)})
            detail.append(rec)
            league_acc[sym].append(rec)
            if sym == INDEX_NAME:
                index_status = rec["status"]
                continue  # the index rides along per-name but stays out of stock aggregates
            n_names += 1
            counts[rec["status"]] += 1
            up += rec["first_breach_side"] == "up"
            dn += rec["first_breach_side"] == "down"
            both += rec["both_sides_breached"]
            closed += rec["closed_by_flips"]
            gap += rec["breakout_at_entry"] is not None
            if rec["first_breach_day"] is not None:
                breach_days.append(rec["first_breach_day"])
                excursions.append(max(rec["max_excursion_up_pct"],
                                      rec["max_excursion_down_pct"]))
        cycle_rows.append({
            "cycle_id": cyc["cycle_id"],
            "range_start": cyc["range_start"].isoformat(),
            "range_end": cyc["range_end"].isoformat(),
            "entry_date": cyc["entry_date"].isoformat(), "expiry": cyc["expiry"].isoformat(),
            "n_names": n_names, **counts, "breakout_up": up, "breakout_down": dn,
            "both_sides": both, "closed_by_flips": closed, "gap_entries": gap,
            "vix_entry": vix_lookup(cyc["entry_date"]) if vix_lookup else None,
            "index_status": index_status,
        })

    league = [_league_row(sym, recs) for sym, recs in league_acc.items() if recs]
    league.sort(key=lambda r: r["breach_rate"], reverse=True)

    stock_recs = [r for r in detail if r["symbol"] != INDEX_NAME]
    total = len(stock_recs)
    breached = [r for r in stock_recs if r["first_breach_side"] is not None]
    re_entered = [r for r in breached if r["re_entered"]]

    def pct(x: int, base: int) -> float | None:
        return round(x / base * 100.0, 1) if base else None

    aggregates = {
        "cycles": len(cycle_rows), "names": len(stock_names), "name_cycles": total,
        "inside_pct": pct(sum(r["status"] == "inside" for r in stock_recs), total),
        "breach_pct": pct(len(breached), total),
        "breakout_up_pct": pct(sum(r["first_breach_side"] == "up" for r in breached),
                               len(breached)),
        "re_entry_pct": pct(len(re_entered), len(breached)),          # of breakouts
        "whipsaw_pct": pct(sum(r["whipsaw"] for r in re_entered), len(re_entered)),  # of re-entries
        "both_sides_pct": pct(sum(r["both_sides_breached"] for r in stock_recs), total),
        "closed_by_flips_pct": pct(sum(r["closed_by_flips"] for r in stock_recs), total),
        "gap_entry_pct": pct(sum(r["breakout_at_entry"] is not None for r in stock_recs), total),
        "median_days_to_first_breach": (median(breach_days) if breach_days else None),
        "avg_flips_per_name_cycle": (round(sum(r["flip_count"] for r in stock_recs) / total, 2)
                                     if total else None),
    }

    return {
        "params": {"buffer_pct": p.buffer_pct, "basis": p.basis, "max_flips": p.max_flips},
        "cycles": cycle_rows,
        "league": league,
        "histograms": {"days_to_first_breach": breach_days, "excursion_pct": excursions},
        "aggregates": aggregates,
        "vix_split": _vix_split(cycle_rows),
        "detail": detail,
        "caveats": [
            "Survivorship bias: the CURRENT Nifty-50 membership is applied to all of history.",
            "Daily bars: intraday ordering is invisible — 'touch' uses the day's high/low, a bar "
            "clearing both edges resolves to the larger excursion, and flip levels use that day's "
            "close as the ATM proxy.",
            "Expiries are calendar-expected (last expiry weekday) snapped back to trading days.",
        ],
    }


def _league_row(sym: str, recs: list[dict]) -> dict:
    n = len(recs)
    breached = [r for r in recs if r["first_breach_side"] is not None]
    re_entered = [r for r in breached if r["re_entered"]]
    exc = [max(r["max_excursion_up_pct"], r["max_excursion_down_pct"]) for r in breached]
    days = [r["first_breach_day"] for r in breached]
    return {
        "symbol": sym, "is_index": sym == INDEX_NAME, "cycles": n,
        "inside": sum(r["status"] == "inside" for r in recs),
        "breach_rate": round(len(breached) / n * 100.0, 1),
        "up": sum(r["first_breach_side"] == "up" for r in breached),
        "down": sum(r["first_breach_side"] == "down" for r in breached),
        "re_entries": len(re_entered),
        "whipsaws": sum(r["whipsaw"] for r in recs),
        "both_sides": sum(r["both_sides_breached"] for r in recs),
        "closed_by_flips": sum(r["closed_by_flips"] for r in recs),
        "avg_flips": round(sum(r["flip_count"] for r in recs) / n, 2),
        "median_breach_day": (median(days) if days else None),
        "avg_excursion_pct": (round(sum(exc) / len(exc), 2) if exc else None),
    }


def _vix_split(cycle_rows: list[dict]) -> list[dict]:
    """Cycle outcomes bucketed by the India VIX at entry (2020+ — VIX coverage)."""
    buckets = [("<15", lambda v: v < 15), ("15-20", lambda v: 15 <= v <= 20),
               (">20", lambda v: v > 20)]
    out = []
    for label, test in buckets:
        rows = [c for c in cycle_rows if c["vix_entry"] is not None and test(c["vix_entry"])]
        n = sum(c["n_names"] for c in rows)
        if not rows or n == 0:
            out.append({"bucket": label, "cycles": len(rows), "name_cycles": 0})
            continue
        out.append({
            "bucket": label, "cycles": len(rows), "name_cycles": n,
            "inside_pct": round(sum(c["inside"] for c in rows) / n * 100.0, 1),
            "whipsaw_pct": round(sum(c["whipsaw"] for c in rows) / n * 100.0, 1),
            "both_sides_pct": round(sum(c["both_sides"] for c in rows) / n * 100.0, 1),
            "closed_pct": round(sum(c["closed_by_flips"] for c in rows) / n * 100.0, 1),
        })
    return out
