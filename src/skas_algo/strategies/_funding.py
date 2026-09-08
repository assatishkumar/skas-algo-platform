"""Entry funding for a positional equity strategy that buys one lot per name (owner ask,
2026-09-08, for supertrend_momentum — any one-lot-per-name equity book can inherit it).

THE PROBLEM. A strategy's cash ledger is whatever ``capital`` was typed at deploy and has no
connection to the account. supertrend_momentum sized every buy off that ledger, so a live
deploy would place a ₹1L order against whatever happened to be in the account, the broker
would reject it, and — because an unfilled ENTRY halts the run (owner rule) — the run would
stop on its first signal. The owner wants two ways to fund an entry instead:

  * ``funding="on_demand"`` — the strategy asks. A signal whose cost exceeds the account's
    settled cash is QUEUED, the owner is told how much to add (banner + one push a day),
    and the buy is retried at every decision while the signal stays valid (SuperTrend still
    green); a red flip cancels it. No order is ever placed that the account cannot pay for,
    so the halt rule never fires and nothing is retried at the broker.
  * ``funding="park"`` — the money sits in an ETF (``fund_source``: LIQUIDCASE, GOLDBEES…)
    and the strategy SELLS the units it needs. Like value_investing this is T+1 aware: an
    equity CNC sale settles the next trading day, so the sale is placed today and the buy
    fills tomorrow from settled cash. To keep the backtest's same-day entry, ``float_parts``
    allocations are held as settled cash at all times (default 1 — one part's worth of idle
    yield is the price of not waiting a day); the float is topped up from the ETF after every
    spend and the excess above it is parked back after every sale.
  * ``funding="ledger"`` — the historical behaviour, byte-identical: spend ``ctx.cash``, skip
    a signal the ledger cannot cover, never queue. The ctor default (§1).

ONE ACCOUNT, MANY RUNS (owner 2026-09-08). Two strategies on one account each keep their
OWN funding ETF, but the broker's CASH balance is one number, and neither run may spend
the other's cash. So each run's settled cash is DERIVED from its own book every decision:
``portfolio.cash`` (the engine's per-run cash, fills and charges included) less the ETF it
ADOPTED (those units moved no cash — ``on_fund_adopted`` records them) less the sale
proceeds still settling. A run deployed with capital = ETF + float therefore starts with
exactly its float, and nothing a sibling run does can appear in its figure. The broker's
available balance only CAPS today's spend (an overdraft guard, never the ledger).
Optional ``fund_size_cap``: treat ``capital`` as the run's fund size and adopt broker-held
units of its ETF only up to (fund size − pool) / price — for a HOLDING shared between two
runs. Off by default: with separate ETFs a top-up should always be adopted in full.

ONE LEDGER, BOTH MODES. ``settled_cash`` + ``pending_credits`` model settlement exactly as
value_investing does (the engine credits a sale synchronously; this is what the strategy
actually spends against), stock-sale proceeds included — a red-flip exit's money is not
spendable until tomorrow either. LIVE the broker is truth: ``set_broker_funds`` (the
manager's ~1/min push) caps what may be spent today, never the ledger itself (a shared
account can block cash that later frees up — value_investing's lesson). In a backtest there
is no push, so the ledger IS the account and backtest == live by construction.

SIGNAL ORDER IS LOAD-BEARING: stock exits, then fund-source sales, then stock buys, then the
park-back buy. A rejected BUY halts the run and abandons the rest of the decision, so a sale
queued behind the buys would never fire and starve tomorrow too. Every fund-source sale is
one EXIT per LOT with a ``lot_id`` — an EXIT with a quantity and no lot_id is a silent no-op
(engine/overrides.py). ETF units bought outside the run are adopted by
``manager._maybe_adopt_fund_holding`` (it reads ``fund_source``), so they are sellable.
"""

from __future__ import annotations

from datetime import date
from math import ceil
from typing import Any

from skas_algo.engine.types import Signal, SignalAction
from skas_algo.live.holidays import next_trading_day

