"""SuperTrend Momentum — an SST-style trend-rider driven by the SuperTrend indicator.

Entry:  buy one lot when SuperTrend flips GREEN (direction −1 → +1) on the chosen timeframe.
Exit:   a fixed configurable % profit AND/OR a SuperTrend RED flip:
        * a RED flip always exits whatever remains;
        * at the % target, book ``partial_book_pct`` of the position (default 50%) and let the
          remainder ride until the RED flip. partial_book_pct = 1.0 → full exit at the target;
          partial_book_pct = 0 → ignore the % target (pure SuperTrend exit on red).

Timeframe ∈ {daily, weekly, monthly}: the SuperTrend direction (computed from OHLC by the market
view and read via ``ctx.supertrend_dir``) reflects the chosen timeframe, so a flip occurs on the
relevant bar's close. Sizing reuses SST's capital/parts (fixed or equity-scaled). Runs unchanged
in BACKTEST and PAPER/LIVE (live SuperTrend is computed from the cached OHLC).

Funding (``funding``, 2026-09-08 — see ``_funding.EntryFundingMixin``): ``ledger`` spends the
run's own cash ledger exactly as before (ctor default, §1); ``on_demand`` queues a buy the
account cannot pay for, tells the owner the rupees to add and retries it while the signal
holds; ``park`` keeps the capital in ``fund_source`` (an ETF) and sells what tomorrow needs,
T+1 aware, holding ``float_parts`` allocations as settled cash so a signal still fills the
day it fires. Decides at 15:05 (``default_decision_time``): the platform's 15:20 is inside the
closing auction for F&O-listed cash names since CAS, where an unfilled entry halts the run.
"""

from __future__ import annotations

from typing import Any

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction

from ._funding import EntryFundingMixin


