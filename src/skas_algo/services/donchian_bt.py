"""Backtest "screener" for donchian_strangle_bt — precomputed per-cycle leg schedules.

Live, the screener (services/donchian_strangle.py) resolves the basket's legs and the
strategy just executes them. The backtest mirrors that split: this module is the
screener run over history — for every expiry-anchored monthly cycle it computes each
name's Donchian range (previous FULL expiry→expiry window), picks the CE/PE strikes on
the name's synthetic grid, applies the live rules (premium floor, breakout-ATM
override), sizes the notional-matched NIFTY hedge off the REAL cached chain, and emits
legs in the exact shape the live strategy consumes. The strategy stays "dumb" and the
shared manage/flip code runs unchanged.

The premium floor is decided with the SAME Black-Scholes inputs the run's loader uses
(spot = entry-day close, sigma = full-history rolling HV × vol_multiplier), so a leg
kept by the schedule always prices identically at the fill — no schedule/fill drift.

The schedule is deterministic from (names, dates, params) and is injected into the
strategy via ``set_cycles`` — NOT persisted in params_snapshot (a 78-cycle × ~100-leg
schedule would bloat every run row by megabytes for re-derivable data).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import pandas as pd

from skas_algo.data.basket_options import (
    _close_series,
    stock_strike_grid,
    stock_strike_step,
)
from skas_algo.data.options_provider import VIX_SYMBOL, _ffill_lookup
from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import parse
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.engine.options.realized_vol import realized_vol_provider
from skas_algo.services.donchian_strangle import donchian_range, pick_strike, strike_step
from skas_algo.services.donchian_study import INDEX_NAME, monthly_cycles

# A quote is "fresh enough" for entry sizing if the name printed within this many days
# of the entry date — beyond that the name is suspended/delisted and sits out the cycle.
_STALE_DAYS = 7


def resolve_basket(universe: str, available: set[str], *,
                   exclude: list[str] | None = None,
                   include: list[str] | None = None) -> list[str]:
    """The basket's names: a universe preset minus ``exclude`` plus ``include`` — the
    per-run overrides from the backtest form (persisted in params, so a run documents
    exactly what it traded). Includes must be in the data cache; anything without a
    known F&O lot size is still dropped later by the schedule builder (lot_size_for
    raises KeyError — today that means includes only stick for the Nifty-50 name pool
    unless params["contract_specs"] supplies a lot size)."""
    from skas_algo.data import universes

    names = universes.resolve(universe, available)
    excl = {s.strip().upper() for s in (exclude or [])}
    names = [n for n in names if n not in excl]
    for s in include or []:
        u = s.strip().upper()
        if u and u not in names and u in available:
            names.append(u)
    return names


def _ffill_close(series: pd.Series, on: date) -> float | None:
    upto = series.loc[: pd.Timestamp(on)]
    return float(upto.iloc[-1]) if len(upto) else None


def _last_print(series: pd.Series, on: date) -> date | None:
    upto = series.loc[: pd.Timestamp(on)]
    return upto.index[-1].date() if len(upto) else None


def build_cycle_schedule(
    sd, names: list[str], start: date, end: date, *,
    r: float = 0.065, vol_window: int = 20, vol_multiplier: float = 1.0,
    skip_leg_min_premium_pct: float = 0.5, round_out: bool = False,
    breakout_atm: bool = True, lots_per_name: int = 1,
    hedge_enabled: bool = True, hedge_otm_pct: float = 4.5,
    # Sizing: lots = round(notional_per_name / (spot × lot_size)) per name. Immune to
    # splits distorting the FLAT lot table (the KOTAKBANK 4–5× artifact found analyzing
    # run 186); a name whose single lot exceeds ~1.5× the target rounds to 0 → sits out.
    # 0 → legacy fixed lots_per_name sizing.
    notional_per_name: float = 750_000.0,
    # Entry filters (0 = off), from the run-186 loss study — the danger signature is vol
    # COMPRESSION + tight channel + market stress, NOT rising vol:
    min_hv_ratio: float = 0.0,          # skip a name when HV20/HV60 < this (~0.85)
    min_channel_width_pct: float = 0.0,  # skip when (high−low)/spot·100 < this (~8)
    vix_half_threshold: float = 0.0,     # entry-day VIX > this → halve every name's size
    vix_skip_threshold: float = 0.0,     # entry-day VIX > this → skip the WHOLE cycle
) -> list[dict]:
    """[{"expiry": iso, "entry_date": iso, "legs": [live-shape leg dicts]}] per cycle."""
    index_df = sd.get_prices(symbol=INDEX_NAME, start_date=start, end_date=end,
                             asset_type="stock")
    if index_df is None or len(index_df) == 0:
        return []
    trading_days = sorted(pd.to_datetime(index_df["date"]).dt.date.tolist())
    # Real listed NIFTY expiries (2020+) anchor the cycles exactly; the calendar-expected
    # anchor fills any months outside options coverage.
    real_expiries = sorted(sd.list_option_expiries("NIFTY") or [])
    cycles = monthly_cycles(trading_days, start, end, real_expiries=real_expiries)

    frames: dict[str, pd.DataFrame] = {}
    closes: dict[str, pd.Series] = {}
    vol_fns: dict[str, Callable[[date], float]] = {}
    hv60_fns: dict[str, Callable[[date], float]] = {}
    for name in names:
        df = sd.get_prices(symbol=name, start_date=start, end_date=end, asset_type="stock")
        if df is None or len(df) == 0:
            continue
        frames[name] = df
        s = df.copy()
        s["date"] = pd.to_datetime(s["date"])
        closes[name] = s.set_index("date")["close"].sort_index()
        # Vol from the FULL cached history (same series the run's loader uses), so the
        # floor decision here equals the loader's entry price exactly.
        full = _close_series(sd, name)
        vol_fns[name] = realized_vol_provider(full, window=vol_window)
        if min_hv_ratio > 0:  # the compression gate compares short- vs medium-window HV
            hv60_fns[name] = realized_vol_provider(full, window=60)
    vix_fn = (_ffill_lookup(sd, VIX_SYMBOL)
              if (vix_half_threshold > 0 or vix_skip_threshold > 0) else None)

    nifty_closes = None
    ns = index_df.copy()
    ns["date"] = pd.to_datetime(ns["date"])
    nifty_closes = ns.set_index("date")["close"].sort_index()

    out: list[dict] = []
    for cyc in cycles:
        entry: date = cyc["entry_date"]
        expiry: date = cyc["expiry"]
        t = max((expiry - entry).days, 1) / 365.0
        legs: list[dict] = []
        agg_notional = 0.0
        # Market-stress rule (entry-day India VIX): skip the whole cycle, or halve every
        # name's size — event gaps (elections, macro shocks) are market-wide, not per-name.
        vix = vix_fn(entry) if vix_fn else None
        if vix is not None and vix_skip_threshold > 0 and vix > vix_skip_threshold:
            out.append({"expiry": expiry.isoformat(), "entry_date": entry.isoformat(),
                        "legs": []})
            continue
        size_scale = 0.5 if (vix is not None and vix_half_threshold > 0
                             and vix > vix_half_threshold) else 1.0
        for name in names:
            df = frames.get(name)
            if df is None:
                continue
            try:
                lot = lot_size_for(name, expiry)
            except KeyError:
                continue  # no F&O listing for this name (e.g. LTIM)
            series = closes[name]
            last = _last_print(series, entry)
            if last is None or (entry - last).days > _STALE_DAYS:
                continue  # suspended/not yet listed at this cycle's entry
            spot = _ffill_close(series, entry)
            rng = donchian_range(df, cyc["range_start"], cyc["range_end"])
            if rng is None or not spot:
                continue
            range_high, range_low = rng
            # Tight-channel gate: strikes hugging spot get breached trivially (run-186
            # study: <8%-wide channels were barely break-even with 2× the whipsaw).
            if (min_channel_width_pct > 0
                    and (range_high - range_low) / spot * 100.0 < min_channel_width_pct):
                continue
            # Vol-compression gate: HV20 < ~0.85×HV60 marked the WORST entries (squeeze →
            # breakout). Note it's compression that hurts — rising vol entries were fine.
            if min_hv_ratio > 0:
                hv60 = hv60_fns[name](entry)
                if hv60 > 0 and vol_fns[name](entry) / hv60 < min_hv_ratio:
                    continue
            # Per-name lots: notional-target sizing (split-proof) or legacy fixed lots.
            if notional_per_name > 0:
                lots = round(notional_per_name * size_scale / (spot * lot))
            else:
                lots = int(lots_per_name * size_scale)
            if lots <= 0:
                continue  # one lot alone would overshoot the target (e.g. pre-split KOTAK)
            step = stock_strike_step(spot)
            grid = stock_strike_grid(spot, step)
            ce_strike = pick_strike(grid, range_high, "CE", round_out)
            pe_strike = pick_strike(grid, range_low, "PE", round_out)
            # Live breakout-ATM override: spot already beyond a range edge → the would-be
            # ITM leg is skipped and the ATM opposite leg is sold instead.
            if breakout_atm:
                if ce_strike is not None and spot >= ce_strike:
                    ce_strike, pe_strike = None, pick_strike(grid, spot, "PE", False)
                elif pe_strike is not None and spot <= pe_strike:
                    ce_strike, pe_strike = pick_strike(grid, spot, "CE", False), None
            sigma = vol_fns[name](entry) * vol_multiplier
            name_legs = []
            for strike, right in ((ce_strike, "CE"), (pe_strike, "PE")):
                if strike is None:
                    continue
                prem = bs.price(spot, strike, t, r, sigma, right)
                if prem / spot * 100.0 < skip_leg_min_premium_pct:
                    continue  # live skip-leg floor — too little premium to bother selling
                name_legs.append({"underlying": name, "right": right, "strike": float(strike),
                                  "side": "sell", "lots": lots, "spot": spot,
                                  "lot_size": lot, "strike_step": step})
            if name_legs:
                legs.extend(name_legs)
                agg_notional += spot * lot * lots  # once per NAME, like the strategy
        if legs and hedge_enabled:
            legs.extend(_hedge_legs(sd, nifty_closes, entry, expiry, agg_notional,
                                    hedge_otm_pct))
        out.append({"expiry": expiry.isoformat(), "entry_date": entry.isoformat(),
                    "legs": legs})
    return out


def _hedge_legs(sd, nifty_closes: pd.Series, entry: date, expiry: date,
                agg_notional: float, hedge_otm_pct: float) -> list[dict]:
    """Long OTM NIFTY CE+PE sized to the basket notional, strikes rounded OUT from the
    REAL cached chain at entry (walking back a few days over holidays). Empty when the
    chain has no coverage (pre-2020) or the notional rounds to zero lots."""
    nifty_spot = _ffill_close(nifty_closes, entry)
    if not nifty_spot or agg_notional <= 0:
        return []
    nifty_lot = lot_size_for("NIFTY", expiry)
    lots = round(agg_notional / (nifty_spot * nifty_lot))
    if lots <= 0:
        return []
    strikes: list[float] = []
    for back in range(0, 5):
        chain = sd.get_option_chain("NIFTY", entry - timedelta(days=back))
        if chain is None or len(chain) == 0:
            continue
        exp_dates = pd.to_datetime(chain["expiry_date"]).dt.date
        strikes = sorted({float(k) for k in chain.loc[exp_dates == expiry, "strike_price"]})
        if strikes:
            break
    if not strikes:
        return []
    ce = pick_strike(strikes, nifty_spot * (1 + hedge_otm_pct / 100.0), "CE", round_out=True)
    pe = pick_strike(strikes, nifty_spot * (1 - hedge_otm_pct / 100.0), "PE", round_out=True)
    step = strike_step(strikes)
    legs = []
    for strike, right in ((ce, "CE"), (pe, "PE")):
        if strike is None:
            continue
        legs.append({"underlying": "NIFTY", "right": right, "strike": float(strike),
                     "side": "buy", "lots": int(lots), "spot": nifty_spot,
                     "lot_size": nifty_lot, "strike_step": step})
    return legs


def estimate_capital(schedule: list[dict], margin_params: dict | None = None,
                     headroom: float = 1.10) -> float | None:
    """Auto-capital for the basket backtest: modelled PEAK entry margin across cycles ×
    ``headroom``, rounded UP to the nearest lakh. The owner asked for this instead of a
    capital input — the basket's size is set by lots-per-name, so capital is a funding
    consequence, not a choice. Hedge premium (a small debit) rides inside the headroom."""
    import math

    params = MarginParams.from_dict(margin_params)
    peak = 0.0
    for cyc in schedule:
        need = sum(
            short_option_margin(leg["spot"], leg["lot_size"] * leg["lots"], 1, params)
            for leg in cyc["legs"] if leg["side"] == "sell"
        )
        peak = max(peak, need)
    if peak <= 0:
        return None
    return math.ceil(peak * headroom / 100_000) * 100_000


def basket_cycles_report(trades: list[dict], history: list[dict]) -> list[dict]:
    """Cycle-first view of a basket run for the UI: one row per monthly cycle (P&L, peak
    margin, exit reason) → per-name breakdown (lots, premium, flips, P&L) → individual
    legs (entry/exit price+date, reason). Built from the SERIALIZED trade log (ISO dates)
    + the runner history (margin curve); a cycle is keyed by the contract expiry every
    one of its legs shares."""
    cycles: dict[str, dict] = {}
    for t in trades:
        inst = parse(t["ticker"])
        if inst is None:
            continue
        key = inst.expiry.isoformat()
        cyc = cycles.setdefault(key, {"expiry": key, "trades": []})
        cyc["trades"].append((t, inst))

    out: list[dict] = []
    for key in sorted(cycles):
        rows = cycles[key]["trades"]
        dates = sorted(t["date"] for t, _ in rows)
        entry_date, exit_date = dates[0], dates[-1]
        reasons = {t.get("exit_reason") for t, _ in rows}
        exit_reason = ("portfolio_stop" if "portfolio_stop" in reasons
                       else "portfolio_target" if "portfolio_target" in reasons
                       else "expiry")

        names: dict[str, dict] = {}
        for t, inst in rows:
            n = names.setdefault(inst.underlying, {
                "name": inst.underlying, "side": "hedge" if t["action"] in ("BUY", "SELL")
                or inst.underlying == "NIFTY" else "short",
                "lot_size": inst.lot_size, "premium": 0.0, "pnl": 0.0, "charges": 0.0,
                "flips": 0, "units": 0.0, "_open": {}, "legs": [],
            })
            n["pnl"] += float(t.get("profit") or 0.0)
            n["charges"] += float(t.get("charge") or 0.0)
            if t["action"] in ("SHORT", "BUY"):
                if t["action"] == "SHORT":
                    n["premium"] += float(t["price"]) * float(t["units"])
                n["units"] = max(n["units"], float(t["units"]))
                # FIFO leg pairing: an open waits for its close (a strike CAN repeat
                # after a flip cycle back — hence a queue, not a single slot).
                n["_open"].setdefault(t["ticker"], []).append(t)
            else:  # COVER / SELL / SETTLE close the earliest open of that contract
                q = n["_open"].get(t["ticker"], [])
                o = q.pop(0) if q else None
                if t.get("exit_reason") == "flip":
                    n["flips"] += 1
                n["legs"].append({
                    "symbol": t["ticker"], "right": inst.right, "strike": inst.strike,
                    "side": "buy" if t["action"] == "SELL" or (
                        t["action"] == "SETTLE" and o and o["action"] == "BUY") else "sell",
                    "units": float(t["units"]),
                    "entry_date": o["date"] if o else None,
                    "entry_price": float(o["price"]) if o else t.get("entry_premium"),
                    "exit_date": t["date"], "exit_price": float(t["price"]),
                    "exit_reason": t.get("exit_reason") or t["action"].lower(),
                    "pnl": float(t.get("profit") or 0.0),
                })
        for n in names.values():
            # Anything never closed (shouldn't happen — expiry settles) still shows up.
            for sym, q in n.pop("_open").items():
                left = parse(sym)
                if left is None:
                    continue
                for o in q:
                    n["legs"].append({
                        "symbol": sym, "right": left.right, "strike": left.strike,
                        "side": "sell" if o["action"] == "SHORT" else "buy",
                        "units": float(o["units"]), "entry_date": o["date"],
                        "entry_price": float(o["price"]), "exit_date": None,
                        "exit_price": None, "exit_reason": "open", "pnl": 0.0,
                    })
            n["lots"] = int(n["units"] / n["lot_size"]) if n["lot_size"] else None
            n["pnl_net"] = n["pnl"] - n["charges"]
            n["legs"].sort(key=lambda x: (x["entry_date"] or "", x["exit_date"] or ""))

        margin_peak = 0.0
        for h in history:
            d = str(h["date"])[:10]
            if entry_date <= d <= exit_date and h.get("margin_used"):
                margin_peak = max(margin_peak, float(h["margin_used"]))

        name_rows = sorted(names.values(), key=lambda x: (x["side"] != "short", x["name"]))
        pnl = sum(n["pnl"] for n in name_rows)
        charges = sum(n["charges"] for n in name_rows)
        out.append({
            "cycle": key[:7], "expiry": key,
            "entry_date": entry_date, "exit_date": exit_date,
            "names": sum(1 for n in name_rows if n["side"] == "short"),
            "premium_collected": sum(n["premium"] for n in name_rows),
            "flips": sum(n["flips"] for n in name_rows),
            "exit_reason": exit_reason,
            "margin_peak": margin_peak or None,
            "pnl": pnl, "charges": charges, "pnl_net": pnl - charges,
            "return_on_margin_pct": ((pnl - charges) / margin_peak * 100.0)
            if margin_peak else None,
            "name_rows": name_rows,
        })
    return out
