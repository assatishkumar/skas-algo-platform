"""Black-Scholes pricing/greeks/IV for European index options."""

from __future__ import annotations

import math

import pytest

from skas_algo.engine.options import black_scholes as bs


def test_textbook_call_put_values():
    # S=100,K=100,T=1,r=5%,sigma=20%,q=0 -> known BS values.
    call = bs.price(100, 100, 1.0, 0.05, 0.20, "CE")
    put = bs.price(100, 100, 1.0, 0.05, 0.20, "PE")
    assert call == pytest.approx(10.4506, abs=1e-3)
    assert put == pytest.approx(5.5735, abs=1e-3)


def test_put_call_parity():
    S, K, T, r, sig = 21000, 21500, 0.05, 0.065, 0.13
    call = bs.price(S, K, T, r, sig, "CE")
    put = bs.price(S, K, T, r, sig, "PE")
    # C - P == S - K e^{-rT}
    assert call - put == pytest.approx(S - K * math.exp(-r * T), abs=1e-6)


def test_intrinsic_at_and_after_expiry():
    assert bs.price(21500, 21000, 0.0, 0.06, 0.2, "CE") == 500  # ITM call
    assert bs.price(21000, 21500, 0.0, 0.06, 0.2, "CE") == 0    # OTM call
    assert bs.price(21000, 21500, 0.0, 0.06, 0.2, "PE") == 500  # ITM put


def test_greek_signs_and_atm_delta():
    S = K = 21000
    T, r, sig = 0.08, 0.065, 0.13
    g_ce = bs.greeks(S, K, T, r, sig, "CE")
    g_pe = bs.greeks(S, K, T, r, sig, "PE")
    assert 0 < g_ce["delta"] < 1 and -1 < g_pe["delta"] < 0
    # ATM call delta ~ 0.5+ (carry), put delta ~ -0.5
    assert g_ce["delta"] == pytest.approx(0.5, abs=0.1)
    assert g_ce["gamma"] > 0 and g_ce["vega"] > 0
    assert g_ce["theta"] < 0  # long option bleeds time value
    # call - put delta ~ e^{-qT} = 1
    assert g_ce["delta"] - g_pe["delta"] == pytest.approx(1.0, abs=1e-6)


def test_implied_vol_round_trips():
    S, K, T, r, true_sig = 21000, 21200, 0.06, 0.065, 0.145
    px = bs.price(S, K, T, r, true_sig, "CE")
    iv = bs.implied_vol(px, S, K, T, r, "CE")
    assert iv == pytest.approx(true_sig, abs=1e-4)
    # repricing at the recovered IV returns the same premium
    assert bs.price(S, K, T, r, iv, "CE") == pytest.approx(px, abs=1e-4)


def test_implied_vol_none_below_intrinsic():
    # A price below intrinsic has no real IV.
    assert bs.implied_vol(10.0, 21500, 21000, 0.05, 0.06, "CE") is None