FUNDING_MODES = ("ledger", "on_demand", "park")


class EntryFundingMixin:
    """Host contract: call ``_init_funding`` from the ctor, ``_settle`` at the top of a
    decision, ``_want`` for every buy, ``_credit`` for every sale, then ``_fund_signals``
    to get the fund-source legs to splice around the stock signals, and merge
    ``funding_state``/``load_funding_state`` into (de)serialize."""

    def _init_funding(
        self,
        funding: str = "ledger",
        fund_source: str | None = None,
        float_parts: float = 1.0,
        settlement_days: int = 1,
        funding_buffer_pct: float = 5.0,
        fund_seed: str = "never",
        fund_size: float | None = None,
        fund_size_cap: bool = False,
    ) -> None:
        self.funding = str(funding or "ledger").lower()
        self.fund_size = float(fund_size) if fund_size else None
        self.fund_size_cap = bool(fund_size_cap)
        if self.funding not in FUNDING_MODES:
            raise ValueError(f"funding must be one of {FUNDING_MODES}, got {funding!r}")
        self.fund_source = (str(fund_source).strip().upper() or None) if fund_source else None
        if self.funding == "park" and not self.fund_source:
            raise ValueError("funding='park' needs a fund_source (the ETF sold to fund entries)")
        self.float_parts = max(0.0, float(float_parts or 0.0))
        self.settlement_days = max(0, int(settlement_days or 0))
        self.funding_buffer_pct = max(0.0, float(funding_buffer_pct or 0.0))
        self.fund_seed = str(fund_seed or "never").lower()
        # ---- persisted state ----
        self.pending_entries: dict[str, dict] = {}   # sym -> {units, price, cost, since}
        self.settled_cash: float | None = None       # derived each decision (see _settle)
        self.pending_credits: list[list] = []        # [[iso_date, amount], …]
        self.adopted_value: float = 0.0              # ETF adopted at cost: no cash moved
        self.seeded = False
        self.strategy_alert: str | None = None
        self._alerted: dict[str, str] = {}
        # ---- transient ----
        self._broker_funds: float | None = None
        self._notify_fn = None
        self._today: date | None = None
        self._spendable = 0.0
        self._shortfall = 0.0
        self._fund_exits: list[Signal] = []
        self._late_buys: list[Signal] = []          # queued buys a same-day sale just funded
        self._park_buy: list[Signal] = []

    # ------------------------------------------------------------- platform hooks
    def set_broker_funds(self, value: float) -> None:
        """Manager push: the account's REAL available balance (live only, ~1/min)."""
        if value is not None and value >= 0:
            self._broker_funds = float(value)

    def set_notify_fn(self, fn) -> None:
        self._notify_fn = fn

    def on_fund_adopted(self, symbol: str, units: float, price: float) -> None:
        """The session adopted ``units`` of the ETF from the broker at ``price`` — no cash
        moved, so the run's cash share is its book cash LESS this from now on."""
        if symbol.upper() == (self.fund_source or "") and units > 0 and price > 0:
            self.adopted_value += float(units) * float(price)

    def _notify_once(self, key: str, today: date, message: str) -> None:
        """Bell/Telegram at most once per day per condition — the decision re-derives every
        day and a queued entry would otherwise nag daily and hourly."""
        if self._alerted.get(key) == today.isoformat():
            return
        self._alerted[key] = today.isoformat()
        if self._notify_fn is not None:
            self._notify_fn(self.fund_source or "funds", message)

    def _alert(self, message: str) -> None:
        self.strategy_alert = (
            message if not self.strategy_alert else f"{self.strategy_alert} · {message}"
        )

    # ------------------------------------------------------------------ the ledger
    @property
    def funds_managed(self) -> bool:
        return self.funding != "ledger"

    def _settle(self, ctx, today: date) -> float:
        """Age pending credits and return what may be spent TODAY. Live, capped at the
        broker's balance — a ceiling on today's spend, never written back."""
        self._today = today
        self.strategy_alert = None
        self._shortfall = 0.0
        self._fund_exits = []
        self._late_buys = []
        self._park_buy = []
        # Settled credits leave the pending list; the engine credited them to the book's
        # cash the day they were raised, so the derived figure below picks them up now.
        self.pending_credits = [
            c for c in self.pending_credits if date.fromisoformat(c[0]) > today
        ]
        # DERIVED, never tallied: the run's own book cash (fills and charges exact) less
        # the ETF it adopted without paying for it, less what is still settling. A tally
        # kept beside the book drifts by every rupee of slippage and charge, and on a
        # shared balance drift IS the sibling run's money.
        self.settled_cash = max(0.0, float(ctx.cash) - self.adopted_value - self._pending_total())
        spendable = self.settled_cash
        if self._broker_funds is not None:
            spendable = min(spendable, self._broker_funds)
        self._spendable = max(0.0, spendable)
        return self._spendable

    def _pending_total(self) -> float:
        return sum(float(c[1]) for c in self.pending_credits)

    def _fund_value(self, book) -> float:
        """This run's fund-source lots AT COST (``book`` = ctx or a portfolio)."""
        if not self.fund_source:
            return 0.0
        try:
            return sum(float(lot.units) * float(lot.price) for lot in (book.lots(self.fund_source) or []))
        except Exception:  # pragma: no cover - a book that cannot price is an empty one
            return 0.0

    def fund_pool(self, book) -> float:
        """What this run holds of its fund: ETF at cost + every other lot at cost + the
        cash ledger (settled + settling). Before the first decision the cash portion is
        taken as capital − ETF, which is what the ledger will be seeded to."""
        etf = self._fund_value(book)
        stocks = 0.0
        try:
            for sym in book.lot_symbols():
                if sym == self.fund_source:
                    continue
                stocks += sum(float(lot.units) * float(lot.price) for lot in book.lots(sym))
        except Exception:  # pragma: no cover
            pass
        book_cash = getattr(book, "cash", None)
        if self.settled_cash is None:
            # No decision yet, so no ledger: the cash share is the float this run is
            # designed to keep (capital − ETF would count the units NOT YET adopted as
            # cash and make the run want nothing before its first decision).
            cash = max(0.0, min(self._float_target_hint(), (self.fund_size or 0.0) - etf))
        elif book_cash is not None:
            # live from the book — settled_cash is the start-of-decision figure and would
            # miss the trades that decision itself made
            cash = max(0.0, float(book_cash) - self.adopted_value)
        else:
            cash = float(self.settled_cash) + self._pending_total()
        return etf + stocks + cash

    def _float_target_hint(self) -> float:
        """The cash the run keeps by design (park: float_parts × one allocation). The host
        overrides it; 0 = the whole fund is meant to sit in the ETF."""
        return 0.0

    def fund_units_wanted(self, book, price: float) -> float | None:
        """How many broker-held fund-source units this run may ADOPT at ``price``: the gap
        between its declared fund size and its pool. None = no cap (no fund size declared,
        or a mode without an ETF), which is the manager's historical behaviour."""
        if (not self.fund_size_cap or self.funding != "park" or not self.fund_size
                or price <= 0):
            return None
        return max(0.0, (self.fund_size - self.fund_pool(book)) / price)

    def funding_status(self, book) -> dict[str, Any]:
        """For the tile: the share this run owns and what it is doing with it."""
        return {
            "funding": self.funding,
            "fund_source": self.fund_source,
            "fund_size": self.fund_size,
            "fund_pool": round(self.fund_pool(book), 2) if self.funds_managed else None,
            "settled_cash": self.settled_cash,
            "settling": round(self._pending_total(), 2),
            "pending_entries": {s: dict(p) for s, p in self.pending_entries.items()},
        }

    def _credit(self, today: date, amount: float) -> float:
        """A sale's proceeds: spendable today only with settlement_days == 0. Returns what
        was added to today's spendable figure."""
        if amount <= 0:
            return 0.0
        if self.settlement_days:
            landing = next_trading_day(today, self.settlement_days)
            self.pending_credits.append([landing.isoformat(), float(amount)])
            return 0.0
        self._spendable += float(amount)     # same-day: the engine credits the book today
        return float(amount)

    def _debit(self, amount: float) -> None:
        """A buy emitted this decision — the engine debits the book when it fills; only
        today's running spendable figure moves here."""
        self._spendable = max(0.0, self._spendable - float(amount))

    # ------------------------------------------------------------------ entries
    def _want(self, sym: str, units: int, price: float, today: date) -> Signal | None:
        """Buy ``units`` of ``sym`` if today's settled cash covers it; else queue it, remember
        the shortfall, and tell the owner (on_demand) or the ETF (park) what is needed."""
        cost = units * price
        if units <= 0 or cost <= 0:
            return None
        if self._spendable >= cost:
            self._debit(cost)
            self.pending_entries.pop(sym, None)
            return Signal(symbol=sym, action=SignalAction.ENTER_LONG, quantity=units)
        first = sym not in self.pending_entries
        self.pending_entries[sym] = {
            "units": int(units), "price": float(price), "cost": float(cost),
            "since": self.pending_entries.get(sym, {}).get("since", today.isoformat()),
        }
        self._shortfall += cost - self._spendable if first else cost
        return None

    def _cancel_pending(self, sym: str, today: date, why: str) -> None:
        if sym in self.pending_entries:
            self.pending_entries.pop(sym, None)
            self._notify_once(f"cancel:{sym}", today,
                              f"{sym}: the queued buy is cancelled — {why}.")

    # ------------------------------------------------------------ fund-source legs
    def _fund_signals(self, ctx, today: date, float_target: float) -> None:
        """After the stock decisions: raise what tomorrow needs from the ETF (park), park
        what is above the float, and say what is missing (on_demand). Results land in
        ``_fund_exits`` / ``_park_buy`` for the host to splice in order."""
        owed = sum(float(p["cost"]) for p in self.pending_entries.values())
        if self.pending_entries:
            names = ", ".join(
                f"{s} ₹{p['cost']:,.0f} ({p['units']} @ {p['price']:,.2f})"
                for s, p in sorted(self.pending_entries.items())
            )
            short = max(0.0, owed - self._spendable)
            self._alert(
                f"WAITING FOR FUNDS — {names}. Settled cash ₹{self._spendable:,.0f}"
                + (f", ₹{self._pending_total():,.0f} settling" if self.pending_credits else "")
                + f"; short ₹{short:,.0f}. Retried every decision while the signal holds."
            )
            if self.funding == "on_demand" and short > 0:
                self._notify_once(
                    "funds_needed", today,
                    f"Add ₹{short:,.0f} to the account: {names}. Settled cash is "
                    f"₹{self._spendable:,.0f}. The buy is retried at each decision while "
                    f"SuperTrend stays green.",
                )
        if self.funding != "park" or not self.fund_source:
            return
        fund = self.fund_source
        try:
            fund_px = float(ctx.close(fund))
        except KeyError:
            self._alert(f"{fund} has no price today — the float cannot be managed")
            return
        if fund_px <= 0:
            return
        fund_lots = list(ctx.lots(fund) or [])
        fund_units = int(sum(lot.units for lot in fund_lots))
        # What tomorrow must have on hand: the queued buys (plus the buffer, so a price that
        # moves overnight still fills) AND the float for the next signal. The buffer is NOT
        # applied to the float: that sold 5% every quiet day and parked it back the next.
        target = owed * (1.0 + self.funding_buffer_pct / 100.0) + float_target
        need = target - self._spendable - self._pending_total()
        if need > 0:
            want = ceil(need / fund_px)
            sell = min(want, fund_units)
            if sell <= 0:
                self._alert(f"FUND DRY — {fund} has nothing left to sell; ₹{need:,.0f} is "
                            f"unfunded until it is topped up in the broker.")
                self._notify_once("fund_dry", today,
                                  f"{fund} is empty — top it up. ₹{need:,.0f} of entries "
                                  f"are waiting on it.")
                return
            if sell < want:
                self._alert(f"{fund} covers ₹{sell * fund_px:,.0f} of the ₹{need:,.0f} needed")
            left = sell
            for lot in fund_lots:                       # one EXIT per LOT, FIFO
                if left <= 0:
                    break
                take = min(left, int(lot.units))
                self._fund_exits.append(Signal(symbol=fund, action=SignalAction.EXIT,
                                               lot_id=lot.id, quantity=take,
                                               reason="fund_source", meta={"tag": "FUND"}))
                left -= take
            if self._credit(today, sell * fund_px) > 0:
                # settlement_days == 0 (the historical same-day model): the sale funds the
                # buys queued a moment ago, in this very decision, after the sale fills
                for sym, pend in list(self.pending_entries.items()):
                    sig = self._want(sym, int(pend["units"]), float(pend["price"]), today)
                    if sig is not None:
                        self._late_buys.append(sig)
            return
        # Nothing owed and cash above the float → park the excess (whole units only).
        excess = self._spendable - float_target
        units = int(excess // fund_px) if excess > 0 else 0
        if units > 0:
            self._debit(units * fund_px)
            self._park_buy.append(Signal(symbol=fund, action=SignalAction.ENTER_LONG,
                                         quantity=units, reason="fund_park",
                                         meta={"tag": "FUND"}))

    def _maybe_seed(self, ctx, float_target: float) -> list[Signal] | None:
        """Backtest bootstrap: a run starts with cash and no ETF, so the first decision parks
        everything above the float. Off by default — a LIVE deploy on an account that
        already holds the ETF must never place this order."""
        if self.funding != "park" or self.fund_seed != "if_empty" or self.seeded:
            return None
        fund = self.fund_source
        if not fund or ctx.lots(fund):
            self.seeded = True
            return None
        self.seeded = True
        try:
            px = float(ctx.close(fund))
        except KeyError:
            return None
        units = int(max(0.0, self._spendable - float_target) // px) if px > 0 else 0
        if units <= 0:
            return None
        self._debit(units * px)
        return [Signal(symbol=fund, action=SignalAction.ENTER_LONG, quantity=units,
                       reason="fund_seed", meta={"tag": "FUND"})]

    # ------------------------------------------------------------ (de)serialize
    def funding_state(self) -> dict[str, Any]:
        return {
            "pending_entries": {s: dict(p) for s, p in self.pending_entries.items()},
            "settled_cash": self.settled_cash,
            "pending_credits": [[str(d), float(a)] for d, a in self.pending_credits],
            "adopted_value": self.adopted_value,
            "seeded": self.seeded,
            "alerted": dict(self._alerted),
            "strategy_alert": self.strategy_alert,
        }

    def load_funding_state(self, state: dict[str, Any]) -> None:
        self.pending_entries = {s: dict(p) for s, p in (state.get("pending_entries") or {}).items()}
        sc = state.get("settled_cash")
        self.settled_cash = None if sc is None else float(sc)
        self.pending_credits = [[str(d), float(a)] for d, a in (state.get("pending_credits") or [])]
        self.adopted_value = float(state.get("adopted_value", 0.0) or 0.0)
        self.seeded = bool(state.get("seeded", False))
        self._alerted = dict(state.get("alerted") or {})
        self.strategy_alert = state.get("strategy_alert")

    def funding_rules(self) -> list[str]:
        if self.funding == "on_demand":
            return ["Funding on demand: a buy the account cannot pay for is queued, you are "
                    "told the rupees to add, and it is retried at each decision while "
                    "SuperTrend stays green (a red flip cancels it)"]
        if self.funding == "park":
            return [f"Funded from {self.fund_source}: keeps {self.float_parts:g} part(s) as "
                    f"settled cash, sells the ETF for what tomorrow needs "
                    f"(T+{self.settlement_days}), parks the excess back after every sale"]
        return []
