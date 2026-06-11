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
        excluded_symbols: list[str] | None = None,
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
        self.resolver = OverrideResolver(overrides, excluded=set(excluded_symbols or []))
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

    # --------------------------------------------------------- exclusions
    @property
    def excluded_symbols(self) -> list[str]:
        return sorted(self.resolver.excluded)

    def set_excluded(self, symbols: list[str]) -> None:
        """Replace the no-new-entry blocklist (open positions keep being managed)."""
        self.resolver.excluded = {s.strip().upper() for s in symbols if s.strip()}

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

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        """Full session state so a running run can be rebuilt after a restart.

        Market history is NOT persisted — it's re-warmed from the cache on recovery.
        """
        return {
            "portfolio": self.portfolio.export_state(),
            "stops": self.stops.export(),
            "strategy": (
                self.strategy.export_state() if hasattr(self.strategy, "export_state") else {}
            ),
            "overrides": [
                {"scope": o.scope, "target": o.target, "rule": o.rule, "active": o.active}
                for o in self.resolver.overrides
            ],
            "current_month": list(self._current_month) if self._current_month else None,
        }

    def load_state(self, state: dict) -> None:
        from skas_algo.engine.overrides import OverrideRule

        self.portfolio.load_state(state["portfolio"])
        self.stops.load(state.get("stops", []))
        if hasattr(self.strategy, "load_state"):
            self.strategy.load_state(state.get("strategy", {}))
        self.resolver.overrides = [
            OverrideRule(
                scope=o["scope"], target=o["target"], rule=o["rule"], active=o.get("active", True)
            )
            for o in state.get("overrides", [])
        ]
        cm = state.get("current_month")
        self._current_month = tuple(cm) if cm else None

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
                    "lots": len(lots),
                    "avg_price": cost / units if units else 0.0,
                    "ltp": ltp,
                    "unrealized_pnl": value - cost,
                }
            )
        holdings = self.portfolio.holdings_value(closes)
        symbols_held = self.portfolio.lot_symbols()
        return {
            "cash": self.portfolio.cash,
            "holdings_value": holdings,
            "equity": self.portfolio.cash + holdings,
            "invested": self.portfolio.invested_capital(),
            "open_positions": len(symbols_held),
            "open_lots": sum(len(self.portfolio.lots(s)) for s in symbols_held),
            "realized_taxes": self.portfolio.total_taxes,
            "positions": positions,
        }

    def watchlist(self) -> list[dict]:
        """Per-symbol decision context: price, 20-day levels, tracking, holding, status.

        Lets you see what the algo is 'thinking' for every name in the universe —
        which it's tracking (waiting for a breakout), holding, or just watching.
        """
        tracking = getattr(self.strategy, "tracking", {})
        excluded = self.resolver.excluded
        rows: list[dict] = []
        for sym in self.market.universe():
            ltp = self.market.last_close(sym)
            levels = self.market.levels(sym)
            high = levels[0] if levels else None
            low = levels[1] if levels else None
            lots = self.portfolio.lots(sym)
            units = sum(lot.units for lot in lots)
            cost = sum(lot.units * lot.price for lot in lots)
            avg = cost / units if units else None
            held = bool(lots)
            upnl = (units * ltp - cost) if (held and ltp is not None) else 0.0
            pnl_pct = ((ltp - avg) / avg * 100) if (held and ltp and avg) else None
            is_tracking = bool(tracking.get(sym, False))

            is_excluded = sym in excluded
            # Would the next decision act on this name? (breakout buy / target sell)
            signal = ""
            if held and ltp is not None and self._would_exit(lots, ltp, avg):
                signal = "SELL"
            elif is_tracking and high is not None and ltp is not None and ltp > high:
                # Excluded names won't be bought, even on a breakout.
                signal = "" if is_excluded else "BUY"

            if is_excluded and not held:
                status = "Excluded — no new entries"
            elif held:
                status = f"Holding {len(lots)} lot(s)" + (" · excluded" if is_excluded else "")
            elif is_tracking and high is not None:
                status = "Tracking → buy on breakout"
            elif ltp is not None and low is not None and ltp <= low:
                status = "At 20-day low"
            elif ltp is not None and high is not None and ltp > high:
                # Above the 20-day high but never made a recent 20-day low -> not a
                # buy (SST only buys a breakout on a stock it was already tracking).
                status = "Above 20d high (not tracking)"
            else:
                status = "Watching"

            to_breakout = ((high - ltp) / ltp * 100) if (high and ltp) else None
            rows.append(
                {
                    "symbol": sym,
                    "ltp": ltp,
                    "high_20d": high,
                    "low_20d": low,
                    "tracking": is_tracking,
                    "excluded": is_excluded,
                    "held": held,
                    "lots": len(lots),
                    "units": units,
                    "avg_price": avg,
                    "unrealized_pnl": upnl,
                    "pnl_pct": pnl_pct,
                    "to_breakout_pct": to_breakout,
                    "signal": signal,
                    "status": status,
                }
            )
        return rows

    def _would_exit(self, lots, ltp: float, avg: float | None) -> bool:
        """Would the held position exit on the next decision (per-lot or pooled target)?"""
        prof = getattr(self.strategy, "profit_target", None)
        if prof is not None:  # SST-LIFO: any lot up >= its target
            return any((ltp - lot.price) / lot.price >= prof for lot in lots)
        target_fn = getattr(self.strategy, "_target", None)  # SST-FIFO: avg vs tiered target
        if target_fn is not None and avg:
            return (ltp - avg) / avg >= target_fn(len(lots))
        return False

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
