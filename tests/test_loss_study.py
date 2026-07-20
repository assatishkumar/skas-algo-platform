"""loss_study: post-hoc loss-reduction rules over reconstructed cycle paths.

Pure functions over synthetic cycles + a hand-built daily frame — no replay, no store,
no network. Pins the load-bearing behaviours: path reconstruction from legs, the
early-exit OVERLAY semantics (a rule only bites before the baseline's own exit), and that
each rule family fires on the case it targets without touching the others.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.services import loss_study as LS

EXP = "2026-02-26"


def _bars(symbol, day_prices):
    """day_prices: {(y,m,d,hh,mm): close}. A one-leg minute frame the loader returns."""
    rows = [{"start": pd.Timestamp(*k), "close": px} for k, px in day_prices.items()]
    return pd.DataFrame(rows).sort_values("start").reset_index(drop=True)


def _cycle(entry, exit_, net, entry_spot, legs):
    """legs: [(strike, right, side, units, entry_premium, {ts: close})]."""
    return {
        "underlying": "NIFTY", "entry_date": entry, "exit_date": exit_,
        "net_pnl": net, "underlying_entry": entry_spot,
        "legs_detail": [
            {"symbol": f"NIFTY|{EXP}|{k}|{r}", "strike": k, "right": r, "side": sd,
             "units": u, "entry_premium": ep, "expiry": EXP}
            for (k, r, sd, u, ep, _px) in legs],
        "_px": {f"NIFTY|{EXP}|{k}|{r}": px for (k, r, sd, u, ep, px) in legs},
    }


def _loader_for(cycles):
    store = {}
    for c in cycles:
        for leg in c["legs_detail"]:
            store[leg["symbol"]] = c["_px"][leg["symbol"]]

    def loader(u, expiry, strike, right, d1, d2):
        px = store.get(f"{u}|{expiry}|{int(strike)}|{right}")
        return _bars(None, px) if px else None
    return loader


def _daily(rows):
    """rows: [(date, close, vix, vix_prev, st_dir)]. ema/hv filled inertly."""
    df = pd.DataFrame([{"date": d, "close": c, "vix": v, "vix_prev": vp, "st_dir": st,
                        "ema_up": c + 1e6, "ema_lo": c - 1e6, "hv20": 10.0}
                       for (d, c, v, vp, st) in rows]).set_index("date")
    return df


def test_reconstruct_path_mtm_and_overlay():
    # One short leg (100 units) sold at 50: MTM = -100*(close-50). Rises to +? then falls.
    c = _cycle("2026-02-02 09:20", "2026-02-04 15:20", -500.0, 25000.0,
               [(25000, "CE", "short", 100, 50.0, {
                   (2026, 2, 2, 9, 20): 50.0,   # 0
                   (2026, 2, 2, 9, 21): 30.0,   # +2000 (profit: sold at 50, now 30)
                   (2026, 2, 3, 15, 20): 90.0,  # -4000
                   (2026, 2, 4, 15, 20): 55.0})])  # -500 (baseline exit here)
    path = LS.reconstruct_path(c, _loader_for([c]))
    assert path is not None
    assert path["body_units"] == 100
    assert LS._mtm_at(path, pd.Timestamp(2026, 2, 2, 9, 21)) == 2000.0
    assert LS._mtm_at(path, pd.Timestamp(2026, 2, 3, 15, 20)) == -4000.0
    # Overlay: a rule firing at 09:21 (before the baseline exit) banks +2000 minus charge;
    # the baseline charge is backed out so baseline_net is reproduced when the rule is off.
    baseline_exit = pd.Timestamp(2026, 2, 4, 15, 20)
    charge = LS._cycle_charge(path, -500.0, baseline_exit)   # = mtm(-500 at exit) - (-500) = 0
    assert charge == 0.0
    assert LS._apply(path, -500.0, baseline_exit, pd.Timestamp(2026, 2, 2, 9, 21), charge) == 2000.0
    # A rule that fires AT/AFTER the baseline exit does nothing.
    assert LS._apply(path, -500.0, baseline_exit, baseline_exit, charge) == -500.0
    assert LS._apply(path, -500.0, baseline_exit, None, charge) == -500.0


def _build(cycles, daily, **kw):
    p = LS.LossStudyParams(oos_start=date(2026, 3, 1), **kw)
    built = LS.build_cycles(cycles, _loader_for(cycles), p)
    return LS.run_study(built, daily, p), p


def test_baseline_is_the_unfiltered_sum():
    c1 = _cycle("2026-02-02 09:20", "2026-02-02 15:20", 3000.0, 25000.0,
                [(25000, "CE", "short", 100, 50.0,
                  {(2026, 2, 2, 9, 20): 50.0, (2026, 2, 2, 15, 20): 20.0})])
    c2 = _cycle("2026-02-09 09:20", "2026-02-09 15:20", -2000.0, 25000.0,
                [(25000, "PE", "short", 100, 50.0,
                  {(2026, 2, 9, 9, 20): 50.0, (2026, 2, 9, 15, 20): 70.0})])
    daily = _daily([(date(2026, 2, 2), 25000, 12, 12, 1), (date(2026, 2, 9), 25000, 12, 12, 1)])
    out, _ = _build([c1, c2], daily)
    assert out["baseline"]["net"] == 1000.0            # 3000 - 2000
    assert out["baseline"]["delta"] == 0.0
    assert out["baseline"]["worst_cycle"] == -2000.0


def test_vix_level_exit_cuts_the_spike_loser():
    # A cycle that is flat on day 1 then blows out on a VIX-spike day 2; a VIX>=20 exit on
    # day 1's close (before the blowout) banks the small day-1 mark instead of the loss.
    c = _cycle("2026-02-02 09:20", "2026-02-03 15:20", -6000.0, 25000.0,
               [(25000, "CE", "short", 100, 50.0, {
                   (2026, 2, 2, 15, 20): 50.0,     # day1 close: MTM 0
                   (2026, 2, 3, 15, 20): 110.0})])  # day2 close: MTM -6000 (baseline)
    daily = _daily([(date(2026, 2, 2), 25000, 21, 13, 1),   # day1 VIX already 21 (>=20)
                    (date(2026, 2, 3), 25400, 24, 21, -1)])
    out, _ = _build([c], daily)
    vix = next(r for r in out["rules"] if r["family"] == "vix_level")
    assert vix["full"]["net"] == 0.0        # exited day1 at MTM 0, not the -6000 baseline
    assert vix["full"]["loss_cut"] == 6000.0


def test_entry_filter_skips_the_cycle_entirely():
    # Two cycles; the loser is entered when VIX-HV20 is thin (11-10=1 < 2) → the vol-premium
    # entry filter drops it, removing its -4000 (and its risk) from the book.
    win = _cycle("2026-02-02 09:20", "2026-02-02 15:20", 3000.0, 25000.0,
                 [(25000, "CE", "short", 100, 50.0,
                   {(2026, 2, 2, 9, 20): 50.0, (2026, 2, 2, 15, 20): 20.0})])
    lose = _cycle("2026-02-09 09:20", "2026-02-09 15:20", -4000.0, 25000.0,
                  [(25000, "PE", "short", 100, 50.0,
                    {(2026, 2, 9, 9, 20): 50.0, (2026, 2, 9, 15, 20): 90.0})])
    daily = _daily([(date(2026, 2, 2), 25000, 20, 20, 1),    # win: premium 20-10=10, kept
                    (date(2026, 2, 9), 25000, 11, 11, 1)])   # lose: premium 11-10=1 < 2, skip
    out, _ = _build([win, lose], daily)
    filt = next(r for r in out["rules"] if r["family"] == "entry_vol_premium")
    # best filter should keep the winner and drop the loser → net 3000 (vs baseline -1000).
    assert out["baseline"]["net"] == -1000.0
    assert filt["full"]["net"] == 3000.0


def test_trailing_locks_a_give_back_cycle():
    # Peaks at +4000 (40/unit) then round-trips to a small loss; arm@10/keep-70% exits near
    # the peak. Baseline rode to a -500 exit.
    c = _cycle("2026-02-02 09:20", "2026-02-04 15:20", -500.0, 25000.0,
               [(25000, "CE", "short", 100, 50.0, {
                   (2026, 2, 2, 9, 20): 50.0,     # 0
                   (2026, 2, 2, 10, 0): 10.0,     # +4000 peak (40/unit)
                   (2026, 2, 2, 11, 0): 40.0,     # +1000  (gave back below 70% of 4000=2800)
                   (2026, 2, 4, 15, 20): 55.0})])  # -500 baseline
    daily = _daily([(date(2026, 2, 2), 25000, 12, 12, 1), (date(2026, 2, 4), 25000, 12, 12, 1)])
    out, _ = _build([c], daily)
    tr = next(r for r in out["rules"] if r["family"] == "trailing")
    assert tr["full"]["net"] > -500.0            # trailing beat the baseline ride-down
    assert tr["full"]["loss_cut"] > 0.0
