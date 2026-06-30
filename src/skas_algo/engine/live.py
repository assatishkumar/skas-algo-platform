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
        # Fill SELLs at the bid / BUYs at the ask for options (fill_price); equity views fill at close.
        price_fn = getattr(self.market, "fill_price", None) or (lambda s, _side: self.market.close(s))
        self.broker = broker or PaperBroker(
            price_fn=price_fn, fill_model=fill_model or FillModel()
        )
        self.resolver = OverrideResolver(overrides, excluded=set(excluded_symbols or []))
        # Real broker basket margin (set by the LiveRun ~1/min); falls back to the model estimate.
        self._margin_override: float | None = None
        self.ctx = AlgoContext(
            algo_id=algo_id,
            params={},
            portfolio=self.portfolio,
            market=self.market,
            stops=self.stops,
            margin_fn=self._decision_margin,
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
        # Strategies with their own leg model (e.g. donchian's string-keyed basket) reconcile
        # themselves — the generic {symbol,...} dict rebuild below is the custom_options model.
        hook = getattr(strat, "sync_to_book", None)
        if callable(hook):
            hook(self.portfolio, ts)
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

        Equity market history is re-warmed from the cache on recovery. Options runs additionally
        persist the last-known per-contract marks (``marks``) — single-stock option premiums aren't
        in the cache, so without this a run recovering while disconnected would have no mark for its
        stock legs (P&L would read "—"). Executed trades ARE persisted (capped) so a CLOSED cycle
        keeps its realized P&L + trade log across restarts.
        """
        marks = self.market.mark_prices() if hasattr(self.market, "load_marks") else {}
        return {
            "portfolio": self.portfolio.export_state(),
            "stops": self.stops.export(),
            "marks": marks,
            "strategy": (
                self.strategy.export_state() if hasattr(self.strategy, "export_state") else {}
            ),
            "overrides": [
                {"scope": o.scope, "target": o.target, "rule": o.rule, "active": o.active}
                for o in self.resolver.overrides
            ],
            "current_month": list(self._current_month) if self._current_month else None,
            "transactions": [self._ser_txn(t) for t in self.transactions[-5000:]],
            # Daily equity-curve history + monthly flush log, so the report (equity curve, yearly,
            # monthly, capital utilization) survives a restart and accumulates over the run's life.
            # Collapsed to one row per calendar day (the EOD point) — small even for tick-driven runs.
            "history": [self._ser_txn(r) for r in self._daily_history()],
            "monthly_flush_log": {
                f"{y}-{m}": self._ser_txn(v) for (y, m), v in self.monthly_flush_log.items()
            },
        }

    def _daily_history(self) -> list[dict]:
        """History collapsed to the last (EOD) row per calendar day."""
        by_day: dict[str, dict] = {}
        for row in self.history:
            d = row.get("date")
            key = (d.date().isoformat() if hasattr(d, "date")
                   else d.isoformat() if hasattr(d, "isoformat") else str(d))
            by_day[key] = row
        return [by_day[k] for k in sorted(by_day)]

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
        if state.get("marks") and hasattr(self.market, "load_marks"):
            self.market.load_marks(state["marks"])  # last live quotes → price legs while disconnected
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
        # History + flush dates restore to datetime (matching the live-stop path) so build_report can
        # compare them against each other and the equity curve — date-vs-datetime/Timestamp raises.
        self.history = [self._rev_dt(r) for r in state.get("history", [])]
        self.monthly_flush_log = {
            tuple(int(p) for p in k.split("-")): self._rev_dt(v)
            for k, v in state.get("monthly_flush_log", {}).items()
        }

    @staticmethod
    def _rev_dt(v: dict) -> dict:
        out = dict(v)
        d = out.get("date")
        if isinstance(d, str):
            try:
                out["date"] = datetime.fromisoformat(d)
            except ValueError:
                pass
        return out

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
            opened = min((lot.opened_at for lot in lots if lot.opened_at is not None), default=None)
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
                    # Earliest lot's open date (the position's entry date) as YYYY-MM-DD.
                    "entry_date": (opened.isoformat()[:10] if hasattr(opened, "isoformat")
                                   else (str(opened)[:10] if opened else None)),
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
            # Human-readable exit criteria the strategy will act on (spot levels, %-targets,
            # per-leg / calendar exits) — surfaced so the live card shows WHY a run would exit.
            "exit_rules": self._exit_rules(),
        }

    def _exit_rules(self) -> list[str]:
        """Short, human-readable summary of the strategy's exit triggers (best-effort
        introspection of the common exit attributes). Empty when none are configured —
        the run then simply manages to expiry."""
        s = self.strategy
        rules: list[str] = []
        pt = getattr(s, "profit_target_pct", None)
        sl = getattr(s, "stop_loss_pct", None)
        if pt is not None:
            rules.append(f"Book profit at +{pt * 100:.0f}%")
        if sl is not None:
            rules.append(f"Stop out at −{sl * 100:.0f}%")
        su = getattr(s, "spot_upper", None)
        slo = getattr(s, "spot_lower", None)
        if su is not None:
            rules.append(f"Exit if spot ≥ {su:g}")
        if slo is not None:
            rules.append(f"Exit if spot ≤ {slo:g}")
        if getattr(s, "leg_targets", None):
            rules.append("Per-leg profit targets")
        if getattr(s, "leg_stops", None):
            rules.append("Per-leg stops")
        ew = getattr(s, "exit_weekday", None)
        if ew is not None:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            rules.append(f"Calendar exit from {days[int(ew) % 7]}")
        return rules

    def _exit_amounts(self) -> tuple[float | None, float | None]:
        strat = self.strategy
        pt = getattr(strat, "profit_target_pct", None)
        sl = getattr(strat, "stop_loss_pct", None)
        if pt is None and sl is None:
            return None, None
        base_fn = getattr(strat, "_risk_base", None)
        try:
            # Pass the ctx so the displayed Target/Stop use the same deployed-margin base the
            # decision uses (real broker margin when known); falls back to capital otherwise.
            base = base_fn(self.ctx) if base_fn else getattr(strat, "initial_capital", None)
        except Exception:  # pragma: no cover
            base = getattr(strat, "initial_capital", None)
        if not base:
            return None, None
        return (pt * base if pt is not None else None), (sl * base if sl is not None else None)

    def set_margin_override(self, margin: float | None) -> None:
        """Record the real broker basket margin so %-of-margin targets use actual capital at risk."""
        self._margin_override = margin

    def _decision_margin(self) -> float | None:
        """Deployed margin the strategy's %-targets apply to: the real broker margin if known,
        else the model estimate."""
        if self._margin_override is not None and self._margin_override > 0:
            return self._margin_override
        return self._model_margin()

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
        if getattr(self.strategy, "needs_supertrend", False) and hasattr(self.market, "supertrend_dir"):
            return self._watchlist_supertrend()
        if not hasattr(self.market, "universe"):
            return []  # options runs have no fixed Donchian universe to introspect
        tracking = getattr(self.strategy, "tracking", {})
        excluded = self.resolver.excluded
        # Can the strategy fund a new entry? SST sizes each buy at one "capital part"
        # (equity/parts when equity_scaled, else a fixed amount). A breakout can't be bought
        # without a free part, so flag those instead of showing a green BUY it can't act on.
        strat = self.strategy
        parts = getattr(strat, "capital_parts", 0) or 0
        cash = self.portfolio.cash
        if parts and getattr(strat, "allocation_mode", "fixed") == "equity_scaled":
            marks = self.market.mark_prices() if hasattr(self.market, "mark_prices") else {}
            allocation = (cash + self.portfolio.holdings_value(marks)) / parts
        else:
            allocation = getattr(strat, "allocation_amount", None)
        fundable = allocation is None or cash >= allocation
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
            # A tracking name whose LTP is above the 20-day high — a breakout we'd buy, if not
            # excluded and a capital part is fundable.
            breakout = is_tracking and high is not None and ltp is not None and ltp > high
            no_cash = breakout and not is_excluded and not fundable
            # Would the next decision act on this name? (breakout buy / target sell)
            signal = ""
            if held and ltp is not None and self._would_exit(lots, ltp, avg):
                signal = "SELL"
            elif breakout and not is_excluded and fundable:
                signal = "BUY"

            if is_excluded and not held:
                status = "Excluded — no new entries"
            elif held:
                status = f"Holding {len(lots)} lot(s)" + (" · excluded" if is_excluded else "")
            elif no_cash:
                status = "Breakout · no free capital"
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
                    "no_cash": no_cash,
                    "status": status,
                }
            )
        return rows

    def _watchlist_supertrend(self) -> list[dict]:
        """SuperTrend decision context per symbol: trend direction (green/red), the SuperTrend
        line + distance-to-flip, holding/P&L, and pullback-setup state — instead of SST's
        Donchian 20-day levels which are meaningless for a SuperTrend run."""
        market = self.market
        if not hasattr(market, "universe"):
            return []
        excluded = self.resolver.excluded
        setup = getattr(self.strategy, "setup", {}) or {}
        entry_mode = getattr(self.strategy, "entry_mode", "flip")
        rows: list[dict] = []
        for sym in market.universe():
            ltp = market.last_close(sym)
            direction = market.supertrend_dir(sym)
            line = market.supertrend_line(sym) if hasattr(market, "supertrend_line") else None
            lots = self.portfolio.lots(sym)
            units = sum(lot.units for lot in lots)
            cost = sum(lot.units * lot.price for lot in lots)
            avg = cost / units if units else None
            held = bool(lots)
            upnl = (units * ltp - cost) if (held and ltp is not None) else 0.0
            pnl_pct = ((ltp - avg) / avg * 100) if (held and ltp and avg) else None
            is_excluded = sym in excluded
            green = direction is not None and direction > 0
            red = direction is not None and direction < 0
            # Distance from price to the SuperTrend line, as % of price (the flip cushion).
            to_flip = ((ltp - line) / ltp * 100) if (ltp and line) else None
            s = setup.get(sym) if isinstance(setup, dict) else None
            pulled_back = bool(s and s.get("pulled_back"))

            signal = ""
            if held and red:
                signal = "SELL"  # a RED flip exits the position
            elif not held and green and not is_excluded and entry_mode == "pullback" and pulled_back:
                signal = "BUY"  # green + pulled back → a breakout entry is imminent

            if is_excluded and not held:
                status = "Excluded — no new entries"
            elif held:
                status = f"Holding {len(lots)} lot(s) · trend {'↑' if green else '↓' if red else '–'}"
            elif green:
                if entry_mode == "pullback":
                    status = "Pullback done → buy on breakout" if pulled_back else (
                        "Green — waiting for pullback" if s else "Trend ↑ (green)")
                else:
                    status = "Trend ↑ (green)"
            elif red:
                status = "Trend ↓ (red)"
            else:
                status = "Warming up"

            rows.append({
                "symbol": sym,
                "ltp": ltp,
                "direction": direction,           # +1 green / −1 red / None
                "supertrend": line,               # the trailing SuperTrend line
                "to_flip_pct": to_flip,           # % from price to the line
                "held": held,
                "lots": len(lots),
                "units": units,
                "avg_price": avg,
                "unrealized_pnl": upnl,
                "pnl_pct": pnl_pct,
                "excluded": is_excluded,
                "signal": signal,
                "status": status,
            })
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
