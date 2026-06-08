"""Shared slice execution — the single code path used by BACKTEST and PAPER/LIVE.

Given a portfolio, stop book, override resolver, and broker, it runs the managed-stop
checks and the strategy decision for one timestamp, executing the resulting actions
through the broker and returning the trade events. The backtest runner appends those
events to its in-memory result; the live session persists them to the DB and
broadcasts — but the *execution logic itself is identical*, which is what makes
"backtest == forward-test == live" hold (see tests/test_mode_equivalence.py).
"""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.brokers.base import BrokerOrder
from skas_algo.db.enums import OrderSide
from skas_algo.engine.context import AlgoContext
from skas_algo.engine.overrides import AttachStop, BuyLot, CloseLot, ClosePosition, OverrideResolver
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.stops import StopBook


def trade_event(ts, ticker, action, units, price, profit, pnl_pct, lots, tag) -> dict:
    return {
        "date": ts,
        "ticker": ticker,
        "action": action,
        "units": units,
        "price": price,
        "amount": units * price,
        "profit": profit,
        "pnl_pct": pnl_pct,
        "lots": lots,
        "tag": tag,
    }


class SliceExecutor:
    """Executes one market slice's stops + strategy decisions through a broker."""

    def __init__(self, portfolio: Portfolio, stops: StopBook, resolver: OverrideResolver, broker):
        self.portfolio = portfolio
        self.stops = stops
        self.resolver = resolver
        self.broker = broker

    def check_stops(self, ts: date | datetime, closes_today: dict[str, float]) -> list[dict]:
        """Evaluate managed (trailing/hard) stops and exit any that trigger."""
        events: list[dict] = []
        for stop in self.stops.evaluate(closes_today):
            lot = self.portfolio.get_lot(stop.symbol, stop.lot_id)
            if lot is None:
                self.stops.remove(stop.lot_id)
                continue
            ev = self._sell(
                ts,
                stop.symbol,
                stop.lot_id,
                lot.units,
                lot.price,
                tag="TRAIL",
                lots=len(self.portfolio.lots(stop.symbol)),
            )
            if ev:
                events.append(ev)
            self.stops.remove(stop.lot_id)
        return events

    def decide_and_execute(self, ts: date | datetime, strategy, ctx: AlgoContext) -> list[dict]:
        """Run the strategy for this slice, resolve overrides, execute in order."""
        lots_at_start = {s: len(self.portfolio.lots(s)) for s in self.portfolio.lot_symbols()}
        events: list[dict] = []
        for action in self.resolver.resolve(strategy.on_slice(ctx), ctx):
            events.extend(self._execute(ts, action, lots_at_start))
        return events

    # ------------------------------------------------------------ internals
    def _execute(self, ts, action, lots_at_start) -> list[dict]:
        if isinstance(action, CloseLot):
            lot = self.portfolio.get_lot(action.symbol, action.lot_id)
            if lot is None:
                return []
            ev = self._sell(
                ts,
                action.symbol,
                action.lot_id,
                action.units,
                lot.price,
                tag=action.tag,
                lots=lots_at_start.get(action.symbol, len(self.portfolio.lots(action.symbol))),
            )
            return [ev] if ev else []
        if isinstance(action, ClosePosition):
            ev = self._close_position(ts, action.symbol, action.tag)
            return [ev] if ev else []
        if isinstance(action, AttachStop):
            self.stops.attach(action.stop)
            return []
        if isinstance(action, BuyLot):
            ev = self._buy(ts, action.symbol, action.units)
            return [ev] if ev else []
        return []

    def _sell(self, ts, symbol, lot_id, units, entry, tag, lots) -> dict | None:
        if units <= 0:
            return None
        fill = self.broker.execute(BrokerOrder(symbol, OrderSide.SELL, units))
        profit = self.portfolio.reduce_lot(symbol, lot_id, units, fill.price)
        pnl_pct = (fill.price - entry) / entry if entry else 0.0
        return trade_event(ts, symbol, "SELL", units, fill.price, profit, pnl_pct, lots, tag)

    def _close_position(self, ts, symbol, tag) -> dict | None:
        lots = self.portfolio.lots(symbol)
        if not lots:
            return None
        total_units = sum(lot.units for lot in lots)
        n_lots = len(lots)
        fill = self.broker.execute(BrokerOrder(symbol, OrderSide.SELL, total_units))
        closed = self.portfolio.close_position(symbol, fill.price)
        if closed is None:
            return None
        _units, total_cost, profit, _n = closed
        avg_cost = total_cost / total_units
        pnl_pct = (fill.price - avg_cost) / avg_cost if avg_cost else 0.0
        return trade_event(
            ts, symbol, "SELL", total_units, fill.price, profit, pnl_pct, n_lots, tag
        )

    def _buy(self, ts, symbol, units) -> dict | None:
        if units <= 0:
            return None
        label = "BUY" if not self.portfolio.lots(symbol) else "AVG_BUY"
        fill = self.broker.execute(BrokerOrder(symbol, OrderSide.BUY, units))
        self.portfolio.buy(symbol, units, fill.price, ts)
        return trade_event(
            ts,
            symbol,
            label,
            units,
            fill.price,
            0.0,
            0.0,
            len(self.portfolio.lots(symbol)),
            "STRATEGY",
        )
