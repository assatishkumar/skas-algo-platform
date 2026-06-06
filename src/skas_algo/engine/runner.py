"""The mode-agnostic run loop.

Phase 1 implements the BACKTEST driver (SimulatedClock + HistoricalReplayFeed +
BacktestBroker). The same loop body — month flush, strategy decision, ordered
execution through a broker, bookkeeping — is what PAPER/LIVE will reuse with a
real clock, a live feed, and a live broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from skas_algo.brokers.base import BrokerOrder
from skas_algo.brokers.sim_broker import BacktestBroker
from skas_algo.db.enums import OrderSide
from skas_algo.engine.context import AlgoContext
from skas_algo.engine.market import HistoricalReplayFeed, MarketView, PriceLoader
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.sim_fill import FillModel
from skas_algo.engine.types import SignalAction


class _StrategyLike(Protocol):
    def on_slice(self, ctx: AlgoContext) -> list: ...


@dataclass
class RunResult:
    history: list[dict] = field(default_factory=list)
    transactions: list[dict] = field(default_factory=list)
    monthly_flush_log: dict = field(default_factory=dict)
    portfolio: Portfolio | None = None


class BacktestRunner:
    """Replays a strategy over historical data and records the run."""

    def __init__(
        self,
        strategy: _StrategyLike,
        universe: list[str],
        loader: PriceLoader,
        initial_capital: float = 2_500_000,
        lookback: int = 20,
        tax_rate: float = 0.20,
        withdrawal_rate: float = 0.0,
        fill_model: FillModel | None = None,
        verbose: bool = False,
    ):
        self.strategy = strategy
        self.universe = universe
        self.loader = loader
        self.initial_capital = initial_capital
        self.lookback = lookback
        self.tax_rate = tax_rate
        self.withdrawal_rate = withdrawal_rate
        self.fill_model = fill_model or FillModel()
        self.verbose = verbose

    def run(self, start_date: date, end_date: date) -> RunResult:
        view: MarketView = HistoricalReplayFeed(self.loader, self.lookback).build(
            self.universe, start_date, end_date, verbose=self.verbose
        )
        portfolio = Portfolio(cash=self.initial_capital)
        broker = BacktestBroker(price_fn=view.close, fill_model=self.fill_model)
        ctx = AlgoContext(algo_id=None, params={}, portfolio=portfolio, market=view)

        result = RunResult(portfolio=portfolio)
        current_month: tuple[int, int] | None = None

        for ts in view.unified_dates:
            view.set_date(ts)

            # --- month transition: flush previous month's tax/withdrawal ---
            this_month = (ts.year, ts.month)
            if current_month is not None and this_month != current_month:
                self._flush(portfolio, result, current_month, ts)
            current_month = this_month

            # Snapshot lot counts before execution (for SELL log parity).
            lots_at_start = {s: len(portfolio.lots(s)) for s in portfolio.lot_symbols()}

            # --- strategy decides, engine executes in emitted order ---
            for sig in self.strategy.on_slice(ctx):
                if sig.action is SignalAction.EXIT:
                    self._execute_exit(broker, portfolio, result, ts, sig, lots_at_start)
                elif sig.action is SignalAction.ENTER_LONG:
                    self._execute_entry(broker, portfolio, result, ts, sig)

            self._record_history(portfolio, view, result, ts)

        if current_month is not None and view.unified_dates:
            self._flush(portfolio, result, current_month, view.unified_dates[-1])

        return result

    # ------------------------------------------------------------ execution
    def _execute_exit(self, broker, portfolio, result, ts, sig, lots_at_start) -> None:
        lot = next((x for x in portfolio.lots(sig.symbol) if x.id == sig.lot_id), None)
        if lot is None:
            return
        entry = lot.price
        units = lot.units
        fill = broker.execute(BrokerOrder(sig.symbol, OrderSide.SELL, units))
        profit = portfolio.close_lot(sig.symbol, sig.lot_id, fill.price)
        pnl_pct = (fill.price - entry) / entry if entry else 0.0
        self._record_txn(
            result,
            ts,
            sig.symbol,
            "SELL",
            units,
            fill.price,
            profit,
            pnl_pct,
            lots_at_start.get(sig.symbol, 0),
        )

    def _execute_entry(self, broker, portfolio, result, ts, sig) -> None:
        units = sig.quantity or 0
        if units <= 0:
            return
        label = "BUY" if not portfolio.lots(sig.symbol) else "AVG_BUY"
        fill = broker.execute(BrokerOrder(sig.symbol, OrderSide.BUY, units))
        portfolio.buy(sig.symbol, units, fill.price, ts)
        self._record_txn(
            result,
            ts,
            sig.symbol,
            label,
            units,
            fill.price,
            0.0,
            0.0,
            len(portfolio.lots(sig.symbol)),
        )

    # ----------------------------------------------------------- bookkeeping
    def _flush(self, portfolio, result, ym, ts) -> None:
        flush = portfolio.flush_month(self.tax_rate, self.withdrawal_rate)
        if flush is not None:
            result.monthly_flush_log[ym] = {
                "tax": flush.tax,
                "withdrawal": flush.withdrawal,
                "date": ts,
            }

    def _record_txn(self, result, ts, ticker, action, units, price, profit, pnl_pct, lots) -> None:
        result.transactions.append(
            {
                "date": ts,
                "ticker": ticker,
                "action": action,
                "units": units,
                "price": price,
                "amount": units * price,
                "profit": profit,
                "pnl_pct": pnl_pct,
                "lots": lots,
            }
        )

    def _record_history(self, portfolio, view, result, ts) -> None:
        closes = view.closes_today()
        holdings = portfolio.holdings_value(closes)
        result.history.append(
            {
                "date": ts,
                "cash": portfolio.cash,
                "holdings_value": holdings,
                "invested_capital": portfolio.invested_capital(),
                "total_equity": portfolio.cash + holdings,
            }
        )
