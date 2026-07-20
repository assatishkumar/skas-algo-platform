"""The durable Order trail must record a leg's true broker side so it can be paired back
into opens/closes. A short-OPEN's action is "SHORT" (not "SELL") — mapping only "SELL"→SELL
stored every short as BUY, and the FIFO reconstruction (_orders_to_trades) then couldn't
detect a single close (delta_neutral run #203, 2026-07: every leg looked open forever)."""

from __future__ import annotations

from skas_algo.api.routes.live import _orders_to_trades
from skas_algo.db.base import session_scope
from skas_algo.db.models import Algo, InstrumentClass, Order, OrderSide, TradingMode
from skas_algo.live.persistence import record_trades

SYM = "BANKNIFTY|2026-07-28|56900|CE"


def _algo(db) -> int:
    a = Algo(name="side-test", strategy_id="delta_neutral_monthly",
             instrument_class=InstrumentClass.DERIV, mode=TradingMode.PAPER, capital=1_000_000)
    db.add(a)
    db.flush()
    return a.id


def test_short_open_records_sell_and_reconstructs():
    with session_scope() as db:
        algo_id = _algo(db)
        # A short strangle-style open (action SHORT) then a cover (action COVER).
        record_trades(db, algo_id, [
            {"ticker": SYM, "action": "SHORT", "units": 175, "price": 893.4, "tag": "dnm_entry"},
            {"ticker": SYM, "action": "COVER", "units": 175, "price": 200.0, "tag": "target"},
        ])
        db.flush()
        orders = db.query(Order).filter(Order.algo_id == algo_id).order_by(Order.id).all()
        assert [o.side for o in orders] == [OrderSide.SELL, OrderSide.BUY]   # SHORT→SELL, COVER→BUY

        trades = _orders_to_trades(orders)
        # the pair reconstructs to a SHORT open + a COVER close (not two opens)
        assert [t["action"] for t in trades] == ["SHORT", "COVER"]
        # short P&L is directional: sold 893.4, covered 200 → profit
        assert trades[1]["profit"] == (893.4 - 200.0) * 175


def test_long_open_records_buy():
    with session_scope() as db:
        algo_id = _algo(db)
        # A long hedge (BUY open) then sell-to-close (SELL).
        record_trades(db, algo_id, [
            {"ticker": SYM, "action": "BUY", "units": 175, "price": 190.3, "tag": "dnm_ironfly"},
            {"ticker": SYM, "action": "SELL", "units": 175, "price": 4.5, "tag": "target"},
        ])
        db.flush()
        orders = db.query(Order).filter(Order.algo_id == algo_id).order_by(Order.id).all()
        assert [o.side for o in orders] == [OrderSide.BUY, OrderSide.SELL]
        trades = _orders_to_trades(orders)
        assert [t["action"] for t in trades] == ["BUY", "SELL"]
        assert trades[1]["profit"] == (4.5 - 190.3) * 175    # long P&L
