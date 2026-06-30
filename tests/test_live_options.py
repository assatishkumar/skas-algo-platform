"""Options strategies driven through the real-time LiveSession (paper, scripted quotes).

Builds the live options stack (LiveOptionsMarketView + ExpirySettler + ChargeModel) from a
fake skas-data source and drives HNI Weekly via update_quotes + run_decision at scripted
timestamps — verifying live entry (gated to 09:45), the intraday profit cadence (book every
15 min, stop held to EOD), and expiry settlement. No DB / async / network.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from skas_algo.data.options_provider import build_live_options_run
from skas_algo.engine.live import LiveSession
from skas_algo.engine.options.charges import ChargeModel
from skas_algo.strategies.hni_weekly import HniWeeklyStrategy

SPOT = 25000.0
EXPIRIES = [date(2026, 1, 6), date(2026, 1, 13), date(2026, 1, 20)]


def _biz(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _prem(strike, dte, spot, right="CE"):
    dist = (strike - spot) if right == "CE" else (spot - strike)
    return round(100.0 * math.exp(-dist / 800.0) * max(0.05, dte / 30.0), 2)


class FakeLiveSD:
    def __init__(self, calendar, spot=SPOT):
        self.cal = calendar
        self.spot = spot
        self.strikes = [24000.0 + 50 * i for i in range(0, 60)]

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        # index series (NIFTY 50) at a flat spot — used for strike selection + settlement
        df = pd.DataFrame({"date": self.cal, "close": [self.spot] * len(self.cal)})
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)

    def get_option_chain(self, underlying, on_date, expiry=None):
        rows = [dict(trade_date=on_date, symbol="NIFTY", expiry_date=e, strike_price=k,
                     option_type=r, close=_prem(k, (e - on_date).days, self.spot, r),
                     settle_price=_prem(k, (e - on_date).days, self.spot, r), open_interest=1000)
                for e in EXPIRIES if e >= on_date for k in self.strikes for r in ("CE", "PE")]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        rows = [{"trade_date": d,
                 "close": _prem(float(strike), (expiry - d).days, self.spot, option_type.upper())}
                for d in self.cal if d <= expiry
                if (start_date is None or d >= start_date) and (end_date is None or d <= end_date)]
        return pd.DataFrame(rows)


def _session(sd, now):
    mv, _chain, settler, margin = build_live_options_run(sd, "NIFTY", now=now)
    strat = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    return LiveSession(strat, initial_capital=1_000_000, market_view=mv, settler=settler,
                       margin_model=margin, charge_model=ChargeModel()), mv, strat


def test_live_entry_gated_to_entry_time_then_enters_132():
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 30))  # Monday 09:30
    # Before 09:45 → no entry.
    assert sess.run_decision(datetime(2026, 1, 5, 9, 30)) == []
    assert not strat.legs
    # 09:50 → enters the 1-3-2 (cache-fallback marks fill the legs).
    events = sess.run_decision(datetime(2026, 1, 5, 9, 50))
    shorts = [e for e in events if e["action"] == "SHORT"]
    buys = [e for e in events if e["action"] == "BUY"]
    assert len(shorts) == 1 and len(buys) == 2
    assert shorts[0]["ticker"].split("|")[1] == "2026-01-13"  # ~8-DTE weekly
    assert shorts[0]["units"] == 195 and shorts[0]["ticker"].split("|")[2] == "25400"


def test_live_profit_books_on_15min_cadence():
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 50))
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    flat = {leg["symbol"]: leg["entry"] for leg in strat.legs}     # no P&L move
    # Big profit on every leg (shorts' premium collapses, longs' spikes) so the MTM clearly
    # clears the profit target — which is now 1% of the (larger) deployed margin, not the config.
    deep = {
        leg["symbol"]: (flat[leg["symbol"]] * 0.01 if leg["dir"] == -1 else flat[leg["symbol"]] * 3.0)
        for leg in strat.legs
    }
    # 10:00 — first profit check (records the boundary), body flat → no book.
    sess.update_quotes(flat)
    assert sess.run_decision(datetime(2026, 1, 5, 10, 0)) == []
    # 10:05 — deep profit available, but only 5 min since the last check → held.
    sess.update_quotes(deep)
    assert sess.run_decision(datetime(2026, 1, 5, 10, 5)) == []
    # 10:15 — 15 min elapsed → profit cadence due → books.
    sess.update_quotes(deep)
    out = sess.run_decision(datetime(2026, 1, 5, 10, 15))
    assert out and all(e.get("exit_reason") == "target" for e in out if e["action"] in ("COVER", "SELL"))
    assert not strat.legs


def test_live_strike_selection_uses_live_index_spot():
    # Cached index is flat at 25000, but a live tick puts spot at 26000 → strikes must be
    # picked off the LIVE spot (26200/26400/26600), not the stale cache (25200/25400/...).
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 50))
    mv.set_index_spot("NIFTY", 26000.0)
    events = sess.run_decision(datetime(2026, 1, 5, 9, 50))
    shorts = [e for e in events if e["action"] == "SHORT"]
    assert shorts and shorts[0]["ticker"].split("|")[2] == "26400"  # 26000 + 400 OTM


def test_live_snapshot_includes_greeks():
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 50))
    mv.set_index_spot("NIFTY", 25000.0)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    sess.update_quotes({leg["symbol"]: leg["entry"] for leg in strat.legs})
    snap = sess.snapshot()
    # Net greeks present, and every (priceable) leg carries an IV + delta.
    assert snap["net_delta"] is not None and snap["net_iv"] is not None
    assert all("iv" in p and "delta" in p and "pos_delta" in p for p in snap["positions"])


def test_live_snapshot_includes_target_stop_and_realized():
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 50))
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    snap = sess.snapshot()
    # HNI target/stop = ±1% of the DEPLOYED margin (model estimate here; the real broker basket
    # margin when live) — NOT the per-lot-set config estimate or account capital.
    assert snap["margin_used"] and snap["margin_used"] > 0
    assert snap["profit_target_amt"] == pytest.approx(0.01 * snap["margin_used"])
    assert snap["stop_loss_amt"] == pytest.approx(0.01 * snap["margin_used"])
    assert snap["realized_pnl"] == 0.0  # nothing booked yet


def test_live_snapshot_includes_margin_and_net_credit():
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 50))
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter the 1-3-2
    snap = sess.snapshot()
    assert snap["net_credit"] is not None             # net premium of the structure
    assert snap["margin_used"] is not None and snap["margin_used"] > 0  # short legs need margin
    assert snap["margin_source"] == "model"           # no live Zerodha session in this test


def test_live_flatten_closes_all_legs():
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 50))
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter the 1-3-2
    assert strat.legs and sess.portfolio.lot_symbols()
    sess.update_quotes({leg["symbol"]: leg["entry"] for leg in strat.legs})
    events = sess.flatten(datetime(2026, 1, 5, 10, 0))
    # short body buys-to-close (COVER), the two longs sell (SELL)
    assert {e["action"] for e in events} <= {"COVER", "SELL"}
    assert not sess.portfolio.lot_symbols()  # fully flat
    assert not strat.legs                     # strategy adopted the flat book


def test_live_manual_order_close_and_open_adopts_book():
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 50))
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    hedge = "NIFTY|2026-01-13|25600|CE"  # the long hedge leg (spot 25000 + 600)
    assert hedge in {leg["symbol"] for leg in strat.legs}
    sess.update_quotes({leg["symbol"]: leg["entry"] for leg in strat.legs})
    # Close the hedge leg entirely AND open a new long PE leg on the same expiry.
    events = sess.manual_order(
        datetime(2026, 1, 5, 10, 0),
        closes=[{"symbol": hedge}],
        opens=[{"right": "PE", "strike": 24800, "lots": 1, "side": "buy"}],
    )
    assert events
    book = set(sess.portfolio.lot_symbols())
    assert hedge not in book                          # closed
    assert "NIFTY|2026-01-13|24800|PE" in book        # opened (1 lot-set = 65 units)
    # "Strategy adopts the book": its tracked legs now mirror exactly what's held.
    assert {leg["symbol"] for leg in strat.legs} == book


def test_live_expiry_settles_legs():
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sd = FakeLiveSD(cal)
    sess, mv, strat = _session(sd, datetime(2026, 1, 5, 9, 50))
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    assert strat.legs
    # Jump to expiry day — the settler realizes the contracts to intrinsic (flat spot →
    # all OTM → ~0), and the strategy sees a flat book.
    settle_events = sess.run_decision(datetime(2026, 1, 13, 15, 20))
    assert any(e["action"] == "SETTLE" for e in settle_events)
    assert not any(strat.legs)


def test_prefetch_batches_quotes_and_caches_chain():
    """The basket-entry latency fix: prefetch_quotes pulls many contracts in ONE quote-source call
    (then close() serves them from cache), and live_chain is cached per (underlying, expiry)."""
    from skas_algo.engine.live_options_market import LiveOptionsMarketView

    mv = LiveOptionsMarketView(chain=None, current_datetime=datetime(2026, 1, 5, 9, 50))
    calls = {"quotes": [], "chains": 0}
    mv.set_quote_fn(lambda syms: (calls["quotes"].append(list(syms)) or {s: 10.0 for s in syms}))

    def _chain(_u, _e):
        calls["chains"] += 1
        return {"spot": 1000.0, "rows": [{"strike": 1000, "ce": {"bid": 9.0, "ask": 11.0}, "pe": {}}]}

    mv.set_chain_fn(_chain)

    legs = ["AAA|2026-01-13|1000|CE", "AAA|2026-01-13|990|PE", "BBB|2026-01-13|500|CE"]
    mv.prefetch_quotes(legs)
    assert len(calls["quotes"]) == 1 and set(calls["quotes"][0]) == set(legs)  # ONE batched call
    assert mv.close(legs[0]) == 10.0 and len(calls["quotes"]) == 1             # served from cache

    mv.live_chain("AAA", "2026-01-13")
    mv.live_chain("AAA", "2026-01-13")
    assert calls["chains"] == 1                                                # chain cached, not refetched
