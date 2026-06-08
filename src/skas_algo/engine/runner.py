"""The BACKTEST driver.

Builds the historical market view and replays it day by day, delegating the actual
stop-check / strategy-decision / execution to the shared SliceExecutor (also used by
the live paper/live engine). Month flush and equity-curve recording are backtest-side
bookkeeping kept here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from skas_algo.brokers.sim_broker import BacktestBroker
from skas_algo.engine.context import AlgoContext
from skas_algo.engine.execution import SliceExecutor
from skas_algo.engine.market import HistoricalReplayFeed, MarketView, PriceLoader
from skas_algo.engine.overrides import OverrideResolver, OverrideRule
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.sim_fill import FillModel
from skas_algo.engine.stops import StopBook


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
        overrides: list[OverrideRule] | None = None,
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
        self.resolver = OverrideResolver(overrides)
        self.verbose = verbose

    def run(self, start_date: date, end_date: date) -> RunResult:
        view: MarketView = HistoricalReplayFeed(self.loader, self.lookback).build(
            self.universe, start_date, end_date, verbose=self.verbose
        )
        portfolio = Portfolio(cash=self.initial_capital)
        stops = StopBook()
        broker = BacktestBroker(price_fn=view.close, fill_model=self.fill_model)
        ctx = AlgoContext(algo_id=None, params={}, portfolio=portfolio, market=view, stops=stops)
        executor = SliceExecutor(portfolio, stops, self.resolver, broker)

        result = RunResult(portfolio=portfolio)
        current_month: tuple[int, int] | None = None

        for ts in view.unified_dates:
            view.set_date(ts)

            # --- month transition: flush previous month's tax/withdrawal ---
            this_month = (ts.year, ts.month)
            if current_month is not None and this_month != current_month:
                self._flush(portfolio, result, current_month, ts)
            current_month = this_month

            # --- shared execution path: stops first, then strategy decisions ---
            result.transactions.extend(executor.check_stops(ts, view.closes_today()))
            result.transactions.extend(executor.decide_and_execute(ts, self.strategy, ctx))

            self._record_history(portfolio, view, result, ts)

        if current_month is not None and view.unified_dates:
            self._flush(portfolio, result, current_month, view.unified_dates[-1])

        return result

    # ----------------------------------------------------------- bookkeeping
    def _flush(self, portfolio, result, ym, ts) -> None:
        flush = portfolio.flush_month(self.tax_rate, self.withdrawal_rate)
        if flush is not None:
            result.monthly_flush_log[ym] = {
                "tax": flush.tax,
                "withdrawal": flush.withdrawal,
                "date": ts,
            }

    def _record_history(self, portfolio, view, result, ts) -> None:
        # Mark-to-market on last-known closes (forward-filled) so a held position is
        # never valued at zero on a day it doesn't print (e.g. Muhurat sessions).
        closes = view.mark_prices()
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
