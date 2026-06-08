"""LiveSession — the real-time (PAPER/LIVE) driver.

Same engine, real-time mode: it reuses the shared SliceExecutor, OverrideResolver,
Portfolio and StopBook, but reads a LiveMarketView fed by quotes and fills through a
live-priced broker (PaperBroker for forward-test). It is *driveable* — warmup, then
``update_quotes`` + ``run_decision`` + ``end_day`` are called by an external loop
(the async session manager) or, in tests, by a replay harness. No DB/async here, so
it stays pure and testable.

Because the decision/execution path is the exact same SliceExecutor the backtest
uses, replaying history through a LiveSession reproduces the backtest trade-for-trade
(see tests/test_mode_equivalence.py).
"""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.brokers.sim_broker import PaperBroker
from skas_algo.engine.context import AlgoContext
from skas_algo.engine.execution import SliceExecutor
from skas_algo.engine.live_market import LiveMarketView
from skas_algo.engine.overrides import OverrideResolver, OverrideRule
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.sim_fill import FillModel
from skas_algo.engine.stops import StopBook


class LiveSession:
    def __init__(
        self,
        strategy,
        *,
        initial_capital: float = 2_500_000,
        lookback: int = 20,
        tax_rate: float = 0.20,
        withdrawal_rate: float = 0.0,
        overrides: list[OverrideRule] | None = None,
        fill_model: FillModel | None = None,
        broker=None,
        algo_id: int | None = None,
    ):
        self.strategy = strategy
        self.lookback = lookback
        self.tax_rate = tax_rate
        self.withdrawal_rate = withdrawal_rate

        self.portfolio = Portfolio(cash=initial_capital)
        self.stops = StopBook()
        self.market = LiveMarketView(lookback)
        # PAPER: simulated fills on live prices. LIVE (later): a ZerodhaAdapter passed in.
        self.broker = broker or PaperBroker(
            price_fn=self.market.close, fill_model=fill_model or FillModel()
        )
        self.resolver = OverrideResolver(overrides)
        self.ctx = AlgoContext(
            algo_id=algo_id,
            params={},
            portfolio=self.portfolio,
            market=self.market,
            stops=self.stops,
        )
        self.executor = SliceExecutor(self.portfolio, self.stops, self.resolver, self.broker)

        self.transactions: list[dict] = []
        self.history: list[dict] = []
        self.monthly_flush_log: dict = {}
        self._current_month: tuple[int, int] | None = None

    # --------------------------------------------------------- lifecycle
    def warmup(self, history_by_symbol: dict[str, list[float]]) -> None:
        """Seed historical closes (chronological, up to yesterday) per symbol.

        Pass the universe in order; symbols with no history yet get an empty list so
        the view's symbol order is established for deterministic iteration.
        """
        for symbol, closes in history_by_symbol.items():
            self.market.seed(symbol, closes)

    def update_quotes(self, quotes: dict[str, float]) -> None:
        for symbol, price in quotes.items():
            self.market.update_quote(symbol, price)

    def run_decision(self, ts: date | datetime) -> list[dict]:
        """One decision cycle: month flush, managed stops, strategy + overrides."""
        this_month = (ts.year, ts.month)
        if self._current_month is not None and this_month != self._current_month:
            self._flush(self._current_month, ts)
        self._current_month = this_month

        events: list[dict] = []
        events.extend(self.executor.check_stops(ts, self.market.closes_today()))
        events.extend(self.executor.decide_and_execute(ts, self.strategy, self.ctx))
        self.transactions.extend(events)
        self._record_history(ts)
        return events

    def end_day(self) -> None:
        """Advance the view: today's quotes become history for tomorrow's levels."""
        self.market.roll_forward()

    def finalize(self, ts: date | datetime) -> None:
        if self._current_month is not None:
            self._flush(self._current_month, ts)

    # ----------------------------------------------------------- views
    def snapshot(self) -> dict:
        """Current positions + cash + mark-to-market equity (for broadcast/persist)."""
        closes = self.market.mark_prices()
        positions = []
        for symbol in self.portfolio.lot_symbols():
            lots = self.portfolio.lots(symbol)
            units = sum(lot.units for lot in lots)
            cost = sum(lot.units * lot.price for lot in lots)
            ltp = closes.get(symbol)
            value = units * ltp if ltp is not None else cost
            positions.append(
                {
                    "symbol": symbol,
                    "units": units,
                    "avg_price": cost / units if units else 0.0,
                    "ltp": ltp,
                    "unrealized_pnl": value - cost,
                }
            )
        holdings = self.portfolio.holdings_value(closes)
        return {
            "cash": self.portfolio.cash,
            "holdings_value": holdings,
            "equity": self.portfolio.cash + holdings,
            "realized_taxes": self.portfolio.total_taxes,
            "positions": positions,
        }

    # ------------------------------------------------------- bookkeeping
    def _flush(self, ym, ts) -> None:
        flush = self.portfolio.flush_month(self.tax_rate, self.withdrawal_rate)
        if flush is not None:
            self.monthly_flush_log[ym] = {
                "tax": flush.tax,
                "withdrawal": flush.withdrawal,
                "date": ts,
            }

    def _record_history(self, ts) -> None:
        closes = self.market.mark_prices()
        holdings = self.portfolio.holdings_value(closes)
        self.history.append(
            {
                "date": ts,
                "cash": self.portfolio.cash,
                "holdings_value": holdings,
                "invested_capital": self.portfolio.invested_capital(),
                "total_equity": self.portfolio.cash + holdings,
            }
        )
