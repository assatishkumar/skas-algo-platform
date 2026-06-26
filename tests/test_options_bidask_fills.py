"""Options liquidity: screener premium uses the bid; live fills SELL@bid / BUY@ask."""

from __future__ import annotations

from datetime import date

from skas_algo.brokers.base import BrokerOrder
from skas_algo.brokers.sim_broker import PaperBroker
from skas_algo.db.enums import OrderSide
from skas_algo.engine.live_options_market import LiveOptionsMarketView
from skas_algo.engine.options.instrument import make
from skas_algo.services.donchian_strangle import _leg


# ── screener: premium = bid, not the (possibly stale) last-traded price ───────────────────
def test_leg_premium_uses_bid_not_ltp():
    rows = [{"strike": 3400.0, "ce": {"ltp": 913.6, "bid": 79.0, "ask": 82.0, "oi": 1000}, "pe": None}]
    leg = _leg(rows, 3400, "CE")
    assert leg["premium"] == 79.0 and leg["liquid"] is True  # bid, tight spread → liquid


def test_leg_illiquid_wide_spread_flagged():
    rows = [{"strike": 3400.0, "ce": {"ltp": 100.0, "bid": 10.0, "ask": 90.0, "oi": 5}, "pe": None}]
    leg = _leg(rows, 3400, "CE")
    assert leg["premium"] == 10.0 and leg["liquid"] is False  # bid; 160% spread → illiquid


def test_leg_premium_falls_back_to_ltp_when_one_sided():
    rows = [{"strike": 3400.0, "ce": {"ltp": 50.0, "bid": None, "ask": None, "oi": 0}, "pe": None}]
    leg = _leg(rows, 3400, "CE")
    assert leg["premium"] == 50.0 and leg["liquid"] is False


# ── live execution: SELL fills at the bid, BUY at the ask ─────────────────────────────────
def _view_with_chain(bid, ask):
    sym = make("NIFTY", date(2026, 7, 28), 24000, "CE", lot_size=50).symbol
    mv = LiveOptionsMarketView(object())
    mv.set_chain_fn(lambda u, e: {"rows": [{"strike": 24000.0,
                                            "ce": {"bid": bid, "ask": ask}, "pe": None}]})
    return mv, sym


def test_options_fill_sell_at_bid_buy_at_ask():
    mv, sym = _view_with_chain(100.0, 110.0)
    assert mv.fill_price(sym, OrderSide.SELL) == 100.0  # sell into the bid
    assert mv.fill_price(sym, OrderSide.BUY) == 110.0   # buy at the ask


def test_options_fill_falls_back_to_close_without_book():
    mv, sym = _view_with_chain(None, None)  # one-sided / no quotes
    mv.update_quote(sym, 55.0)
    assert mv.fill_price(sym, OrderSide.SELL) == 55.0   # falls back to LTP/mark


def test_paper_broker_passes_side_to_price_fn():
    b = PaperBroker(price_fn=lambda symbol, side: 100.0 if side is OrderSide.SELL else 110.0)
    assert b.execute(BrokerOrder("X", OrderSide.SELL, 1)).price == 100.0
    assert b.execute(BrokerOrder("X", OrderSide.BUY, 1)).price == 110.0
