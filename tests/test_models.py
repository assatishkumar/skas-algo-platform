"""Smoke test: domain models persist and relationships resolve."""

from __future__ import annotations

from skas_algo.db.base import session_scope
from skas_algo.db.enums import InstrumentClass, TradingMode
from skas_algo.db.models import Algo, BrokerAccount


def test_create_algo_with_broker_account():
    with session_scope() as s:
        acct = BrokerAccount(broker="zerodha", label="primary")
        algo = Algo(
            name="SST-LIFO paper",
            strategy_id="sst_lifo",
            instrument_class=InstrumentClass.STOCK,
            mode=TradingMode.PAPER,
            capital=2_500_000,
            params={"target": 0.06, "parts": 50, "lookback": 20},
            broker_account=acct,
        )
        s.add(algo)
        s.flush()
        algo_id = algo.id

    with session_scope() as s:
        loaded = s.get(Algo, algo_id)
        assert loaded is not None
        assert loaded.mode == TradingMode.PAPER
        assert loaded.params["target"] == 0.06
        assert loaded.broker_account.broker == "zerodha"
