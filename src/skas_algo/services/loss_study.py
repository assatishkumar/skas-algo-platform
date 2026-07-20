"""Loss-reduction study for the ratio family (batman_ratio_monthly on the Research page).

Motivation (run #224): batman is net-profitable but a few cycles lose big on FAST
directional moves — two that peaked deep in profit then round-tripped, one VIX-spike crash
the EOD-only stop let bleed, two slow grinds that rode the time exit. The owner asked which
signal (trailing stop / VIX / trend / entry filter) would have cut those losses.

METHOD — "capture once, evaluate offline". Every candidate here is an EXIT-timing or
ENTRY-SKIP decision, so it is computable post-hoc over each cycle's recorded MTM path — no
re-replaying per rule:

  1. Two replays (done by the route, memoised): a BASELINE run with the real params, and a
     FULL run with profit/stop OFF so every cycle rides to its time exit — the fullest path.
     Both share the same entries (entry logic ignores profit/stop), aligned by entry minute.
  2. Reconstruct each cycle's per-minute cum-MTM from the 1-min store (leg symbol + entry
     premium + units + side) and attach the daily NIFTY/VIX/indicator series.
  3. Each rule is an EARLY-EXIT OVERLAY on the baseline: if it fires before the baseline's
     own exit, the cycle banks the marked MTM there; otherwise the baseline stands. Entry
     filters simply drop a skipped cycle's P&L.
  4. Aggregate over the FULL store history with an in-sample / out-of-sample split, and a
     robustness curve per threshold — because a single window overfits wildly (verified: a
     directional exit at 2.5% loses -87k but 3.5% gains +13k on 19 cycles).

FAITHFUL because exit-only rules flatten at the marked MTM at the signal minute (exact) and
entry filters remove an independent cycle (exact under fixed sizing). OUT OF SCOPE (caveat):
mid-cycle rolling/adjustment rules change the book and can't be modelled post-hoc.

Pure functions over prebuilt inputs — the route (api/routes/research.py) runs the replays,
loads bars, and wires these together, mirroring services/donchian_study.py.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

# A per-cycle charge estimate is derived from the baseline (see _cycle_charge); the study
# compares gross-MTM exits net of that same charge, so no separate charge model is needed.


@dataclass
class LossStudyParams:
    strategy_id: str = "batman_ratio_monthly"
    underlying: str = "NIFTY"
    start: date = date(2021, 1, 1)
    end: date = date(2026, 12, 31)
    capital: float = 1_000_000.0
    margin_per_lot: float = 200_000.0
    lots: int = 3
    # In-sample / out-of-sample split: cycles ENTERED before oos_start fit the thresholds,
    # cycles on/after validate. Default ~last third of a 5-year window.
    oos_start: date = date(2025, 1, 1)
    # Trailing is SELF-NORMALIZING per cycle: arm once peak ≥ ``trail_activate`` per body-unit
    # (a small noise floor), then exit if MTM gives back to ``trail_keep`` × peak. Giving back
    # a FRACTION of peak works across lot-size eras where a fixed rupee giveback would not.
    trail_activate: list[float] = field(default_factory=lambda: [5, 10, 15])
    trail_keep: list[float] = field(default_factory=lambda: [0.3, 0.5, 0.7])
    # Intraday stop stays a per-body-unit rupee loss (comparable across eras).
    stop_levels: list[float] = field(default_factory=lambda: [20, 30, 40, 55])
    vix_jumps: list[float] = field(default_factory=lambda: [2.0, 3.0, 4.0])
    vix_levels: list[float] = field(default_factory=lambda: [16, 18, 20, 22])
    spot_moves: list[float] = field(default_factory=lambda: [2.0, 2.5, 3.0, 3.5, 4.0])


# ----------------------------------------------------------------- path reconstruction
def reconstruct_path(cycle_full: dict, bars_loader) -> dict | None:
    """One cycle's per-minute cum-MTM from the store, over the FULL (stop/target-off) ride.

    ``bars_loader(underlying, expiry_iso, strike, right, d1, d2) -> DataFrame[start, close]``
    (the store's load_contract_bars). Returns ``None`` if no leg printed. ``mtm`` is GROSS
    (Σ dir·units·(close−entry)); charges are applied later, per cycle, from the baseline.
    """
    legs = cycle_full.get("legs_detail") or []
    if not legs:
        return None
    u = cycle_full.get("underlying") or "NIFTY"
    entry = _parse_ts(cycle_full["entry_date"])
    exit_ = _parse_ts(cycle_full["exit_date"])
    d1, d2 = entry.date(), exit_.date()
    body_units = sum(int(leg["units"]) for leg in legs if leg.get("side") == "short") or 1

    # symbol -> forward-filled close Series (minute index), + its (dir, units, entry).
    per_leg: list[tuple[pd.Series, int, int, float]] = []
    all_minutes: set[pd.Timestamp] = set()
    for leg in legs:
        df = bars_loader(u, str(leg["expiry"])[:10], float(leg["strike"]), leg["right"], d1, d2)
        if df is None or len(df) == 0:
            continue
        s = df.set_index(pd.to_datetime(df["start"]))["close"].sort_index()
        per_leg.append((s, 1 if leg.get("side") == "long" else -1, int(leg["units"]),
                        float(leg["entry_premium"])))
        all_minutes.update(s.index)
    if not per_leg:
        return None
    minutes = sorted(m for m in all_minutes if entry <= m <= exit_)
    if not minutes:
        return None
    idx = pd.DatetimeIndex(minutes)
    mtm = np.zeros(len(idx))
    for s, sgn, units, ent in per_leg:
        filled = s.reindex(idx, method="ffill").to_numpy(dtype=float)
        # A leg that hasn't printed yet at the front carries its entry premium (flat P&L).
        filled = np.where(np.isnan(filled), ent, filled)
        mtm += sgn * units * (filled - ent)
    day_key = np.array([m.date() for m in minutes])
    return {
        "entry": entry, "exit": exit_, "body_units": body_units,
        "minutes": idx, "ts": np.array([m.value for m in minutes]), "mtm": mtm,
        "day": day_key, "entry_spot": cycle_full.get("underlying_entry"),
    }


def _parse_ts(v) -> pd.Timestamp:
    return pd.Timestamp(str(v).replace("T", " "))


def _mtm_at(path: dict, ts: pd.Timestamp) -> float:
    """Marked cum-MTM at the last minute <= ts (0 before the first print)."""
    i = bisect_right(path["ts"], ts.value)
    return float(path["mtm"][i - 1]) if i > 0 else 0.0


def _eod_ts(path: dict, d: date) -> pd.Timestamp | None:
    """The last printed minute on day ``d`` within the hold (None if the day is absent)."""
    on = path["minutes"][path["day"] == d]
    return on[-1] if len(on) else None


# ----------------------------------------------------------------- daily signal frame
def build_daily_signals(underlying: str, start: date, end: date, price_getter,
                        vix_lookup, ema_period: int = 21, st_period: int = 10,
                        st_mult: float = 3.0, hv_window: int = 20) -> pd.DataFrame:
    """Daily NIFTY OHLC + VIX + indicators, indexed by date. ``price_getter(symbol,start,
    end)`` returns the cached index OHLC; ``vix_lookup(d)`` the India-VIX close. Indicators
    reuse the platform's own implementations (SuperTrend, EMA-channel, realized-vol HV)."""
    from skas_algo.data.options_provider import INDEX_SYMBOL
    from skas_algo.engine.indicators.supertrend import supertrend_direction
    from skas_algo.engine.options.realized_vol import realized_vol_series

    sym = INDEX_SYMBOL.get(underlying.upper(), underlying.upper())
    df = price_getter(sym, start, end)
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.rename(columns=str.lower).copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"]
    df["ema_up"] = df["high"].ewm(span=ema_period, adjust=False).mean()
    df["ema_lo"] = df["low"].ewm(span=ema_period, adjust=False).mean()
    df["hv20"] = pd.Series(realized_vol_series(close.tolist(), window=hv_window)).to_numpy() * 100.0
    st = supertrend_direction(df.rename(columns={"date": "date"}), period=st_period,
                              multiplier=st_mult)
    df["st_dir"] = st.to_numpy()
    df["vix"] = [vix_lookup(d) for d in df["date"]]
    df["vix_prev"] = df["vix"].shift(1)
    return df.set_index("date")


# ----------------------------------------------------------------- rule evaluators
# Each returns the exit Timestamp a rule would trigger within the cycle, or None.
def _rule_trailing(path, activate: float, keep: float) -> pd.Timestamp | None:
    """Arm once peak ≥ ``activate`` per body-unit, then exit if MTM gives back to
    ``keep`` × peak (self-normalizing — a fraction of the cycle's own peak)."""
    bu = path["body_units"]
    peak = 0.0
    for ts, m in zip(path["minutes"], path["mtm"], strict=True):
        peak = max(peak, m)
        if peak / bu >= activate and peak > 0 and m <= keep * peak:
            return ts
    return None


def _rule_intraday_stop(path, level: float) -> pd.Timestamp | None:
    bu = path["body_units"]
    for ts, m in zip(path["minutes"], path["mtm"], strict=True):
        if m / bu <= -level:
            return ts
    return None


def _rule_vix(path, daily: pd.DataFrame, jump: float, level: float) -> pd.Timestamp | None:
    for d in sorted(set(path["day"])):
        row = daily.loc[d] if d in daily.index else None
        if row is None:
            continue
        v, vp = row["vix"], row["vix_prev"]
        if v is None or (np.isnan(v) if isinstance(v, float) else False):
            continue
        spike = (jump > 0 and vp is not None and not np.isnan(vp) and (v - vp) >= jump)
        high = (level > 0 and v >= level)
        if spike or high:
            ts = _eod_ts(path, d)
            if ts is not None:
                return ts
    return None


def _rule_spot_move(path, daily: pd.DataFrame, band_pct: float) -> pd.Timestamp | None:
    es = path.get("entry_spot")
    if not es:
        return None
    for d in sorted(set(path["day"])):
        if d not in daily.index:
            continue
        sp = daily.loc[d, "close"]
        if abs((sp - es) / es * 100.0) >= band_pct:
            ts = _eod_ts(path, d)
            if ts is not None:
                return ts
    return None


def _rule_supertrend(path, daily: pd.DataFrame) -> pd.Timestamp | None:
    days = sorted(set(path["day"]))
    if not days or days[0] not in daily.index:
        return None
    entry_dir = daily.loc[days[0], "st_dir"]
    for d in days[1:]:
        if d in daily.index and daily.loc[d, "st_dir"] != entry_dir:
            ts = _eod_ts(path, d)
            if ts is not None:
                return ts
    return None


def _rule_ema_break(path, daily: pd.DataFrame) -> pd.Timestamp | None:
    for d in sorted(set(path["day"])):
        if d not in daily.index:
            continue
        row = daily.loc[d]
        if row["close"] > row["ema_up"] or row["close"] < row["ema_lo"]:
            ts = _eod_ts(path, d)
            if ts is not None:
                return ts
    return None


# ----------------------------------------------------------------- overlay + aggregation
def _cycle_charge(path: dict, baseline_net: float, baseline_exit: pd.Timestamp) -> float:
    """Per-cycle round-trip charge, backed out of the baseline: the gross marked MTM at the
    baseline's own exit minus the baseline net. Assumed ~constant across exit prices."""
    return _mtm_at(path, baseline_exit) - baseline_net


def _apply(path: dict, baseline_net: float, baseline_exit: pd.Timestamp,
           rule_exit: pd.Timestamp | None, charge: float) -> float:
    """A rule is an early-exit OVERLAY: only bites if it fires strictly before the baseline
    exit; otherwise the baseline outcome stands."""
    if rule_exit is None or rule_exit >= baseline_exit:
        return baseline_net
    return _mtm_at(path, rule_exit) - charge


def _score(cycles: list[dict], nets: list[float]) -> dict:
    base = np.array([c["baseline_net"] for c in cycles])
    new = np.array(nets)
    losers = base < 0
    winners = base > 0
    return {
        "net": float(new.sum()),
        "delta": float(new.sum() - base.sum()),
        "loss_cut": float(new[losers].sum() - base[losers].sum()),   # + = losses reduced
        "winners_hurt": float(base[winners].sum() - new[winners].sum()),  # + = wins given up
        "worst_cycle": float(new.min()) if len(new) else 0.0,
        "cycles_changed": int((np.abs(new - base) > 1.0).sum()),
        "num_cycles": len(cycles),
    }


def _split(cycles: list[dict], oos_start: date):
    is_i = [i for i, c in enumerate(cycles) if c["entry"].date() < oos_start]
    oos_i = [i for i, c in enumerate(cycles) if c["entry"].date() >= oos_start]
    return is_i, oos_i


def build_cycles(cycles_base: list[dict], bars_loader,
                 params: LossStudyParams) -> list[dict]:
    """Reconstruct each baseline cycle's per-minute path over [entry, baseline exit]. Since
    every candidate rule only exits EARLIER than the baseline, the baseline path is all we
    need — no second full-ride replay (whose exit timing would desync the monthly entries).
    Cycles whose path can't be rebuilt (store gap) are dropped with no effect on the rest."""
    out = []
    for cb in cycles_base:
        path = reconstruct_path(cb, bars_loader)
        if path is None:
            continue
        baseline_net = float(cb.get("net_pnl") or 0.0)
        baseline_exit = _parse_ts(cb["exit_date"])
        out.append({"entry": _parse_ts(cb["entry_date"]), "baseline_net": baseline_net,
                    "baseline_exit": baseline_exit, "path": path,
                    "charge": _cycle_charge(path, baseline_net, baseline_exit),
                    "_oos_start": params.oos_start})
    return out


def run_study(cycles: list[dict], daily: pd.DataFrame, params: LossStudyParams) -> dict:
    """cycles: [{entry, baseline_net, baseline_exit, path, charge}] already reconstructed.
    Evaluates every rule/threshold, picks each family's best by IN-SAMPLE net, reports its
    OUT-OF-SAMPLE result, plus a robustness curve. All rupee thresholds are per body-unit."""
    is_i, oos_i = _split(cycles, params.oos_start)

    def eval_rule(exit_fn) -> list[float]:
        return [_apply(c["path"], c["baseline_net"], c["baseline_exit"], exit_fn(c["path"]),
                       c["charge"]) for c in cycles]

    baseline_nets = [c["baseline_net"] for c in cycles]
    baseline = _score(cycles, baseline_nets)

    families: dict[str, list[dict]] = {}

    def _sub(nets, sel):
        return _score([cycles[i] for i in sel], [nets[i] for i in sel]) if sel else {}

    def add(family: str, label: str, exit_fn):
        nets = eval_rule(exit_fn)
        entry = {"label": label, "_nets": nets, "full": _score(cycles, nets),
                 "in_sample": _sub(nets, is_i), "oos": _sub(nets, oos_i)}
        families.setdefault(family, []).append(entry)

    # A — trailing / profit-lock (give back a fraction of peak)
    for a in params.trail_activate:
        for k in params.trail_keep:
            add("trailing", f"arm@{a:.0f}/u keep {k:.0%} of peak",
                lambda p, a=a, k=k: _rule_trailing(p, a, k))
    # B — VIX
    for j in params.vix_jumps:
        add("vix_jump", f"VIX +{j:.0f}/day", lambda p, j=j: _rule_vix(p, daily, j, 0))
    for lv in params.vix_levels:
        add("vix_level", f"VIX >= {lv:.0f}", lambda p, lv=lv: _rule_vix(p, daily, 0, lv))
    # C — trend / directional
    for k in params.spot_moves:
        add("spot_move", f"|move| >= {k:.1f}%", lambda p, k=k: _rule_spot_move(p, daily, k))
    add("supertrend", "SuperTrend flip", lambda p: _rule_supertrend(p, daily))
    add("ema_break", "EMA(21) channel break", lambda p: _rule_ema_break(p, daily))
    # D — intraday stop
    for lv in params.stop_levels:
        add("intraday_stop", f"stop @ -{lv:.0f}/u", lambda p, lv=lv: _rule_intraday_stop(p, lv))

    # E — ENTRY filters: skip a cycle entirely (removes its P&L, wins and losses). The VIX
    # finding above suggests entry-timing matters more than any exit; test it directly.
    def entry_feat(c):
        d = c["entry"].date()
        if d not in daily.index:
            return None
        r = daily.loc[d]
        v, hv = r["vix"], r["hv20"]
        return (v, hv, (v - hv) if (v is not None and hv is not None) else None)

    def add_filter(family: str, label: str, skip_fn):
        nets = [0.0 if skip_fn(c) else c["baseline_net"] for c in cycles]
        entry = {"label": label, "_nets": nets, "full": _score(cycles, nets),
                 "in_sample": _sub(nets, is_i), "oos": _sub(nets, oos_i)}
        families.setdefault(family, []).append(entry)

    def skip_vix_ceiling(c, L):
        f = entry_feat(c)
        return f is not None and f[0] is not None and f[0] >= L

    def skip_low_premium(c, floor):
        f = entry_feat(c)
        return f is not None and f[2] is not None and f[2] < floor

    for L in params.vix_levels:
        add_filter("entry_vix_ceiling", f"skip entry if VIX >= {L:.0f}",
                   lambda c, L=L: skip_vix_ceiling(c, L))
    for fl in (-2.0, 0.0, 2.0):
        add_filter("entry_vol_premium", f"skip entry if VIX-HV20 < {fl:.0f}",
                   lambda c, fl=fl: skip_low_premium(c, fl))

    # Best per family by IN-SAMPLE net (fall back to full when no split), reported OOS.
    ranked, best_by_family = [], {}
    for fam, entries in families.items():
        key = "in_sample" if is_i else "full"
        best = max(entries, key=lambda e: (e.get(key) or e["full"])["net"])
        best_by_family[fam] = best
        ranked.append({"family": fam, "best": best["label"],
                       "in_sample": best.get("in_sample", {}), "oos": best.get("oos", {}),
                       "full": best["full"]})
    # Rank by resulting NET P&L — the honest "where do you end up", not raw loss_cut (which
    # ignores the winners a trigger-happy rule gives up: EMA-break "cuts" 92k of loss but
    # nets 5k because it also flattens every winner).
    ranked.sort(key=lambda r: r["full"]["net"], reverse=True)

    # Per-cycle net under the TOP-ranked rule → the before/after table.
    top_nets = best_by_family[ranked[0]["family"]]["_nets"] if ranked else baseline_nets

    robustness = {fam: [{"label": e["label"], "net": e["full"]["net"],
                         "loss_cut": e["full"]["loss_cut"],
                         "winners_hurt": e["full"]["winners_hurt"]} for e in entries]
                  for fam, entries in families.items()}

    return {
        "params": {"strategy_id": params.strategy_id, "underlying": params.underlying,
                   "start": params.start.isoformat(), "end": params.end.isoformat(),
                   "oos_start": params.oos_start.isoformat(), "num_cycles": len(cycles)},
        "baseline": baseline,
        "top_rule": ranked[0] if ranked else None,
        "rules": ranked,
        "robustness": robustness,
        "cycles": [_cycle_row(c, top_nets[i], daily, params.oos_start)
                   for i, c in enumerate(cycles)],
        "caveats": [
            "Rules are early-exit overlays on the baseline; a rule that fires after the "
            "baseline's own exit does nothing (the baseline stands).",
            "Exit-only + entry-skip rules are modelled EXACTLY over the marked MTM path; "
            "mid-cycle rolling/adjustment is out of scope (it would change the book).",
            "Rupee thresholds are per BODY-UNIT so they're comparable across lot-size eras.",
            "VIX / trend signals are daily-granularity (exit at that day's close); the "
            "trailing + intraday-stop rules use the full minute path.",
            "Per-cycle charge is backed out of the baseline and held constant across exits "
            "(a small approximation on STT/turnover).",
        ],
    }


def _cycle_row(c: dict, top_net: float, daily: pd.DataFrame, oos_start: date) -> dict:
    """One row for the before/after table + the signals that flagged (or missed) it."""
    path = c["path"]
    bu = path["body_units"]
    es = path.get("entry_spot")
    days = sorted(set(path["day"]))
    spots = [daily.loc[d, "close"] for d in days if d in daily.index]
    vixs = [daily.loc[d, "vix"] for d in days
            if d in daily.index and daily.loc[d, "vix"] is not None]
    max_move = max((abs((s - es) / es * 100.0) for s in spots), default=0.0) if es else 0.0
    return {"entry_date": c["entry"].strftime("%Y-%m-%d %H:%M"),
            "baseline_exit": c["baseline_exit"].strftime("%Y-%m-%d %H:%M"),
            "baseline_net": round(c["baseline_net"], 0),
            "top_rule_net": round(top_net, 0),
            "entry_spot": round(es, 0) if es else None,
            "body_units": bu,
            "peak_per_unit": round(float(max(path["mtm"])) / bu, 1),
            "trough_per_unit": round(float(min(path["mtm"])) / bu, 1),
            "max_move_pct": round(max_move, 2),
            "max_vix": round(max(vixs), 1) if vixs else None,
            "in_sample": c["entry"].date() < oos_start}
