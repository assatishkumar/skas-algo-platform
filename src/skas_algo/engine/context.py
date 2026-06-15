"""AlgoContext — the handle a strategy uses to observe the world.

It exposes the portfolio (cash, lots), the market view (today's close + rolling
levels), and run parameters. The same context type is used in every mode, so a
strategy written against it runs unchanged in BACKTEST, PAPER, and LIVE.
"""

from __future__ import annotations

from typing import Any

from .market import MarketLike
from .portfolio import Lot, Portfolio
from .stops import StopBook


class AlgoContext:
    def __init__(
        self,
        algo_id: int | None,
        params: dict[str, Any],
        portfolio: Portfolio,
        market: MarketLike,
        stops: StopBook | None = None,
    ):
        self.algo_id = algo_id
        self.params = params
        self.portfolio = portfolio
        self.market = market
        self.stops = stops or StopBook()

    # ----- portfolio -----
    @property
    def cash(self) -> float:
        return self.portfolio.cash

    def equity(self) -> float:
        """Total mark-to-market equity: cash + holdings at last-known closes."""
        return self.portfolio.cash + self.portfolio.holdings_value(self.market.mark_prices())

    def lots(self, symbol: str) -> list[Lot]:
        """Lots the strategy may act on — excludes lots under a managed stop.

        A lot with an attached trailing/hard stop is controlled by the engine, so
        the strategy no longer sees it (it won't double-exit the "trailed" remainder).
        """
        managed = self.stops.managed_lot_ids()
        return [lot for lot in self.portfolio.lots(symbol) if lot.id not in managed]

    def lot_symbols(self) -> list[str]:
        managed = self.stops.managed_lot_ids()
        return [
            s
            for s in self.portfolio.lot_symbols()
            if any(lot.id not in managed for lot in self.portfolio.lots(s))
        ]

    # ----- market (today) -----
    def present_symbols(self) -> list[str]:
        return self.market.present_symbols()

    def close(self, symbol: str) -> float:
        return self.market.close(symbol)

    def rolling_high(self, symbol: str) -> float:
        return self.market.rolling_high(symbol)

    def rolling_low(self, symbol: str) -> float:
        return self.market.rolling_low(symbol)

    # ----- options (no-ops / None for non-options runs) -----
    def today(self):
        """Current trading date (a ``datetime.date``). Used by options strategies."""
        cur = getattr(self.market, "current_date", None)
        if cur is not None:
            return cur.date() if hasattr(cur, "date") else cur
        from datetime import date as _date
        return _date.today()

    def now(self):
        """Current timestamp (a ``datetime.datetime``). Live: the market view's real-time
        cursor; backtest: the bar date at end-of-session (15:30 IST) so intraday exit
        cadences collapse to one evaluation per daily bar."""
        from datetime import datetime as _dt, time as _time
        cur = getattr(self.market, "current_datetime", None)
        if cur is not None:
            return cur
        return _dt.combine(self.today(), _time(15, 30))

    def option_chain(self):
        """The OptionChainView for this run, or None for equity runs."""
        return getattr(self.market, "chain", None)
