"""value_investing — a daily rupee drip into a watchlist, funded by selling an ETF.

The owner's spec (2026-08-20), a long-term accumulation system rather than a trading one:

    Every trading day, sort the WATCHLIST by today's change % — biggest faller first — and
    walk that list from the top buying ONE SHARE of each name while the daily budget lasts.
    Pay for it by selling exactly enough of a FUND SOURCE ETF (LIQUIDCASE / GOLDBEES /
    LIQUIDBEES) in the same decision. Never sell the stocks. Warn when the fund source has
    about two days of runway left.

Four rules that are decisions, not details (all confirmed by the owner):
  * a name the remaining budget cannot afford is SKIPPED and the walk continues to cheaper
    names below it — the day is never abandoned because one leader got expensive;
  * ONE share each, top to bottom, NO wrap-around — leftover budget evaporates, so the fund
    source drains at a steady, forecastable rate (which is what makes the runway warning
    honest);
  * funding is ALL-OR-NOTHING on the day's list: if the fund source cannot cover it, buy
    NOTHING and raise the alert. Never a half-filled day, never a silent fall back to cash;
    IDLE CASH IS SPENT FIRST, though — selling an ETF while cash sits in the account would
    be strictly worse. Live that cash is ~0 so every day sells; a backtest run without
    ``fund_seed`` spends its opening capital and only then reports the fund dry;
  * an all-green day still buys — the least-up name tops the list. The drip is the point.

WHY THIS ONE DEPLOYS LIVE AND gap_reversal/happy_twins DO NOT. Ranking by daily change needs
yesterday's close, and the obvious route — the ``indicators=`` precompute — is dead in live
(nothing seeds ``LiveMarketView.set_indicators``), which is exactly why those two fail closed
on a deploy. This reads ``ctx.prev_close`` instead, which MarketView answers from its own
series and LiveMarketView from ``_hist`` — no seeding, no warmup, both modes.

THE ONE TRAP TO KNOW: on ``quote_source="cache"`` the "live" price IS yesterday's cached
close, so every change % would be exactly 0.00, the ranking would be a total tie and the walk
would buy in watchlist order every day looking perfectly healthy. ``_stale_feed`` catches that
and refuses the day. Deploy this on a BROKER quote source.
"""

from __future__ import annotations

from datetime import date
from math import ceil

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.types import Signal, SignalAction


