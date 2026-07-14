"""NIFTY '100-multiples only' strike-selection rule (owner directive, 2026-07).

Automated options strategies must never SELECT a NIFTY 50-point strike — only round 100s. The rule
lives in ``contract_specs`` (selection_step / strike_allowed / eligible_strikes) and is enforced at
the three candidate choke points: the cached/backtest ``OptionChainView`` (Path A), and the live
``LiveOptionsMarketView.live_chain`` dict (Path B) — the latter being the coverage the fake-market
strategy tests miss (they inject a pre-built chain dict that bypasses the real view). BANKNIFTY /
SENSEX already list 100s so they must pass through unchanged.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.engine.live_options_market import LiveOptionsMarketView, _coarsen_chain
from skas_algo.engine.options.chain import OptionChainView
from skas_algo.engine.options.contract_specs import (
    eligible_strikes,
    selection_step,
    strike_allowed,
)

EXP = date(2026, 7, 28)


def _chain_df(strikes, expiry=EXP):
    rows = []
    for k in strikes:
        for rt in ("CE", "PE"):
            rows.append({
                "expiry_date": expiry, "strike_price": float(k), "option_type": rt,
                "close": 100.0, "settle_price": 100.0, "open_interest": 10,
            })
    return pd.DataFrame(rows)


def _live_chain_dict(strikes, spot, atm):
    return {
        "rows": [{"strike": float(k), "ce": {"ltp": 1.0}, "pe": {"ltp": 1.0}} for k in strikes],
        "atm_strike": float(atm), "spot": float(spot), "lot_size": 65,
    }


# --------------------------------------------------------------- helpers (unit)
def test_selection_helpers():
    assert selection_step("NIFTY") == 100
    assert selection_step("nifty") == 100                 # case-insensitive
    assert selection_step("BANKNIFTY") is None            # no rule
    assert selection_step("BANKNIFTY", 100) == 100        # falls back to the listing step
    assert strike_allowed("NIFTY", 24000) and not strike_allowed("NIFTY", 24050)
    assert strike_allowed("BANKNIFTY", 57050)             # no rule → any listed strike allowed
    assert eligible_strikes("NIFTY", [24000, 24050, 24100]) == [24000, 24100]
    assert eligible_strikes("BANKNIFTY", [57000, 57050]) == [57000, 57050]  # identity


def test_eligible_strikes_empty_fallback():
    # If the rule would drop EVERY strike (a chain with no 100-multiples), keep the full list so a
    # strategy never faces an empty chain and silently stops trading.
    assert eligible_strikes("NIFTY", [24050, 24150]) == [24050, 24150]


# --------------------------------------------------------------- Path A (OptionChainView)
def test_option_chain_view_filters_nifty_to_100s():
    strikes = list(range(24000, 24401, 50))               # 24000, 24050, … 24400
    view = OptionChainView(lambda u, on: _chain_df(strikes), lambda u, on: 24175.0)
    assert view.strikes("NIFTY", date(2026, 7, 13), EXP) == [24000.0, 24100.0, 24200.0, 24300.0, 24400.0]
    # ATM snaps to the nearest surviving 100 (24175 → 24200; the listed 24150/24200 both gone/kept
    # is irrelevant — only 100s remain as candidates).
    assert view.atm_strike("NIFTY", date(2026, 7, 13), EXP) == 24200.0


def test_option_chain_view_banknifty_unchanged():
    strikes = [57000, 57050, 57100]
    view = OptionChainView(lambda u, on: _chain_df(strikes), lambda u, on: 57030.0)
    assert view.strikes("BANKNIFTY", date(2026, 7, 13), EXP) == [57000.0, 57050.0, 57100.0]
    assert view.atm_strike("BANKNIFTY", date(2026, 7, 13), EXP) == 57050.0  # true nearest listed


# --------------------------------------------------------------- Path B (live chain dict)
def test_coarsen_chain_filters_and_recomputes_atm():
    chain = _live_chain_dict(range(24000, 24401, 50), spot=24175.0, atm=24150.0)
    out = _coarsen_chain("NIFTY", chain)
    assert sorted(r["strike"] for r in out["rows"]) == [24000.0, 24100.0, 24200.0, 24300.0, 24400.0]
    # atm_strike is RECOMPUTED to the nearest surviving strike (was the listed 24150, now gone) —
    # else call_put_ratio_expiry's rows.get(atm) would miss and the strategy would silently no-op.
    assert out["atm_strike"] == 24200.0
    # The cached adapter dict is never mutated (shallow copy).
    assert chain["rows"][1]["strike"] == 24050.0 and chain["atm_strike"] == 24150.0


def test_coarsen_chain_non_nifty_is_identity():
    chain = _live_chain_dict([57000, 57050, 57100], spot=57030.0, atm=57050.0)
    assert _coarsen_chain("BANKNIFTY", chain) is chain   # untouched, same object


def test_live_options_market_live_chain_coarsens_nifty():
    cache = OptionChainView(lambda u, on: None, lambda u, on: None)
    mv = LiveOptionsMarketView(cache)
    mv.set_chain_fn(lambda u, e: _live_chain_dict(range(24000, 24401, 50), 24175.0, 24150.0))
    out = mv.live_chain("NIFTY", EXP.isoformat())
    assert out is not None
    assert all(r["strike"] % 100 == 0 for r in out["rows"])
    assert out["atm_strike"] == 24200.0
