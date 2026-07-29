"""Per-run analytics bundle for the Analyze workbench (design_handoff_analyze, 2026-07-28).

Builds ONE self-contained JSON bundle per completed options run: enriched per-trade rows
(MAE/MFE, skew, VIX, DTE, credits), a 5-minute downsampled normalized MTM path per trade
(powers the Stop/Target Simulator's crossing detection + the path-spaghetti panel), daily
risk series, cost totals and headline KPIs. The FRONTEND does all interactive math
(sliders, bucketing, normalization) client-side over this bundle — the backend computes
once and caches.

Compute is a background job on the ``analytics`` slot of ``services.replay_jobs`` (never
contends with an intraday replay / loss study on the default slot), triggered on first
open of a run in Analyze. Result cached as ``~/.skas_data/analytics/run_<id>.v<V>.json.gz``
(atomic tmp→rename, the option-store convention); the cache is keyed by bundle VERSION +
the run's trade count, so a re-saved run recomputes.

MAE/MFE reconstruction reuses ``loss_study.reconstruct_path`` over the 1-min option store
— intraday-basis runs only; eod/live-basis runs get a degraded bundle (coverage map says
what's missing) rather than an error. KPI invariants: total P&L / win rate / max DD are
taken from the run's OWN report so Analyze always agrees with Run Detail to the rupee.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
from datetime import date, datetime
from pathlib import Path

import numpy as np

from skas_algo.data.option_intraday_store import load_contract_bars
from skas_algo.data.options_provider import INDEX_SYMBOL, VIX_SYMBOL, _ffill_lookup
from skas_algo.engine.jsonutil import to_native
from skas_algo.services.loss_study import reconstruct_path

logger = logging.getLogger("skas_algo")

BUNDLE_VERSION = 5  # v5: liquidity flags, daily margin, regimes, cost breakdown
ANALYTICS_DIR = Path.home() / ".skas_data" / "analytics"


# ------------------------------------------------------------------- cache
def _cache_path(run_id: int) -> Path:
    return ANALYTICS_DIR / f"run_{run_id}.v{BUNDLE_VERSION}.json.gz"


def load_cached(run_id: int, trade_count: int) -> dict | None:
    """The cached bundle, or None when absent/stale (trade count changed = re-saved run)."""
    p = _cache_path(run_id)
    if not p.exists():
        return None
    try:
        with gzip.open(p, "rt") as f:
            bundle = json.load(f)
    except Exception:  # pragma: no cover - a corrupt cache is just a recompute
        logger.exception("analytics cache unreadable for run %s", run_id)
        return None
    if bundle.get("trade_count") != trade_count:
        return None
    return bundle


def _write_cache(run_id: int, bundle: dict) -> None:
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _cache_path(run_id).with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(bundle, f)
    tmp.rename(_cache_path(run_id))


# ------------------------------------------------------------------- pieces
def _basis(mode: str, params: dict) -> str:
    if str(mode).upper() in ("PAPER", "LIVE"):
        return "live"
    return "intraday" if str(params.get("data_basis", "")) == "intraday" else "eod"


def _parse_dt(v) -> datetime:
    return datetime.fromisoformat(str(v).replace("T", " ").replace("Z", ""))


def _downsample_path(minutes, mtm, credit_rs: float, step: int = 5) -> list[list[float]]:
    """[[minutes-since-entry, mtm as % of credit], …] every ``step`` minutes + the last
    point — small enough to ship (~75 pts for a full session), fine-grained enough for
    the simulator's stop/target crossing detection."""
    if credit_rs <= 0 or len(minutes) == 0:
        return []
    t0 = minutes[0]
    out: list[list[float]] = []
    for i in range(len(minutes)):
        off = int((minutes[i] - t0).total_seconds() // 60)
        if i == len(minutes) - 1 or off % step == 0:
            pct = 100.0 * float(mtm[i]) / credit_rs
            if out and out[-1][0] == off:
                out[-1][1] = round(pct, 2)
            else:
                out.append([off, round(pct, 2)])
    return out


def _trade_row(idx: int, cyc: dict, path: dict | None, margin_rs: float | None) -> dict:
    legs = cyc.get("legs_detail") or []
    shorts = [lg for lg in legs if lg.get("side") == "short"]
    credit_rs = sum(float(lg["entry_premium"]) * float(lg["units"]) for lg in shorts)
    credit_pts = sum(float(lg["entry_premium"]) for lg in shorts)
    entry, ex = _parse_dt(cyc["entry_date"]), _parse_dt(cyc["exit_date"])
    expiry = date.fromisoformat(str(cyc.get("expiry"))[:10]) if cyc.get("expiry") else None
    pe = [lg for lg in legs if lg.get("right") == "PE" and lg.get("side") == "short"]
    ce = [lg for lg in legs if lg.get("right") == "CE" and lg.get("side") == "short"]

    def _skew(price_key: str) -> float | None:
        # PE/CE premium ratio of the short body — >1 = puts pricier (fear priced in)
        if not pe or not ce:
            return None
        c = sum(float(lg.get(price_key) or 0) for lg in ce)
        p = sum(float(lg.get(price_key) or 0) for lg in pe)
        return round(p / c, 3) if c > 0 else None

    row: dict = {
        "id": idx,
        "entry": cyc["entry_date"],
        "exit": cyc["exit_date"],
        "dte": (expiry - entry.date()).days if expiry else None,
        "weekday": entry.weekday(),
        "entry_slot": entry.strftime("%H:%M"),
        "credit_rs": round(credit_rs, 2),
        "credit_pts": round(credit_pts, 2),
        "pnl_rs": round(float(cyc.get("net_pnl") or 0.0), 2),
        "pnl_pct_credit": round(100.0 * float(cyc.get("net_pnl") or 0.0) / credit_rs, 2)
        if credit_rs > 0 else None,
        "pnl_pe_rs": round(sum(float(lg.get("pnl") or 0) for lg in legs
                               if lg.get("right") == "PE"), 2),
        "pnl_ce_rs": round(sum(float(lg.get("pnl") or 0) for lg in legs
                               if lg.get("right") == "CE"), 2),
        "skew_entry": _skew("entry_premium"),
        "skew_exit": _skew("exit_price"),
        "vix_entry": cyc.get("vix_entry"),
        "vix_exit": cyc.get("vix_exit"),
        "u_move_pct": cyc.get("underlying_pct"),
        "exit_reason": cyc.get("exit_reason"),
        "hold_min": int((ex - entry).total_seconds() // 60),
        # the strategy's actual risk denominator that day (frozen margin_base in the
        # replay) — the simulator's %-of-margin basis divides by THIS, per trade
        "margin_rs": round(margin_rs, 2) if margin_rs else None,
        "n_legs": len(legs),
        "units": int(shorts[0]["units"]) if shorts else None,
        "mae_pct": None, "mfe_pct": None, "t_mfe_min": None, "path5": [],
    }
    if path is not None and credit_rs > 0:
        mtm = path["mtm"]
        row["mae_pct"] = round(100.0 * float(np.min(mtm)) / credit_rs, 2)
        row["mfe_pct"] = round(100.0 * float(np.max(mtm)) / credit_rs, 2)
        imax = int(np.argmax(mtm))
        row["t_mfe_min"] = int((path["minutes"][imax] - path["minutes"][0])
                               .total_seconds() // 60)
        row["path5"] = _downsample_path(path["minutes"], mtm, credit_rs)
    return row


def _iv_hv(cyc: dict, hv_fn, underlying: str) -> float | None:
    """IV−HV spread at entry (vol points): BS-implied vol of the short ATM CE at its entry
    premium (spot ≈ strike — the structures enter ATM) minus HV20 of the underlying.
    Best-effort: None when either leg can't be computed (missing data, 0-DTE at the wire)."""
    try:
        from skas_algo.engine.options.black_scholes import implied_vol

        legs = cyc.get("legs_detail") or []
        ce = next((lg for lg in legs if lg.get("right") == "CE" and lg.get("side") == "short"),
                  None)
        if ce is None or hv_fn is None:
            return None
        entry = _parse_dt(cyc["entry_date"])
        expiry = datetime.fromisoformat(str(ce.get("expiry"))[:10]).replace(hour=15, minute=30)
        t = max((expiry - entry).total_seconds() / (365.0 * 86400.0), 1.0 / (365.0 * 12))
        iv = implied_vol(float(ce["entry_premium"]), float(ce["strike"]), float(ce["strike"]),
                         t, 0.065, "CE")
        hv = hv_fn(underlying, entry.date())
        if iv is None or hv is None:
            return None
        return round(iv * 100.0 - hv, 2)
    except Exception:  # pragma: no cover - a bad cycle degrades the field only
        return None


def _liquidity_check(cyc: dict, bars_cache: dict) -> tuple[int, list[dict]]:
    """Phantom-fill check (6.5): a fill is flagged when the fill minute's traded volume
    could not have absorbed its size. Reuses the bars already loaded for the path —
    zero extra store reads. Returns (fills_checked, flagged_rows)."""
    checked = 0
    flags: list[dict] = []
    frames = list(bars_cache.values())
    for lg in cyc.get("legs_detail") or []:
        df = None
        for key, f in bars_cache.items():
            if (key[2] == float(lg["strike"]) and key[3] == lg["right"]):
                df = f
                break
        if df is None or len(df) == 0 or "volume" not in df.columns:
            continue
        vol = {str(s)[:16]: float(v) for s, v in zip(df["start"], df["volume"])}
        units = float(lg["units"])
        for which, ts in (("entry", lg.get("entry_date")), ("exit", lg.get("exit_date"))):
            if not ts:
                continue
            v = vol.get(str(ts)[:16].replace("T", " "))
            if v is None:
                continue
            checked += 1
            if v < units:
                flags.append({
                    "date": str(ts)[:16], "which": which,
                    "leg": f"{lg['strike']:g} {lg['right']}",
                    "size": int(units), "volume": int(v),
                    "ratio": round(units / v, 1) if v > 0 else 99.0,
                })
    _ = frames  # keep the cache alive through the loop
    return checked, flags


def _daily_series(equity_curve: list[dict], capital: float) -> tuple[list[dict], dict]:
    """Per-day {date, pnl, equity, dd} + derived risk series (rolling Sharpe 63d,
    autocorrelation lags 1-10 with the 95% significance band)."""
    daily: list[dict] = []
    prev = capital
    peak = capital
    for pt in equity_curve:
        eq = float(pt["equity"])
        peak = max(peak, eq)
        daily.append({"date": str(pt["date"])[:10], "pnl": round(eq - prev, 2),
                      "equity": round(eq, 2), "dd": round(eq - peak, 2)})
        prev = eq
    pnl = np.array([d["pnl"] for d in daily], dtype=float)
    series: dict = {"rolling_sharpe": [], "autocorr": [], "sig_band": None}
    if len(pnl) >= 20:
        win = 63
        for i in range(win, len(pnl) + 1):
            w = pnl[i - win:i]
            sd = float(np.std(w))
            if sd > 0:
                series["rolling_sharpe"].append(
                    {"date": daily[i - 1]["date"],
                     "v": round(float(np.mean(w)) / sd * math.sqrt(252), 2)})
        x = pnl - pnl.mean()
        denom = float(np.sum(x * x))
        if denom > 0:
            for lag in range(1, 11):
                series["autocorr"].append(
                    {"lag": lag, "v": round(float(np.sum(x[lag:] * x[:-lag])) / denom, 3)})
            series["sig_band"] = round(1.96 / math.sqrt(len(pnl)), 3)
    return daily, series


def _correlations(daily: list[dict], underlying: str) -> dict:
    """Daily-P&L correlation vs the underlying's return and vs VIX change (cache series)."""
    out = {"nifty": None, "vix": None}
    try:
        from skas_algo.data.provider import get_data_cache

        sd = get_data_cache()
        spot = _ffill_lookup(sd, INDEX_SYMBOL.get(underlying.upper(), underlying))
        vix = _ffill_lookup(sd, VIX_SYMBOL)
        rows = []
        for i in range(1, len(daily)):
            d0, d1 = date.fromisoformat(daily[i - 1]["date"]), date.fromisoformat(daily[i]["date"])
            s0, s1, v0, v1 = spot(d0), spot(d1), vix(d0), vix(d1)
            if None in (s0, s1, v0, v1) or not s0:
                continue
            rows.append((daily[i]["pnl"], (s1 - s0) / s0, v1 - v0))
        if len(rows) >= 20:
            a = np.array(rows, dtype=float)
            if np.std(a[:, 0]) > 0:
                if np.std(a[:, 1]) > 0:
                    out["nifty"] = round(float(np.corrcoef(a[:, 0], a[:, 1])[0, 1]), 3)
                if np.std(a[:, 2]) > 0:
                    out["vix"] = round(float(np.corrcoef(a[:, 0], a[:, 2])[0, 1]), 3)
    except Exception:  # pragma: no cover - correlations are best-effort context
        logger.exception("analytics correlations failed")
    return out


# Structural regime boundaries the risk section flags (7.10): what changed mid-history,
# so a performance break has a candidate explanation before blaming the edge.
_EXPIRY_MOVES = {
    "NIFTY": [("2025-09-01", "Weekly expiry moved Thu → Tue")],
    "SENSEX": [("2025-01-01", "Weekly expiry moved Fri → Thu")],
}


def _regimes(underlying: str, start: str | None, end: str | None) -> list[dict]:
    out: list[dict] = []
    try:
        from skas_algo.engine.options.contract_specs import _LOT_SIZES

        eras = _LOT_SIZES.get(underlying.upper()) or []
        for i in range(1, len(eras)):
            d = eras[i][0].isoformat()
            out.append({"date": d,
                        "label": f"Lot size {eras[i - 1][1]} → {eras[i][1]}"})
    except Exception:  # pragma: no cover
        logger.exception("lot-era regimes unavailable")
    out += [{"date": d, "label": lab} for d, lab in _EXPIRY_MOVES.get(underlying.upper(), [])]
    lo, hi = start or "0000", end or "9999"
    return sorted([r for r in out if lo <= r["date"] <= hi], key=lambda r: r["date"])


_SLOT0 = 9 * 60 + 15          # 09:15 IST, minutes-from-midnight
N_SLOTS = 26                  # 15-min slots 09:15 → 15:30


def _slot_of(dt: datetime) -> int | None:
    m = dt.hour * 60 + dt.minute - _SLOT0
    return m // 15 if 0 <= m < N_SLOTS * 15 else None


def slot_labels() -> list[str]:
    return [f"{(_SLOT0 + 15 * i) // 60:02d}:{(_SLOT0 + 15 * i) % 60:02d}" for i in range(N_SLOTS)]


class _MeltAgg:
    """Time-of-day aggregation straight from the reconstructed paths — for an ALL-SHORT
    equal-units structure, combined premium(t) = credit_pts − mtm(t)/units, so the melt
    curve costs no extra store reads. Curves are averaged per 15-min slot, normalized to
    the trade's first slot; MTM overlay is % of credit."""

    def __init__(self):
        self.prem = {k: [ [0.0, 0] for _ in range(N_SLOTS)] for k in ("0", "1", "2", "3+")}
        self.prem_vix0 = {k: [[0.0, 0] for _ in range(N_SLOTS)] for k in ("low", "med", "high")}
        self.mtm = {k: [[0.0, 0] for _ in range(N_SLOTS)] for k in ("all", "0", "1", "2", "3+")}
        self.entries = [0] * N_SLOTS
        self.exits = [0] * N_SLOTS
        self.n = 0

    @staticmethod
    def _dkey(dte) -> str | None:
        if dte is None:
            return None
        return "3+" if dte >= 3 else str(int(dte))

    def add(self, cyc: dict, path: dict, credit_rs: float, credit_pts: float,
            dte, vix) -> None:
        legs = cyc.get("legs_detail") or []
        units = {int(lg["units"]) for lg in legs}
        if any(lg.get("side") != "short" for lg in legs) or len(units) != 1 or credit_pts <= 0:
            return  # premium identity only holds for equal-units all-short structures
        u = units.pop()
        dk = self._dkey(dte)
        if dk is None:
            return
        mins, mtm = path["minutes"], path["mtm"]
        # per-slot last observation
        prem_slot: dict[int, float] = {}
        mtm_slot: dict[int, float] = {}
        for i in range(len(mins)):
            sl = _slot_of(mins[i])
            if sl is None:
                continue
            prem_slot[sl] = credit_pts - float(mtm[i]) / u
            mtm_slot[sl] = 100.0 * float(mtm[i]) / credit_rs
        if not prem_slot:
            return
        first = prem_slot[min(prem_slot)]
        if first <= 0:
            return
        vk = None if vix is None else ("high" if vix >= 20 else "med" if vix >= 13 else "low")
        for sl, v in prem_slot.items():
            self.prem[dk][sl][0] += v / first
            self.prem[dk][sl][1] += 1
            if dk == "0" and vk is not None:
                self.prem_vix0[vk][sl][0] += v / first
                self.prem_vix0[vk][sl][1] += 1
        for sl, v in mtm_slot.items():
            for key in ("all", dk):
                self.mtm[key][sl][0] += v
                self.mtm[key][sl][1] += 1
        e0, e1 = _slot_of(path["minutes"][0]), _slot_of(path["minutes"][-1])
        if e0 is not None:
            self.entries[e0] += 1
        if e1 is not None:
            self.exits[e1] += 1
        self.n += 1

    def result(self) -> dict | None:
        if self.n == 0:
            return None
        avg = lambda rows: [round(v / c, 4) if c else None for v, c in rows]  # noqa: E731
        return {
            "slots": slot_labels(),
            "trades": self.n,
            "premium_by_dte": {k: avg(v) for k, v in self.prem.items()},
            "premium_0dte_by_vix": {k: avg(v) for k, v in self.prem_vix0.items()},
            "mtm_by_dte": {k: avg(v) for k, v in self.mtm.items()},
            "entries": self.entries,
            "exits": self.exits,
        }


# -------------------------------------------------------------------- build
def build_bundle(run_id: int, progress=None) -> dict:
    """Assemble the analytics bundle for a saved options run (see module docstring).
    Raises ValueError with a user-facing message when the run can't be analyzed."""
    from skas_algo.db.base import session_scope
    from skas_algo.db.models import Algo, AlgoRun

    with session_scope() as db:
        run = db.get(AlgoRun, run_id)
        if run is None:
            raise ValueError("run not found")
        algo = db.get(Algo, run.algo_id)
        report = dict(run.metrics or {})
        trade_log = list(run.trade_log or [])
        params = dict((algo.params if algo else None) or {})
        name = algo.name if algo else f"run #{run_id}"
        strategy_id = algo.strategy_id if algo else ""
        capital = float(algo.capital or 0.0) if algo else 0.0
        mode = run.mode.value if hasattr(run.mode, "value") else str(run.mode)

    options = report.get("options") or {}
    # The intraday replay stores exit_date on the LEGS, not the cycle (run #216 taught us);
    # a cycle is CLOSED when every leg has exited — inject the derived cycle-level exit so
    # reconstruct_path and the trade rows have one authoritative timestamp.
    cycles = []
    for c in options.get("cycles") or []:
        legs = c.get("legs_detail") or []
        if not c.get("exit_date"):
            exits = [str(lg.get("exit_date")) for lg in legs if lg.get("exit_date")]
            if legs and len(exits) == len(legs):
                c = {**c, "exit_date": max(exits)}
        if c.get("exit_date"):
            cycles.append(c)
    if not cycles:
        raise ValueError("this run has no closed options cycles — the Analyze workbench "
                         "needs a completed options backtest")
    basis = _basis(mode, params)
    metrics = report.get("metrics") or {}
    margin_ref = float((options.get("summary") or {}).get("max_margin_used") or 0.0)
    underlying = (params.get("underlying")
                  or (cycles[0].get("underlying") if cycles else None) or "NIFTY")

    # VIX context: older saved runs' cycles predate enrich_with_market — fill from the
    # cached daily India VIX series so the conditioner has the field either way.
    vix_fn = None
    try:
        from skas_algo.data.provider import get_data_cache

        vix_fn = _ffill_lookup(get_data_cache(), VIX_SYMBOL)
    except Exception:  # pragma: no cover - VIX context is best-effort
        logger.exception("VIX lookup unavailable for analytics")

    # per-day frozen margin (the replay's margin_series) → each trade's risk denominator
    margin_by_day: dict[str, float] = {}
    for m in options.get("margin_series") or []:
        try:
            margin_by_day[str(m.get("date"))[:10]] = float(m.get("margin") or 0.0)
        except (TypeError, ValueError):  # pragma: no cover
            continue
    margin_days = sorted(margin_by_day)

    def _margin_at(day: str) -> float | None:
        if day in margin_by_day:
            return margin_by_day[day]
        prior = [d for d in margin_days if d <= day]
        return margin_by_day[prior[-1]] if prior else (margin_ref or None)

    # HV lookup for the IV−HV spread (best-effort; None degrades the field, not the run)
    hv_fn = None
    try:
        from skas_algo.data.provider import get_data_cache
        from skas_algo.data.options_provider import make_realized_vol_fn

        sd_hv = get_data_cache()
        hv_fn = make_realized_vol_fn(
            lambda u: sd_hv.get_prices(symbol=INDEX_SYMBOL.get(u.upper()) or u.upper(),
                                       asset_type="stock"), window=20)
    except Exception:  # pragma: no cover
        logger.exception("HV lookup unavailable for analytics")

    # --- per-trade rows (+ minute-path reconstruction on the intraday basis) ---
    trades: list[dict] = []
    paths_missing = 0
    melt = _MeltAgg()
    liq_checked = 0
    liq_rows: list[dict] = []
    total = len(cycles)
    for i, cyc in enumerate(sorted(cycles, key=lambda c: str(c["entry_date"]))):
        if vix_fn is not None and cyc.get("vix_entry") is None:
            cyc = {**cyc,
                   "vix_entry": vix_fn(_parse_dt(cyc["entry_date"]).date()),
                   "vix_exit": vix_fn(_parse_dt(cyc["exit_date"]).date())}
        path = None
        bars_cache: dict = {}   # one store read per leg, shared by path + liquidity check

        def _loader(u, exp, k, r, d1, d2, _c=bars_cache):
            key = (u, exp, k, r, str(d1), str(d2))
            if key not in _c:
                _c[key] = load_contract_bars(u, exp, k, r, d1, d2)
            return _c[key]

        if basis == "intraday":
            try:
                path = reconstruct_path(cyc, _loader)
            except Exception:  # pragma: no cover - a bad day never kills the bundle
                logger.exception("path reconstruction failed for cycle %s", i)
        if basis == "intraday" and path is None:
            paths_missing += 1
        row = _trade_row(i, cyc, path, _margin_at(str(cyc["entry_date"])[:10]))
        row["ivhv"] = _iv_hv(cyc, hv_fn, underlying)
        trades.append(row)
        if path is not None:
            melt.add(cyc, path, row["credit_rs"], row["credit_pts"], row["dte"],
                     row["vix_entry"])
            c, flags = _liquidity_check(cyc, bars_cache)
            liq_checked += c
            liq_rows.extend(flags)
        if progress is not None and (i % 10 == 0 or i == total - 1):
            progress(i + 1, total, str(cyc["entry_date"])[:10])
    liq_rows.sort(key=lambda r: r["ratio"], reverse=True)

    # --- charges per trade: allocate each fill's charge to the cycle whose window AND
    # leg symbols contain it (windows can overlap for positional books; the symbol match
    # disambiguates). Filtered-range KPIs need net-of-costs per trade, not just a total.
    fills = [(str(t.get("date") or ""), str(t.get("ticker") or ""),
              float(t.get("charge") or 0.0)) for t in trade_log if t.get("charge")]
    charges_total = sum(c for _, _, c in fills)
    for row, cyc in zip(trades, sorted(cycles, key=lambda c: str(c["entry_date"])),
                        strict=True):
        syms = {str(lg.get("symbol")) for lg in (cyc.get("legs_detail") or [])}
        lo, hi = str(cyc["entry_date"]), str(cyc["exit_date"])
        row["charge_rs"] = round(sum(
            c for ts, sym, c in fills
            if lo[:16] <= ts[:16] <= hi[:16] and (not sym or sym in syms)), 2)

    # run-216 lesson: the intraday replay's cycle net_pnl is ALREADY net of charges and
    # its flat rows carry no per-fill charge (Σ pnl_rs == report net to the paise) — so
    # per-trade charge_rs is 0 there and the costs section reads the report's own total.
    if not any(c for _, _, c in fills):
        charges_total = float(metrics.get("Total Charges") or 0.0)

    daily, series = _daily_series(report.get("equity_curve") or [], capital)
    series["correlations"] = _correlations(daily, underlying)
    for d in daily:  # margin per day (7.8 utilization) — ffill from the replay's series
        m = _margin_at(d["date"])
        d["margin"] = round(m, 2) if m else None

    melt_out = melt.result()
    with_paths = sum(1 for t in trades if t["path5"])
    coverage = {
        "conditioning": "full",
        "skew": "full" if any(t["skew_entry"] is not None for t in trades) else "none",
        "melt": "full" if melt_out else "none",
        "lifecycle": "full" if with_paths else "none",
        "execution": "partial" if basis != "live" else "full",
        "risk": "full",
        "simulator": "full" if with_paths else "none",
    }
    if 0 < with_paths < len(trades):
        coverage["lifecycle"] = coverage["simulator"] = "partial"

    wins = [t for t in trades if t["pnl_rs"] > 0]
    credits = [t["credit_rs"] for t in trades if t["credit_rs"] > 0]
    dpnl = np.array([d["pnl"] for d in daily], dtype=float)
    sharpe = (round(float(np.mean(dpnl) / np.std(dpnl)) * math.sqrt(252), 2)
              if len(dpnl) > 1 and float(np.std(dpnl)) > 0 else None)
    years = max(len(daily) / 252.0, 1e-9)
    net = float(metrics.get("Net Realized P&L") or sum(t["pnl_rs"] for t in trades))
    bundle = {
        "version": BUNDLE_VERSION,
        "run_id": run_id,
        "trade_count": len(trade_log),
        "name": name,
        "strategy_id": strategy_id,
        "basis": basis,
        "underlying": underlying,
        "start": daily[0]["date"] if daily else None,
        "end": daily[-1]["date"] if daily else None,
        "capital": capital,
        "kpis": {
            # headline numbers COPIED from the run's own report — Analyze must agree with
            # Run Detail to the rupee (acceptance bar), never recompute them differently.
            "total_pnl": net,
            "cagr": metrics.get("CAGR %"),
            "sharpe": sharpe,  # replay reports carry no Sharpe — daily P&L, annualized
            "win_rate": round(100.0 * len(wins) / len(trades), 1) if trades else 0.0,
            "wins": len(wins),
            "trades": len(trades),
            "max_dd": round(min((d["dd"] for d in daily), default=0.0), 2),
            "margin_ref": margin_ref,
            "rom_annual": round(100.0 * net / margin_ref / years, 1) if margin_ref else None,
            "premium_capture": round(
                100.0 * net / sum(credits), 1) if credits else None,
            "avg_credit_rs": round(float(np.mean(credits)), 0) if credits else None,
            "avg_credit_pts": round(float(np.mean(
                [t["credit_pts"] for t in trades if t["credit_pts"]])), 1)
            if credits else None,
            "avg_hold_min": round(float(np.mean([t["hold_min"] for t in trades])), 0)
            if trades else None,
        },
        "coverage": coverage,
        "paths_missing": paths_missing,
        "trades": trades,
        "melt": melt_out,
        "daily": daily,
        "series": series,
        "costs": {"charges": round(charges_total, 2),
                  "net": round(net, 2), "gross": round(net + charges_total, 2),
                  "breakdown": (options.get("charges") or None)},
        "liquidity": {"checked": liq_checked, "flagged": len(liq_rows),
                      "rows": liq_rows[:25]},
        "regimes": _regimes(underlying, daily[0]["date"] if daily else None,
                            daily[-1]["date"] if daily else None),
    }
    bundle = to_native(bundle)
    _write_cache(run_id, bundle)
    return bundle