class ValueInvestingStrategy:
    strategy_id = "value_investing"

    def __init__(
        self,
        universe: list[str],
        initial_capital: float = 1_000_000,
        # ---- the drip ----
        daily_budget: float = 5_000.0,  # rupees deployed per trading day
        # Comma-separated, NOT a list: the Live "Edit params" modal only renders scalars, so a
        # list-valued knob would be invisible there — and hot-editing this list without a
        # redeploy is the whole point (the run's symbol list is edit-blocklisted).
        watchlist: str = "",  # "" = every priced symbol except the fund source
        # ---- funding ----
        fund_source: str = "LIQUIDBEES",  # the ONE ETF sold to raise each day's cash
        warn_days_left: int = 2,  # runway (in days of budget) that triggers the warning
        # Sell this % extra to absorb a live fill printing above the mark. 0 keeps the
        # backtest and paper exact (they fill at the same close the sizing used).
        funding_buffer_pct: float = 0.0,
        # "never" = the account already holds the ETF (LIVE default — a fresh deploy must
        # never place a surprise multi-lakh buy). "if_empty" = the backtest bootstrap: turn
        # the opening cash into the fund source on day 1, then drip from day 2.
        fund_seed: str = "never",
        **_ignored,
    ):
        self.universe = list(universe or [])
        self.initial_capital = float(initial_capital)
        self.daily_budget = float(daily_budget)
        self.fund_source = str(fund_source or "").upper()
        self.warn_days_left = int(warn_days_left)
        self.funding_buffer_pct = float(funding_buffer_pct)
        self.fund_seed = str(fund_seed or "never").lower()
        raw = watchlist if isinstance(watchlist, list) else str(watchlist or "").split(",")
        # The fund source is never bought as a stock, however it is spelled in the list.
        self.watchlist = [
            s.strip().upper()
            for s in raw
            if s and s.strip() and s.strip().upper() != self.fund_source
        ]

        # ---- runtime state (persisted) ----
        self.strategy_alert: str | None = None  # amber banner on the Live card; self-clearing
        self.last_shop_day: str | None = None  # one shop per day, survives a restart
        self.seeded = False  # the fund_seed bootstrap fires at most once
        self._alerted: dict[str, str] = {}  # {key: day_iso} — the push-notifier dedupe latch
        self._notify_fn = None

    # the manager injects this; momentum_theta_intra uses the same (symbol, message) shape
    def set_notify_fn(self, fn) -> None:
        self._notify_fn = fn

    def _notify_once(self, key: str, today: date, message: str) -> None:
        """Push to the bell/Telegram at most once per day per condition — a depletion warning
        re-derives every slice, and without the latch a 30s loop would send ~750 a day."""
        if self._alerted.get(key) == today.isoformat():
            return
        self._alerted[key] = today.isoformat()
        if self._notify_fn is not None:
            self._notify_fn(self.fund_source, message)

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx: AlgoContext) -> list[Signal]:
        today = ctx.today()
        day = today.isoformat()
        # Checked BEFORE the alert is cleared: a manual "Run decision" after the day's
        # shopping must not wipe the banner that explains what happened.
        if self.last_shop_day == day:
            return []
        self.strategy_alert = None  # state, not an event — re-derived from scratch each slice

        present = set(ctx.present_symbols())
        fund = self.fund_source
        if fund not in present:
            self._alert(f"no price for {fund} today — nothing bought (the fund source must be "
                        f"in the run's symbol list and in the data cache)")
            return []
        fund_px = ctx.close(fund)
        fund_lots = list(ctx.lots(fund))
        fund_units = sum(lot.units for lot in fund_lots)

        if seed := self._maybe_seed(ctx, fund, fund_px, fund_units, day):
            return seed

        ranked = self._rank(ctx, present)
        if ranked is None:  # the stale-feed guard already set the alert
            return []
        if not ranked:
            self._alert("no watchlist name is priced with a previous close today — "
                        "nothing to rank, nothing bought")
            return []

        plan = self._shopping_list(ranked)
        if not plan:
            self._alert(f"the daily budget ₹{self.daily_budget:,.0f} does not cover a single "
                        f"share of any watchlist name (cheapest ₹{ranked[-1][3]:,.0f})")
            return []
        cost = sum(px for _, px in plan)

        exits, running_cash, fund_units = self._fund(ctx, plan, cost, fund, fund_px,
                                                     fund_lots, fund_units, today)
        if exits is None:  # dry — _fund alerted
            return []

        entries: list[Signal] = []
        for sym, px in plan:
            # The shadow ledger is the ONLY cash check that exists — the engine happily
            # takes portfolio.cash negative (see engine/execution.py::_buy).
            if running_cash < px:
                break
            running_cash -= px
            entries.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG, quantity=1,
                                  reason="drip"))

        self._check_runway(fund_units, fund_px, running_cash, today)
        self._flag_unpriced(present)
        if entries:
            self.last_shop_day = day
        # ORDER IS LOAD-BEARING: the engine executes signals in list order and credits a
        # sale's cash synchronously, so every fund-source EXIT must precede every buy.
        return exits + entries

    # ------------------------------------------------------------------ pieces
    def _alert(self, message: str) -> None:
        self.strategy_alert = (
            message if not self.strategy_alert else f"{self.strategy_alert} · {message}"
        )

    def _maybe_seed(self, ctx, fund: str, fund_px: float, fund_units: int,
                    day: str) -> list[Signal] | None:
        """Backtest bootstrap: a run starts with cash and no ETF, so day 1 converts the
        capital into the fund source and the drip starts day 2. Off by default — a LIVE
        deploy on an account that already holds the ETF must never place this order."""
        if self.fund_seed != "if_empty" or self.seeded or fund_units > 0:
            return None
        self.seeded = True
        units = int(ctx.cash // fund_px) if fund_px > 0 else 0
        if units <= 0:
            return None
        self.last_shop_day = day
        return [Signal(symbol=fund, action=SignalAction.ENTER_LONG, quantity=units,
                       reason="fund_seed")]

    def _rank(self, ctx, present: set[str]) -> list[tuple[float, int, str, float]] | None:
        """(change, watchlist index, symbol, price), biggest faller first. None = the feed is
        stale and the whole slice must fail closed."""
        names = self.watchlist or [s for s in self.universe if s != self.fund_source]
        rows: list[tuple[float, int, str, float]] = []
        for i, sym in enumerate(names):
            if sym not in present or sym == self.fund_source:
                continue
            prev = ctx.prev_close(sym)
            if prev is None or prev <= 0:
                continue  # first bar / no history → fail closed for THIS name only
            px = ctx.close(sym)
            rows.append((px / prev - 1.0, i, sym, px))
        # A tie on change breaks by the owner's own list order, so a run is reproducible.
        rows.sort(key=lambda r: (r[0], r[1]))
        if len(rows) > 1 and all(abs(r[0]) < 1e-9 for r in rows):
            # Every name unchanged to 9 decimals is not a market, it is a quote source
            # replaying yesterday's closes (the cache-source trap in the module docstring).
            self._alert("every watchlist name shows a 0.00% change — the quote source is "
                        "replaying cached closes, so the ranking is meaningless. Nothing "
                        "bought; switch this deploy to a broker quote source.")
            return None
        return rows

    def _shopping_list(self, ranked) -> list[tuple[str, float]]:
        """One share each, top to bottom, no wrap-around. An unaffordable name is skipped and
        the walk continues; whatever is left over at the bottom evaporates."""
        remaining = self.daily_budget
        plan: list[tuple[str, float]] = []
        for _chg, _i, sym, px in ranked:
            if 0 < px <= remaining:
                plan.append((sym, px))
                remaining -= px
        return plan

    def _fund(self, ctx, plan, cost: float, fund: str, fund_px: float, fund_lots,
              fund_units: int, today: date):
        """Sell just enough of the fund source. Returns (exits, running_cash, units left), or
        (None, …) when the holding cannot cover the day — all-or-nothing, per the owner."""
        need = cost * (1.0 + self.funding_buffer_pct / 100.0) - ctx.cash
        running_cash = ctx.cash
        exits: list[Signal] = []
        if need <= 0:
            return exits, running_cash, fund_units
        sell_units = ceil(need / fund_px) if fund_px > 0 else 0
        if sell_units > fund_units:
            have = fund_units * fund_px
            self._alert(f"FUND DRY — {fund} holds ₹{have:,.0f}, today's list needs "
                        f"₹{cost:,.0f}. Nothing bought; top up {fund}.")
            self._notify_once("fund_dry", today,
                              f"{fund} cannot fund today's ₹{cost:,.0f} list (holds "
                              f"₹{have:,.0f}) — the drip has stopped.")
            return None, running_cash, fund_units
        # One EXIT per LOT: an EXIT with a quantity but no lot_id is a SILENT no-op, and the
        # quantity is clamped to that single lot (engine/overrides.py) — there is no
        # cross-lot partial sell in the engine. Portfolio order is purchase order, so this
        # walks FIFO, which is also how the ETF's gains are taxed.
        left = sell_units
        for lot in fund_lots:
            if left <= 0:
                break
            take = min(left, lot.units)
            exits.append(Signal(symbol=fund, action=SignalAction.EXIT, lot_id=lot.id,
                                quantity=take, reason="fund_source", meta={"tag": "FUND"}))
            left -= take
            running_cash += take * fund_px
        return exits, running_cash, fund_units - sell_units

    def _check_runway(self, fund_units: int, fund_px: float, cash: float, today: date) -> None:
        """Days of budget the fund source still covers, measured AFTER today's sale."""
        if self.daily_budget <= 0:
            return
        runway = fund_units * fund_px + max(0.0, cash)
        days = int(runway // self.daily_budget)
        if days > self.warn_days_left:
            return
        msg = (f"{self.fund_source} covers about {days} more day(s) of the ₹"
               f"{self.daily_budget:,.0f} budget (₹{runway:,.0f} left) — top it up.")
        self._alert(msg)
        self._notify_once("low_fund", today, msg)

    def _flag_unpriced(self, present: set[str]) -> None:
        """A watchlist name outside the run's symbol list is never priced, so it is silently
        never bought. The run's symbols are edit-blocklisted while the watchlist is not, so
        this is the ONLY layer that catches a name added by a live params edit."""
        missing = [s for s in self.watchlist if s not in present]
        if missing:
            self._alert(f"{len(missing)} watchlist name(s) have no price today: "
                        f"{', '.join(missing[:6])}{' …' if len(missing) > 6 else ''} — they "
                        f"are not in this run's symbol list (stop + redeploy to add them)")

    # ------------------------------------------------------------------ status
    def exit_rules(self) -> list[str]:
        return [
            f"Buys 1 share each, biggest faller first, until the ₹{self.daily_budget:,.0f} "
            f"daily budget runs out (no wrap-around; leftover evaporates)",
            f"Funded by selling {self.fund_source}; if it cannot cover the day's list, "
            f"NOTHING is bought and the run alerts",
            f"Warns when {self.fund_source} has ≤ {self.warn_days_left} day(s) of runway",
            "Never sells a stock — accumulation only",
        ]

    # ------------------------------------------------------------------ state
    def export_state(self) -> dict:
        return {
            "last_shop_day": self.last_shop_day,  # a 15:25 restart must not re-shop
            "seeded": self.seeded,  # never bootstrap the fund source twice
            "alerted": dict(self._alerted),  # keep the push latch across a restart
            "strategy_alert": self.strategy_alert,  # the banner survives too
        }

    def load_state(self, state: dict) -> None:
        self.last_shop_day = state.get("last_shop_day")
        self.seeded = bool(state.get("seeded", False))
        self._alerted = dict(state.get("alerted", {}))
        self.strategy_alert = state.get("strategy_alert")
