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
from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.instrument import make as make_option
from skas_algo.engine.options.instrument import parse as parse_option
from skas_algo.engine.overrides import (
    BuyLot,
    CloseLot,
    ClosePosition,
    CloseShort,
    OpenShort,
    OverrideResolver,
    OverrideRule,
)
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
        market_view=None,
        settler=None,
        charge_model=None,
        margin_model=None,
    ):
        self.strategy = strategy
        self.lookback = lookback
        self.tax_rate = tax_rate
        self.withdrawal_rate = withdrawal_rate

        self.portfolio = Portfolio(cash=initial_capital)
        self.stops = StopBook()
        # Options runs pass a prebuilt LiveOptionsMarketView (+ settler/charge/margin);
        # equity runs default to the Donchian LiveMarketView → byte-identical path.
        self.market = market_view or LiveMarketView(lookback)
        self.settler = settler
        self.margin_model = margin_model
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
        self.executor = SliceExecutor(
            self.portfolio, self.stops, self.resolver, self.broker, charge_model=charge_model
        )

        self.transactions: list[dict] = []
        self.history: list[dict] = []
        self.monthly_flush_log: dict = {}
        self._current_month: tuple[int, int] | None = None

    # --------------------------------------------------------- lifecycle
    def warmup(self, history_by_symbol: dict[str, list[float]]) -> None:
        """Seed historical closes (chronological, up to yesterday) per symbol.

        Pass the universe in order; symbols with no history yet get an empty list so
        the view's symbol order is established for deterministic iteration. Options runs
        (no Donchian seed) skip this — the chain view supplies everything.
        """
        if not hasattr(self.market, "seed"):
            return
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

    # ----------------------------------------------- manual intervention
    def flatten(self, ts: date | datetime, *, tag: str = "MANUAL", reason: str = "manual") -> list[dict]:
        """Close every open position now (exit-all). Short legs buy-to-close, long legs
        sell. Afterwards the strategy adopts the now-flat book (so it won't try to manage
        legs that no longer exist)."""
        actions: list = []
        for symbol in list(self.portfolio.lot_symbols()):
            lots = self.portfolio.lots(symbol)
            if not lots:
                continue
            if all(lot.direction == -1 for lot in lots):
                actions.extend(CloseShort(symbol, lot.id, tag=tag, reason=reason) for lot in lots)
            else:
                actions.append(ClosePosition(symbol, tag=tag, reason=reason))
        events = self.executor.execute_actions(ts, actions)
        self.transactions.extend(events)
        self.sync_strategy_book(ts)
        self._record_history(ts)
        return events

    def manual_order(self, ts: date | datetime, *, closes=None, opens=None,
                     tag: str = "MANUAL") -> list[dict]:
        """Close selected legs/lots and/or open new legs immediately, at live prices.

        ``closes``: [{"symbol", "lots"?}] — close ``lots`` lot-records (None = all).
        ``opens``:  [{"right", "strike", "lots", "side"}] — new legs on the strategy's
        current expiry. Afterwards the strategy adopts the resulting book.
        """
        actions: list = []
        for c in closes or []:
            symbol = c["symbol"]
            held = self.portfolio.lots(symbol)
            if not held:
                continue
            n = c.get("lots")
            chosen = held if n is None else held[: max(0, int(n))]
            for lot in chosen:
                if lot.direction == -1:
                    actions.append(CloseShort(symbol, lot.id, tag=tag, reason="manual"))
                else:
                    actions.append(CloseLot(symbol, lot.id, lot.units, tag=tag))
        for o in opens or []:
            symbol, units = self._build_manual_leg(o)
            side = str(o.get("side", "")).lower()
            if side in ("sell", "short"):
                actions.append(OpenShort(symbol, units, int(o.get("multiplier", 1))))
            elif side in ("buy", "long"):
                actions.append(BuyLot(symbol, units, tag=tag))
            else:
                raise ValueError(f"manual open side must be buy/sell, got {o.get('side')!r}")
        if not actions:
            raise ValueError("no manual actions to apply")
        events = self.executor.execute_actions(ts, actions)
        self.transactions.extend(events)
        self.sync_strategy_book(ts)
        self._record_history(ts)
        return events

    def _build_manual_leg(self, o: dict) -> tuple[str, int]:
        """Resolve a manual-open spec to an (option_symbol, units) pair."""
        strat = self.strategy
        underlying = getattr(strat, "underlying", None)
        if underlying is None:
            raise ValueError("manual legs require an options strategy")
        expiry = getattr(strat, "entry_expiry", None) or self._default_expiry()
        if expiry is None:
            raise ValueError("could not resolve an expiry for the manual leg")
        lots = int(o["lots"])
        if lots <= 0:
            raise ValueError("manual open lots must be > 0")
        inst = make_option(
            underlying, expiry, float(o["strike"]), str(o["right"]).upper(),
            lot_overrides=getattr(strat, "lot_overrides", None),
        )
        return inst.symbol, lots * inst.lot_size

    def _default_expiry(self):
        """Fallback expiry for a manual leg when the strategy is flat — reuse the
        strategy's own expiry selection against the live chain."""
        chain = getattr(self.market, "chain", None)
        today = getattr(self.market, "current_date", None)
        select = getattr(self.strategy, "_select_expiry", None)
        if chain is not None and today is not None and select is not None:
            try:
                return select(chain, today)
            except Exception:  # pragma: no cover - thin/odd chain → caller raises
                return None
        return None

    def sync_strategy_book(self, ts: date | datetime) -> None:
        """Rebuild an options strategy's tracked legs from the live book, so after a manual
        change it keeps managing exactly what's held ("strategy adopts the book"). No-op for
        strategies that don't track ``legs`` (e.g. equity SST)."""
        strat = self.strategy
        if not hasattr(strat, "legs"):
            return
        legs: list[dict] = []
        for symbol in self.portfolio.lot_symbols():
            lots = self.portfolio.lots(symbol)
            if not lots:
                continue
            units = sum(lot.units for lot in lots)
            cost = sum(lot.units * lot.price for lot in lots)
            legs.append({
                "symbol": symbol,
                "dir": lots[0].direction,  # all lots of a symbol share direction
                "units": units,
                "entry": cost / units if units else 0.0,
            })
        strat.legs = legs
        if not legs:
            if hasattr(strat, "_flat"):
                strat._flat()  # clears entry_expiry/date so a flat book reads as flat
            return
        # Keep entry bookkeeping coherent so _manage()/_time_exit() still work.
        if getattr(strat, "entry_expiry", None) is None:
            inst = parse_option(legs[0]["symbol"])
            if inst is not None:
                strat.entry_expiry = inst.expiry
        if getattr(strat, "entry_date", None) is None:
            strat.entry_date = ts.date() if isinstance(ts, datetime) else ts

    def run_decision(self, ts: date | datetime) -> list[dict]:
        """One decision cycle: cursor, expiry settlement, month flush, stops, strategy."""
        # Advance the options cursor so ctx.now()/today() (and the exit cadences) see ``ts``.
        if hasattr(self.market, "set_now") and isinstance(ts, datetime):
            self.market.set_now(ts)

        this_month = (ts.year, ts.month)
        if self._current_month is not None and this_month != self._current_month:
            self._flush(self._current_month, ts)
        self._current_month = this_month

        events: list[dict] = []
        # Settle expired option contracts first so the strategy sees a flat book and can
        # re-enter (mirrors the backtest runner). No-op (settler=None) for equity runs.
        if self.settler is not None:
            events.extend(self.executor.settle_expiries(ts, self.settler))
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

        Market history is NOT persisted — it's re-warmed from the cache on recovery. Executed
        trades ARE persisted (capped) so a CLOSED cycle keeps its realized P&L + trade log across
        restarts (otherwise the live card goes blank after the position exits).
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
            "transactions": [self._ser_txn(t) for t in self.transactions[-5000:]],
        }

    @staticmethod
    def _ser_txn(t: dict) -> dict:
        out = dict(t)
        d = out.get("date")
        if hasattr(d, "isoformat"):
            out["date"] = d.isoformat()
        return out

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
        # Restore executed trades, reviving the date back to a date object (downstream
        # serialization/report code calls .strftime on it).
        self.transactions = [self._rev_txn(t) for t in state.get("transactions", [])]

    @staticmethod
    def _rev_txn(t: dict) -> dict:
        out = dict(t)
        d = out.get("date")
        if isinstance(d, str):
            try:
                out["date"] = date.fromisoformat(d[:10])
            except ValueError:
                pass
        return out

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
            direction = lots[0].direction if lots else 1  # all lots of a symbol share it
            value = units * ltp if ltp is not None else cost
            positions.append(
                {
                    "symbol": symbol,
                    "units": units,
                    "lots": len(lots),
                    "direction": direction,  # +1 long / −1 short — for the payoff diagram
                    "avg_price": cost / units if units else 0.0,
                    "ltp": ltp,
                    # Short legs profit when the mark falls: sign the unrealized P&L.
                    "unrealized_pnl": direction * (value - cost),
                }
            )
        net_delta, net_iv = self._enrich_greeks(positions)
        holdings = self.portfolio.holdings_value(closes)
        symbols_held = self.portfolio.lot_symbols()
        # Net premium collected at entry (+credit for shorts, −debit for longs).
        net_credit = sum(-p["direction"] * p["avg_price"] * p["units"] for p in positions)
        # Realized (booked) P&L across all trades so far — includes anything a backtest
        # seed already booked during the replay (so a seeded-then-flat run isn't blank).
        realized_pnl = sum((t.get("profit") or 0.0) for t in self.transactions)
        target_amt, stop_amt = self._exit_amounts()
        return {
            "cash": self.portfolio.cash,
            "holdings_value": holdings,
            "equity": self.portfolio.cash + holdings,
            "invested": self.portfolio.invested_capital(),
            "open_positions": len(symbols_held),
            "open_lots": sum(len(self.portfolio.lots(s)) for s in symbols_held),
            "realized_taxes": self.portfolio.total_taxes,
            "positions": positions,
            # Options greeks (derived from live LTP + index spot + DTE); None for equity.
            "net_delta": net_delta,
            "net_iv": net_iv,
            # Margin estimate from the built-in SPAN+exposure model (live runs override
            # this with the real Zerodha basket margin when a session is active).
            "margin_used": self._model_margin(),
            "margin_source": "model" if self.margin_model is not None else None,
            "net_credit": net_credit if positions else None,
            "realized_pnl": realized_pnl,
            # Rupee profit-target / stop-loss the strategy will act on (so the live UI can
            # show "Target +₹X / Stop −₹Y"). None for strategies without %-based exits.
            "profit_target_amt": target_amt,
            "stop_loss_amt": stop_amt,
        }

    def _exit_amounts(self) -> tuple[float | None, float | None]:
        strat = self.strategy
        pt = getattr(strat, "profit_target_pct", None)
        sl = getattr(strat, "stop_loss_pct", None)
        if pt is None and sl is None:
            return None, None
        base_fn = getattr(strat, "_risk_base", None)
        try:
            base = base_fn() if base_fn else getattr(strat, "initial_capital", None)
        except Exception:  # pragma: no cover
            base = getattr(strat, "initial_capital", None)
        if not base:
            return None, None
        return (pt * base if pt is not None else None), (sl * base if sl is not None else None)

    def _model_margin(self) -> float | None:
        if self.margin_model is None:
            return None
        on_date = getattr(self.market, "current_date", None) or date.today()
        try:
            return self.margin_model.margin_used(self.portfolio, on_date)
        except Exception:  # pragma: no cover - spot provider gap → no estimate
            return None

    def _enrich_greeks(self, positions: list[dict]) -> tuple[float | None, float | None]:
        """Attach per-leg IV/delta to option positions and return (net_delta, net_iv).

        Greeks are backed out of the live mark (LTP) using the live index spot and the
        contract's days-to-expiry — the same Black-Scholes inversion Sensibull uses (Kite
        exposes no greeks field). No-op for equity runs (no index spot) → returns (None,None).
        """
        market = self.market
        if not hasattr(market, "index_spot"):
            return None, None  # equity run
        underlying = getattr(self.strategy, "underlying", None)
        spot = market.index_spot(underlying) if underlying else None
        if spot is None:
            return None, None
        r = float(getattr(self.strategy, "r", 0.065))
        today = getattr(market, "current_date", None) or date.today()
        net_delta = 0.0
        iv_num = iv_den = 0.0
        have = False
        for p in positions:
            inst = parse_option(p["symbol"])
            ltp = p.get("ltp")
            if inst is None or ltp is None or ltp <= 0:
                continue
            t = max((inst.expiry - today).days, 0) / 365.0
            if t <= 0:
                continue
            iv = bs.implied_vol(ltp, spot, inst.strike, t, r, inst.right)
            if iv is None:
                continue
            d = bs.delta(spot, inst.strike, t, r, iv, inst.right)
            pos_delta = p["direction"] * d * p["units"]
            p["iv"] = iv
            p["delta"] = d
            p["pos_delta"] = pos_delta
            net_delta += pos_delta
            iv_num += iv * p["units"]
            iv_den += p["units"]
            have = True
        if not have:
            return None, None
        return net_delta, (iv_num / iv_den if iv_den else None)

    def watchlist(self) -> list[dict]:
        """Per-symbol decision context: price, 20-day levels, tracking, holding, status.

        Lets you see what the algo is 'thinking' for every name in the universe —
        which it's tracking (waiting for a breakout), holding, or just watching.
        """
        if not hasattr(self.market, "universe"):
            return []  # options runs have no fixed Donchian universe to introspect
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