class SuperTrendMomentumStrategy(EntryFundingMixin):
    strategy_id = "supertrend_momentum"
    needs_supertrend = True  # tells the build wiring to compute SuperTrend for this run
    report_deployed_metrics = True  # adds deployed-capital + idle-cash CAGR to the report
    # 15:20 lands in the closing auction for F&O-listed cash stocks (CAS, 2026-08); an
    # order resting there does not fill, and an unfilled ENTRY halts the run. Same call as
    # value_investing; the deploy route resolves it, explicit → this → the platform's 15:20.
    default_decision_time = "15:05"

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 2_500_000,
        capital_parts: int = 50,
        allocation_mode: str = "fixed",      # "fixed" | "equity_scaled"
        timeframe: str = "daily",            # "daily" | "weekly" | "monthly"
        supertrend_period: int = 10,         # ATR period (configurable)
        supertrend_multiplier: float = 3.0,  # ATR band multiplier (configurable)
        profit_target: float = 0.05,         # book at +this% over average cost
        partial_book_pct: float = 0.5,       # share booked at the target (1.0 = full, 0 = none)
        entry_mode: str = "flip",            # "flip" = buy on green flip; "pullback" = wait for a dip + breakout
        pullback_pct: float = 0.0,           # min dip below the post-flip peak to count as a pullback
        idle_return: float = 0.06,           # reporting-only: assumed annual yield on idle cash
        # Point-in-time index membership (pit_universe mode): entries only, never exits.
        membership: dict[str, list[str]] | None = None,
        # EXPOSURE BRAKE: no NEW entries while this symbol's SuperTrend (same config as
        # the run's) is red; exits unaffected. None = no brake (unchanged).
        regime_symbol: str | None = None,
        # ---- entry funding (EntryFundingMixin; every default = the historical ledger) ----
        funding: str = "ledger",             # "ledger" | "on_demand" | "park"
        fund_source: str | None = None,      # park: the ETF that holds the capital
        float_parts: float = 1.0,            # park: allocations kept as settled cash
        settlement_days: int = 1,            # T+1 for every sale's proceeds (managed modes)
        funding_buffer_pct: float = 5.0,     # park: sell this much extra ETF
        fund_seed: str = "never",            # park backtest: "if_empty" parks day-1 cash
        fund_size_cap: bool = False,         # adopt the ETF only up to capital (shared holding)
        **_ignored,
    ):
        self.universe = universe
        self._init_funding(funding, fund_source, float_parts, settlement_days,
                           funding_buffer_pct, fund_seed, fund_size=initial_capital,
                           fund_size_cap=fund_size_cap)
        self.capital_parts = int(capital_parts)
        self.allocation_mode = allocation_mode
        self.allocation_amount = initial_capital / capital_parts
        self.timeframe = str(timeframe).lower()
        self.supertrend_period = int(supertrend_period)
        self.supertrend_multiplier = float(supertrend_multiplier)
        self.profit_target = float(profit_target)
        self.partial_book_pct = float(partial_book_pct)
        self.entry_mode = str(entry_mode).lower()
        self.pullback_pct = float(pullback_pct)
        self.idle_return = float(idle_return)
        from ._membership import MembershipGate
        self.gate = MembershipGate(membership)
        self.regime_symbol = regime_symbol or None
        # Per-symbol state: last seen SuperTrend direction + whether we've booked the partial.
        self.prev_dir: dict[str, float] = {}
        self.partial_booked: dict[str, bool] = {}
        # Pending pullback setups (pullback mode): symbol -> {peak, pulled_back, pivot}.
        self.setup: dict[str, dict] = {}

    def supertrend_config(self) -> dict:
        """Params the market view needs to precompute SuperTrend for this run."""
        return {
            "period": self.supertrend_period,
            "multiplier": self.supertrend_multiplier,
            "timeframe": self.timeframe,
        }

    def _float_target_hint(self) -> float:
        return self.float_parts * self.allocation_amount if self.funding == "park" else 0.0

    def _allocation(self, ctx: AlgoContext) -> float:
        if self.allocation_mode == "equity_scaled":
            # The run's FUND, not its book equity. Adopted ETF units sit on the book without
            # having been paid for, so ctx.equity() counts the deploy capital AND the ETF —
            # ₹19L on a ₹10L fund, every part sized at ₹1.9L. Subtract what was adopted
            # once; profits and losses then flow into every part through cash and holdings.
            return max(0.0, ctx.equity() - self.adopted_value) / self.capital_parts
        return self.allocation_amount

    # ------------------------------------------------------- (de)serialize
    def initial_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.export_state()

    def export_state(self) -> dict[str, Any]:
        return {
            "prev_dir": dict(self.prev_dir),
            "partial_booked": dict(self.partial_booked),
            "setup": {s: dict(v) for s, v in self.setup.items()},
            **self.funding_state(),
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.prev_dir = {k: float(v) for k, v in state.get("prev_dir", {}).items()}
        self.partial_booked = {**self.partial_booked, **state.get("partial_booked", {})}
        self.setup = {s: dict(v) for s, v in state.get("setup", {}).items()}
        self.load_funding_state(state)

    def exit_rules(self) -> list[str]:
        rules = ["SuperTrend flips red → exit everything that remains"]
        if self.partial_book_pct > 0 and self.profit_target > 0:
            share = ("the whole position" if self.partial_book_pct >= 1.0
                     else f"{self.partial_book_pct * 100:g}% of the position")
            rules.append(f"+{self.profit_target * 100:g}% over cost → book {share}")
        return rules + self.funding_rules()

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        present = ctx.present_symbols()
        signals: list[Signal] = []
        managed = self.funds_managed
        today = ctx.today() if hasattr(ctx, "today") else None
        today_iso = today.isoformat() if today else "9999-12-31"
        allocation = self._allocation(ctx)
        held = set(ctx.lot_symbols())
        fund = self.fund_source if managed else None
        if fund:
            held.discard(fund)   # the parking ETF is never a trading name
        float_target = self.float_parts * allocation if self.funding == "park" else 0.0
        if managed:
            running_cash = self._settle(ctx, today)
            seed = self._maybe_seed(ctx, float_target)
            if seed:
                self._remember_dirs(ctx, present)
                return seed
        else:
            running_cash = ctx.cash

        # --- Step 1: exits (held names) — RED flip exits the remainder; % target books a share ---
        for sym in held:
            if sym not in present:
                continue
            dir_now = ctx.supertrend_dir(sym)
            if dir_now is None:
                continue
            lots = ctx.lots(sym)
            if not lots:
                continue
            close = ctx.close(sym)
            units = sum(lot.units for lot in lots)
            avg = sum(lot.units * lot.price for lot in lots) / units if units else 0.0

            if dir_now < 0:  # SuperTrend red → exit everything that remains
                signals.append(Signal(symbol=sym, action=SignalAction.EXIT_ALL, reason="supertrend_red"))
                proceeds = units * close
                running_cash += self._credit(today, proceeds) if managed else proceeds
                self.partial_booked[sym] = False
                continue

            # Still green: book the configured share once, at the % target.
            if (
                self.partial_book_pct > 0
                and not self.partial_booked.get(sym, False)
                and avg > 0
                and (close - avg) / avg >= self.profit_target
            ):
                book_units = int(round(units * self.partial_book_pct))
                if self.partial_book_pct >= 1.0 or book_units >= units:
                    signals.append(Signal(symbol=sym, action=SignalAction.EXIT_ALL, reason="target"))
                    proceeds = units * close
                    running_cash += self._credit(today, proceeds) if managed else proceeds
                    self.partial_booked[sym] = False
                elif book_units > 0:
                    lot = lots[0]  # one lot per entry → book part of it; remainder rides to red
                    signals.append(Signal(symbol=sym, action=SignalAction.EXIT, lot_id=lot.id,
                                          quantity=book_units, reason="partial_target",
                                          meta={"tag": "BOOK"}))
                    running_cash += (self._credit(today, book_units * close) if managed
                                     else book_units * close)
                    self.partial_booked[sym] = True

        # --- Step 2: entries — buy one lot on a GREEN flip ("flip"), or after a pullback +
        #     breakout of the post-flip high ("pullback") ---
        buys: list[Signal] = []

        def _buy(sym: str, close: float) -> bool:
            nonlocal running_cash
            units = int(allocation // close)
            if units <= 0:
                return False
            if managed:
                # Funded from settled cash, else QUEUED (the decision is made either way —
                # the queue owns it from here, retried daily while the signal holds).
                sig = self._want(sym, units, close, today)
                if sig is not None:
                    buys.append(sig)
                    self.partial_booked[sym] = False
                return True
            if running_cash < allocation:
                return False
            running_cash -= units * close
            buys.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG, quantity=units))
            self.partial_booked[sym] = False
            return True

        regime_ok = True
        if self.regime_symbol and self.regime_symbol in present:
            rd = ctx.supertrend_dir(self.regime_symbol)
            regime_ok = rd is None or rd > 0     # fail OPEN on missing data
        if managed:
            # Retry what is queued FIRST (oldest claims on today's cash), re-sized at today's
            # price. A name that flipped red meanwhile is cancelled — the signal is gone.
            for sym in list(self.pending_entries):
                if sym in held or sym == fund:
                    self.pending_entries.pop(sym, None)
                    continue
                if sym not in present:
                    continue
                d = ctx.supertrend_dir(sym)
                if d is None:
                    continue
                if d < 0:
                    self._cancel_pending(sym, today, "SuperTrend turned red before it was funded")
                    continue
                if regime_ok:
                    _buy(sym, ctx.close(sym))
        for sym in present:
            if sym in held or sym == fund or sym in self.pending_entries:
                continue
            if sym == self.regime_symbol or not regime_ok:
                continue   # the index itself is never traded; red regime = no new parts
            if not self.gate.allows(sym, today_iso):
                continue   # not an index member on this date (point-in-time universe)
            dir_now = ctx.supertrend_dir(sym)
            prev = self.prev_dir.get(sym)
            if dir_now is None or prev is None:
                continue  # need a prior direction to detect an actual flip (no mid-trend entry)
            close = ctx.close(sym)
            flipped_green = prev < 0 and dir_now > 0

            if self.entry_mode != "pullback":
                if flipped_green:
                    _buy(sym, close)
                continue

            # Pullback mode: arm on the green flip, then enter on the breakout of the post-flip
            # high after a dip. A red flip cancels the pending setup.
            if dir_now < 0:
                self.setup.pop(sym, None)
                continue
            if flipped_green:
                self.setup[sym] = {"peak": close, "pulled_back": False, "pivot": None}
            s = self.setup.get(sym)
            if s is None:
                continue  # green but no fresh flip armed (don't enter mid-trend)
            if not s["pulled_back"]:
                if close > s["peak"]:
                    s["peak"] = close
                elif s["peak"] > 0 and (s["peak"] - close) / s["peak"] >= self.pullback_pct and close < s["peak"]:
                    s["pulled_back"] = True
                    s["pivot"] = s["peak"]  # the prior high to break for entry
                continue
            if close > s["pivot"] and _buy(sym, close):  # breakout above the pre-pullback high
                self.setup.pop(sym, None)

        # --- Step 3: fund-source legs, then remember today's direction ---
        if managed:
            self._fund_signals(ctx, today, float_target)
            # ORDER: stock exits, fund sales, stock buys, park-back. A rejected BUY halts the
            # run and abandons the rest of the decision, so the sale that funds tomorrow must
            # never sit behind a buy.
            signals = signals + self._fund_exits + buys + self._late_buys + self._park_buy
        else:
            signals = signals + buys
        self._remember_dirs(ctx, present)
        return signals

    def _remember_dirs(self, ctx, present) -> None:
        for sym in present:
            d = ctx.supertrend_dir(sym)
            if d is not None:
                self.prev_dir[sym] = d
