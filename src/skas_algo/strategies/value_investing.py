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

HOW THE BUDGET IS SPREAD (``sizing``). The spec's one-share-of-each walk turned out to weight
the book by SHARE PRICE — one share of a ₹2,299 name is 51× the rupees of one share of a ₹45
name — which over 2020-26 spread the owner's list across ₹1,383 to ₹2,85,902 per name (207×)
and lost ~7 points of XIRR to a plain index SIP. ``sizing="equal_value"`` separates ALLOCATION
from TIMING: each name is credited ``daily_budget/N`` into its own pot every day and spends
only from that pot, so the ranking decides who buys first, never who gets the money. Names buy
at different rhythms and end up with the same rupees (₹75,380…₹77,584, XIRR 18.87%, +3.14 pts
vs the index). See the ctor comment for all three modes. An unrecognised mode RAISES — it used
to fall through to one_share in silence, and a run that asked for equal_value against an older
backend traded price-weighted while looking perfectly healthy (#279).

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
from skas_algo.live.holidays import next_trading_day


_SIZING_MODES = frozenset({"one_share", "balanced", "equal_value"})


class ValueInvestingStrategy:
    strategy_id = "value_investing"
    # The platform-wide default is 15:20, which since SEBI's Closing Auction Session
    # (2026-08-03) is PAST the end of continuous cash trading for F&O-listed stocks — the
    # order would rest in the auction, never fill, and halt the run daily. That default was
    # harmless while no equity strategy could deploy live; this one can (it reads
    # ctx.prev_close, not the dead indicators= precompute), so it names its own. 15:05 keeps
    # the ranking signal (nearly the whole day's move) while staying clear of the auction.
    # Deploy-level, so a run that stored 15:20 keeps it until redeployed (§1).
    default_decision_time = "15:05"
    # Ask the backtest service for the accumulation panel (per-holding breakdown, sleeve
    # XIRR, index-SIP comparison) instead of leaving only the trading metrics, which here
    # describe the ETF sweeps rather than the investments.
    report_holdings = True

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
        # How the daily budget is SPREAD across the watchlist. All three keep the same
        # faller-first ranking; they differ only in how many rupees each name receives.
        #
        #   "one_share"   the spec: one share of each name, top-down. Silently weights the
        #                 portfolio by SHARE PRICE, because a ₹2,299 name absorbs 51× the
        #                 rupees of a ₹45 one for the same "one share". Over 2020-26 on the
        #                 owner's list that put 18.9% of the money into TCS (1.2% CAGR) and
        #                 4.3% into KTKBANK (39.1%) — invested per name ₹1,383…₹2,85,902, a
        #                 207× spread — and cost ~7 points of XIRR against a plain index SIP.
        #   "balanced"    the same one-share walk, but SKIP a name whose cumulative invested
        #                 already exceeds the watchlist average by max_skew_pct. A corrective
        #                 overlay: it steers new flow to the laggards but cannot undo a gap
        #                 already opened, and it still buys in whole shares.
        #   "equal_value" allocation and timing are SEPARATED, which is what actually fixes
        #                 it. Every name is credited daily_budget/N into its own POT — every
        #                 name, every day, whatever it did or whether it even printed. A name
        #                 then buys floor(pot/price) shares and pays from its OWN pot; if the
        #                 pot cannot afford one share the money simply waits for tomorrow.
        #                 The ranking no longer decides allocation, only who spends first when
        #                 the day is tight. Names therefore buy at different RHYTHMS (a ₹2,299
        #                 name every ~2 days, a ₹45 name daily) while receiving identical
        #                 rupees: the only divergence is what is briefly parked in a pot, which
        #                 is bounded by one share's price. Same list, same window: invested per
        #                 name ₹75,380…₹77,584 (1.0×), XIRR 18.87%, +3.14 pts vs the index SIP.
        #                 A watchlist change needs no epoch logic here — the split is over the
        #                 CURRENT names, so a name added today takes its 1/N from today and
        #                 never buys catch-up, and a removed name stops being credited (it also
        #                 stops being ranked, so its leftover pot is inert).
        #
        # Ctor default is the spec (§1: a recovered deploy must be byte-identical); the
        # backtest FORM defaults to equal_value.
        # T+1 SETTLEMENT (owner 2026-08-27). An Indian equity CNC sale's proceeds are NOT
        # spendable the same day — Dhan confirmed it in writing after live run 23 halted on
        # "insufficient funds". 0 = the historical same-day funding, so a recovered deploy is
        # byte-identical (§1); the FORM and deploy default to 1. Modelled in BOTH modes, so
        # backtest == live (the founding parity rule) rather than the backtest flattering
        # itself by deploying a day early.
        settlement_days: int = 0,
        sizing: str = "one_share",  # one_share | balanced | equal_value
        max_skew_pct: float = 25.0,         # balanced only: allowed overweight vs the average
        fund_seed: str = "never",
        # Annualised yield the FUND SOURCE really earns, for reporting only. A dividend
        # liquid ETF (LIQUIDBEES) holds NAV at ₹1,000 and pays out as units, so a price-only
        # backtest credits parked money 0% — over a long run that understates the portfolio
        # badly. Stated as its own report line, NEVER folded into the engine's equity curve.
        # 0 = report nothing (the honest default when the rate is unknown).
        fund_yield_pct: float = 0.0,
        **_ignored,
    ):
        self.universe = list(universe or [])
        self.initial_capital = float(initial_capital)
        self.daily_budget = float(daily_budget)
        self.fund_source = str(fund_source or "").upper()
        self.warn_days_left = int(warn_days_left)
        self.funding_buffer_pct = float(funding_buffer_pct)
        self.settlement_days = max(0, int(settlement_days))
        self.sizing = str(sizing or "one_share").lower()
        # An unrecognised mode used to fall through to one_share SILENTLY: a run asking for
        # equal_value against a backend that predates it traded price-weighted and looked
        # perfectly healthy (#279, 2026-08-24). A sizing typo must never be a silent
        # allocation change — fail at construction, so the deploy/backtest 422s instead.
        if self.sizing not in _SIZING_MODES:
            raise ValueError(
                f"unknown sizing {self.sizing!r} — expected one of {sorted(_SIZING_MODES)}"
            )
        self.max_skew_pct = float(max_skew_pct)
        self.fund_seed = str(fund_seed or "never").lower()
        # ---- the settlement ledger (persisted) ----
        # `settled_cash` is what is spendable NOW; `pending_credits` are sale proceeds and the
        # trading day they land. The strategy owns this rather than reading ctx.cash because
        # the ENGINE credits a sale synchronously — which is exactly the fiction T+1 breaks.
        # None = uninitialised; seeded on the first decision (broker funds live, ctx.cash in
        # backtest).
        self.settled_cash: float | None = None
        self.pending_credits: list[list] = []          # [[iso_date, amount], …]
        self._broker_funds: float | None = None        # manager push; transient, not persisted
        # cumulative rupees put into each name — drives the balanced walk (persisted)
        self.invested: dict[str, float] = {}
        # Balance is measured over the CURRENT EPOCH, not since inception. An epoch starts
        # whenever the watchlist changes: every name's counter is re-based to what it already
        # holds, so from that day the budget splits evenly among the names now on the list.
        # Without this a name added to a mature book is bought EVERY day until it reaches the
        # pack's average — ~18 months and a permanent daily slot on the owner's numbers —
        # which is catch-up, not ratio maintenance (owner rule 2026-08-21).
        self.epoch_base: dict[str, float] = {}
        self.epoch_names: list[str] = []
        # equal_value: each name's unspent share of past budgets. Carrying the remainder is
        # what makes the rupees equal — an expensive name saves up for a few days instead of
        # being priced out, and nothing evaporates.
        self.pot: dict[str, float] = {}
        self.fund_yield_pct = float(fund_yield_pct)
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

        # SPEND ONLY WHAT HAS SETTLED. Under T+1 the money for today's buys was raised on a
        # PREVIOUS day, and today's sale funds tomorrow. The two models are kept strictly
        # apart: settlement_days=0 is the historical same-day path, byte-identical (§1).
        spendable = self._settle(ctx, today)
        # equal_value's POTS already do the budgeting (a name may only spend its own pot), so
        # its cap is purely the cash. one_share/balanced have no pots, so the day's budget is
        # the discipline. cap=None → the historical unbounded walk.
        if self.settlement_days:
            cap = spendable if self.sizing == "equal_value" else min(self.daily_budget, spendable)
        else:
            cap = None

        plan = self._shopping_list(ranked, cap)
        if not plan:
            if self.settlement_days and spendable < 1.0:
                self._alert("nothing has settled yet — today's fund-source sale lands "
                            "tomorrow (T+1), and the drip starts from it")
            else:
                self._alert(f"the daily budget ₹{self.daily_budget:,.0f} does not cover a "
                            f"single share of any watchlist name "
                            f"(cheapest ₹{ranked[-1][3]:,.0f})")

        if self.settlement_days:
            # BUY from settled cash…
            entries, running_cash = self._emit(plan, spendable)
            self.settled_cash = running_cash
            # …then raise TOMORROW's float.
            exits, fund_units = self._presell(fund, fund_px, fund_lots, fund_units, today)
        else:
            cost = sum(px * u for _, px, u in plan)
            exits, running_cash, fund_units = self._legacy_fund(
                ctx, cost, fund, fund_px, fund_lots, fund_units, today)
            if exits is None:                    # dry — _legacy_fund alerted
                return []
            entries, running_cash = self._emit(plan, running_cash)

        self._check_runway(fund_units, fund_px, running_cash, today)
        self._flag_unpriced(present)
        if entries or exits:
            self.last_shop_day = day
        # ORDER IS LOAD-BEARING, but under T+1 for a NEW reason. It used to be "the sale funds
        # the same tick, so every fund-source EXIT must precede every buy". The sale no longer
        # funds today at all — yet it must STILL come first, because a rejected BUY halts the
        # run (order_error) and a sale queued behind the buys would then never fire, starving
        # TOMORROW as well. Pre-funding the next session is the more important half, and is
        # harmless if the buys then fail.
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

    # ------------------------------------------------------- the settlement ledger
    def set_broker_funds(self, value: float) -> None:
        """Manager push: the account's REAL available balance. Live only — probed by
        ``getattr`` exactly like ``set_broker_margin``, so nothing else has to know."""
        if value is not None and value >= 0:
            self._broker_funds = float(value)

    def _settle(self, ctx, today: date) -> float:
        """Age pending credits into spendable cash and return what may be spent today.

        LIVE, the BROKER is truth: the ledger drifts (charges, and the same account funds
        other runs), so the spendable figure is capped at what the account actually holds.
        Run 23 is the case in point — a ₹1,00,00,000 ledger against a ₹146.03 balance, which
        is how a ₹329.40 buy came to be rejected for want of ₹83.48."""
        if self.settled_cash is None:                    # first decision of this run
            self.settled_cash = float(ctx.cash)
        due = [c for c in self.pending_credits if date.fromisoformat(c[0]) <= today]
        for c in due:
            self.settled_cash += float(c[1])
        if due:
            self.pending_credits = [
                c for c in self.pending_credits if date.fromisoformat(c[0]) > today
            ]
        if self._broker_funds is not None:
            self.settled_cash = min(self.settled_cash, self._broker_funds)
        return max(0.0, self.settled_cash)

    def _pending_total(self) -> float:
        return sum(float(c[1]) for c in self.pending_credits)

    def _presell(self, fund: str, fund_px: float, fund_lots, fund_units: int,
                 today: date) -> tuple[list[Signal], int]:
        """Top the settled-cash float up for the NEXT session — the T+1 half of the model.

        Target is one day's budget plus ``funding_buffer_pct`` (the owner's ~10%), and we
        sell only the DIFFERENCE: cash already settled and sales already in flight both
        count, so a day that underspent does not trigger a second sale and the float never
        ratchets up."""
        if fund_px <= 0 or self.daily_budget <= 0:
            return [], fund_units
        # Pots are CLAIMS on cash, so the float has to cover them — a ₹2,900 name saving for
        # six days needs ₹2,900 on the day it fires, not one day's ₹1,000. In steady state
        # sum(pot) is a few days of budget (each pot is < one share's price), so this settles
        # naturally rather than growing.
        claims = sum(self.pot.values()) if self.sizing == "equal_value" else 0.0
        target = max(self.daily_budget, claims) * (1.0 + self.funding_buffer_pct / 100.0)
        need = target - max(0.0, self.settled_cash or 0.0) - self._pending_total()
        if need <= 0:
            return [], fund_units                        # the float is already covered
        want = ceil(need / fund_px)
        sell = min(want, fund_units)
        if sell <= 0:
            # LOUD, but never a halt (owner 2026-08-27): the ETF is refilled outside the
            # strategy, so a dry moment is a top-up reminder, not a failure. Cash already
            # settled keeps buying — the drip degrades, it does not stop dead.
            self._alert(f"FUND DRY — {fund} has nothing left to sell, so tomorrow's ₹"
                        f"{self.daily_budget:,.0f} budget is unfunded. Top it up in the "
                        f"broker; the drip continues on whatever cash has settled.")
            self._notify_once("fund_dry", today,
                              f"{fund} is empty — top it up. Buying continues on settled "
                              f"cash only, so the drip will thin out within a day.")
            return [], fund_units
        if sell < want:
            self._alert(f"{fund} could only cover ₹{sell * fund_px:,.0f} of tomorrow's ₹"
                        f"{need:,.0f} top-up — the drip will be short.")
        # One EXIT per LOT: an EXIT with a quantity but no lot_id is a SILENT no-op and the
        # quantity is clamped to that single lot (engine/overrides.py) — there is no
        # cross-lot partial sell. Portfolio order is purchase order → FIFO, the Indian tax
        # treatment too.
        exits: list[Signal] = []
        left = sell
        for lot in fund_lots:
            if left <= 0:
                break
            take = min(left, lot.units)
            exits.append(Signal(symbol=fund, action=SignalAction.EXIT, lot_id=lot.id,
                                quantity=take, reason="fund_source", meta={"tag": "FUND"}))
            left -= take
        # The proceeds are NOT spendable today. Park them on the settlement date; the engine
        # will still credit portfolio.cash immediately, and this ledger is what the strategy
        # actually spends against.
        landing = next_trading_day(today, self.settlement_days) if self.settlement_days else today
        proceeds = sell * fund_px
        if self.settlement_days:
            self.pending_credits.append([landing.isoformat(), proceeds])
        else:
            self.settled_cash = (self.settled_cash or 0.0) + proceeds
        return exits, fund_units - sell

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

    def _shopping_list(self, ranked, cap: float | None) -> list[tuple[str, float, int]]:
        """(symbol, price, units) for today, in rank order.

        "equal_value" — THE fix for the price-weighting problem. Every watchlist name is
        credited an equal slice of the daily budget into its own running pot; a name buys
        whole shares only when its pot can afford them, and the remainder carries forward.
        Rupees in are therefore equal BY CONSTRUCTION regardless of share price: a Rs2,300
        stock buys one share every six days, a Rs6.53 stock buys 59 in one. Under one-share-
        each the same 13-name list put Rs2,85,902 into TCS and Rs1,383 into SOUTHBANK — the
        same number of buys, 207x the money (run #278).

        "one_share" — the original spec: one share each, top to bottom, budget-limited, no
        wrap-around, leftover evaporates. "balanced" — one_share plus a skew cap.
        """
        if self.sizing == "equal_value":
            return self._equal_value_plan(ranked, cap)

        remaining = self.daily_budget if cap is None else max(0.0, cap)
        plan: list[tuple[str, float, int]] = []
        balanced = self.sizing == "balanced"
        spent = dict(self.invested)     # projected book as the walk spends
        names = [sym for _c, _i, sym, _p in ranked]
        if balanced:
            # Roll on the CONFIGURED watchlist, never on the names that happen to have printed
            # today — one missing quote would otherwise re-base the whole balance and forgive
            # every accumulated overweight.
            self._roll_epoch(self.watchlist or names)
            names = self.epoch_names or names
        for _chg, _i, sym, px in ranked:
            if not (0 < px <= remaining):
                continue
            if balanced and self._too_heavy(sym, px, spent, names):
                continue
            plan.append((sym, px, 1))
            remaining -= px
            spent[sym] = spent.get(sym, 0.0) + px
        return plan

    def _equal_value_plan(self, ranked, cap: float | None) -> list[tuple[str, float, int]]:
        """Credit every name an equal share of the budget, then spend what each pot affords.

        ``cap`` is the rupees actually spendable today (settled cash under T+1). It bounds the
        walk so the PLAN equals what gets EMITTED — previously the caller could stop emitting
        when its cash ran out AFTER this method had already debited those names' pots, and
        that money silently vanished. Same-day funding hid it (the sale always covered the
        plan); settled-cash gating would have hit it almost daily. It also replaces a
        `daily_budget + sum(pot)` ceiling that double-counted the day's credit and so never
        actually bound."""
        names = self.watchlist or [s for _c, _i, s, _p in ranked]
        if not names:
            return []
        slice_ = self.daily_budget / len(names)
        for n in names:                       # credited even when the name did not print —
            self.pot[n] = self.pot.get(n, 0.0) + slice_   # its money waits, it is not lost
        # cap None = the historical unbounded walk (settlement_days=0, byte-identical per §1).
        # A pot exists precisely so an expensive name can SAVE UP and then spend more than one
        # day's slice, so the cap must be spendable CASH — capping at daily_budget would price
        # a ₹2,900 stock out forever on a ₹1,000 budget (caught by an existing test).
        remaining = (self.daily_budget + sum(self.pot.values())) if cap is None else max(0.0, cap)
        plan: list[tuple[str, float, int]] = []
        for _chg, _i, sym, px in ranked:      # rank order decides WHO SPENDS FIRST…
            if px <= 0:
                continue
            units = int(self.pot.get(sym, 0.0) // px)     # …not who gets the money
            if units <= 0:
                continue
            cost = units * px
            if cost > remaining:
                units = int(remaining // px)
                cost = units * px
            if units <= 0:
                continue
            self.pot[sym] = self.pot.get(sym, 0.0) - cost
            remaining -= cost
            plan.append((sym, px, units))
        return plan

    def _roll_epoch(self, names: list[str]) -> None:
        """Start a new balance epoch when the watchlist changes — re-base every current name
        to what it already holds, so the ONGOING flow is what gets split evenly. A name added
        today then takes its 1/N share from today; it does not chase the incumbents' history.

        Keyed on the CONFIGURED watchlist, so a name that simply did not print today does not
        look like a watchlist change."""
        key = sorted(names)
        if key == self.epoch_names:
            return
        self.epoch_names = key
        self.epoch_base = {n: self.invested.get(n, 0.0) for n in key}

    def _flow(self, sym: str, spent: dict[str, float]) -> float:
        """Rupees into ``sym`` SINCE the current epoch began."""
        return max(0.0, spent.get(sym, 0.0) - self.epoch_base.get(sym, 0.0))

    def _too_heavy(self, sym: str, px: float, spent: dict[str, float],
                   names: list[str]) -> bool:
        """Is this name ALREADY unfairly ahead of the pack?

        Tests the name's CURRENT book, not the book after the buy — testing the post-buy
        figure compares one share's price against a per-name average that starts near zero,
        so the very first purchase locks out every other name and the strategy grinds to a
        halt (Rs159 invested over six years, 2026-08-21). Judging what is already owned lets
        an untouched name through every time and lets an expensive one buy its first share,
        while a name that has run ahead waits for the others to catch up."""
        if not names:
            return False
        avg = sum(self._flow(n, spent) for n in names) / len(names)
        if avg <= 0:
            return False
        return self._flow(sym, spent) > avg * (1.0 + self.max_skew_pct / 100.0)

    def _legacy_fund(self, ctx, cost: float, fund: str, fund_px: float, fund_lots,
                     fund_units: int, today: date):
        """The PRE-T+1 model, kept verbatim for ``settlement_days=0`` (§1: a recovered deploy
        must be byte-identical). Sell just enough of the fund source to cover TODAY's basket
        and spend the proceeds in the same tick. Returns (exits, running_cash, units left), or
        (None, …) when the holding cannot cover the day — all-or-nothing, per the owner.

        Correct in a backtest, impossible live: an equity CNC sale settles T+1. See _presell
        for the model that replaced it."""
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

    def _emit(self, plan, cash: float) -> tuple[list[Signal], float]:
        """Turn the plan into BUY signals, stopping when ``cash`` runs out. The shadow ledger
        is the ONLY cash check that exists — the engine takes portfolio.cash negative without
        complaint (engine/execution.py::_buy)."""
        out: list[Signal] = []
        for sym, px, units in plan:
            cost = px * units
            if cash < cost:
                break
            cash -= cost
            self.invested[sym] = self.invested.get(sym, 0.0) + cost
            out.append(Signal(symbol=sym, action=SignalAction.ENTER_LONG, quantity=units,
                              reason="drip"))
        return out, cash

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
            "settled_cash": self.settled_cash,
            "pending_credits": [[str(d), float(a)] for d, a in self.pending_credits],
            "invested": dict(self.invested),  # the balanced walk needs the running book
            "epoch_base": dict(self.epoch_base),   # …measured over the current epoch
            "epoch_names": list(self.epoch_names),
            "pot": dict(self.pot),
            "alerted": dict(self._alerted),  # keep the push latch across a restart
            "strategy_alert": self.strategy_alert,  # the banner survives too
        }

    def load_state(self, state: dict) -> None:
        self.last_shop_day = state.get("last_shop_day")
        self.seeded = bool(state.get("seeded", False))
        sc = state.get("settled_cash")
        self.settled_cash = None if sc is None else float(sc)
        self.pending_credits = [[str(d), float(a)]
                                for d, a in (state.get("pending_credits") or [])]
        self.invested = dict(state.get("invested", {}))
        self.epoch_base = dict(state.get("epoch_base", {}))
        self.epoch_names = list(state.get("epoch_names", []))
        self.pot = dict(state.get("pot", {}))
        self._alerted = dict(state.get("alerted", {}))
        self.strategy_alert = state.get("strategy_alert")
