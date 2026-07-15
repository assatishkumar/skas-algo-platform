"""intraday_straddle_bt: replay a synthetic 1-min day through the REAL strategy — entry at
09:18 off the store-built chain (NIFTY 100-strike rule applied), decaying premiums → EOD
exit profit, model-margin push arms the stop. Store redirected to tmp, no network."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from skas_algo.data import option_intraday_store as store
from skas_algo.services.intraday_straddle_bt import replay_day, run_backtest

DAY = date(2026, 7, 15)
EXP = "2026-07-21"


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    from skas_algo.config import get_settings
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)


def _bars():
    """A tiny chain: 24000 (ATM by parity) + 24050 (50-strike, must be filtered) + 24100.
    Premiums decay through the day → a short straddle finishes in profit at 15:25."""
    rows = []

    def leg(strike, right, px_by_minute):
        sym = f"NIFTY|{EXP}|{strike}|{right}"
        for (hh, mm), px in px_by_minute.items():
            rows.append({"symbol": sym, "start": datetime(2026, 7, 15, hh, mm),
                         "open": px, "high": px, "low": px, "close": px,
                         "volume": 100.0, "oi": 5000.0})

    decay = {(9, 15): 160.0, (9, 18): 158.0, (12, 0): 130.0, (15, 20): 110.0}
    # 24000 CE/PE nearly equal → parity spot ≈ 24000 → it is the ATM.
    leg(24000, "CE", decay)
    leg(24000, "PE", {k: v + 2 for k, v in decay.items()})
    leg(24050, "CE", decay)   # 50-strike — the live coarsening must hide it
    leg(24050, "PE", decay)
    leg(24100, "CE", {k: v - 40 for k, v in decay.items()})
    leg(24100, "PE", {k: v + 45 for k, v in decay.items()})
    return pd.DataFrame(rows, columns=store.COLUMNS)


def test_replay_enters_atm_and_exits_eod_with_decay_profit():
    store.write_day(DAY, _bars())
    r = replay_day("NIFTY", DAY)
    assert r is not None and r["entered"] is True
    assert r["expiry"] == EXP and r["strikes"] == [24000.0]   # ATM, and a 100-multiple
    assert r["entry_time"] == "09:18"
    assert r["exit_time"] == "15:25" and r["exit_reason"] == "eod"
    # SELL 158 + 160 at 09:18, buy back 110 + 112 at 15:25 → +96/share × 65 units.
    assert r["pnl_rupees"] == pytest.approx(96 * 65, abs=1)
    assert r["margin_base"] > 0 and r["pnl_pct_of_margin"] > 0


def test_replay_never_picks_a_nifty_50_strike():
    df = _bars()
    # Make the 50-strike the "best" ATM by parity — it must STILL be excluded.
    df.loc[df["symbol"].str.contains("24050"), "close"] -= 1.0
    store.write_day(DAY, df)
    r = replay_day("NIFTY", DAY)
    assert r["entered"] and all(k % 100 == 0 for k in r["strikes"])


def test_run_backtest_aggregates():
    store.write_day(DAY, _bars())
    out = run_backtest(["NIFTY", "SENSEX"])          # SENSEX has no data → no result row
    assert out["totals"]["entries"] == 1
    assert [r["underlying"] for r in out["results"]] == ["NIFTY"]
