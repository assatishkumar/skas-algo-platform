"""monthly_butterfly — a plain ATM butterfly held for one monthly expiry (owner spec, 2026-09-03).

THE STRUCTURE (per lot-SET, one side only — no CE/PE mix):
  * SELL ``body_lots`` (2) lots ATM
  * BUY  ``wing_lots``  (1) lot  ATM + ``wing_points``
  * BUY  ``wing_lots``  (1) lot  ATM − ``wing_points``
A long butterfly: a small net DEBIT, max loss bounded by that debit, max payoff ≈ the wing
width if spot pins the body at expiry. Defined risk BY CONSTRUCTION, which is the whole
point of it next to this repo's other option books — no single month can run away, so there
is no stop to tune and nothing to adjust.

CYCLE. Enter on the first session AFTER the previous month's expiry (the spec's "prev month
expiry + 1 day"), at ``entry_time``, on the CURRENT month's monthly expiry. Retry every tick
inside the entry window and every following day until a valid setup prices, so a data hole
or a missing wing costs a day, never the month. Exit on the FIRST of:
  * profit ≥ ``profit_target_pct`` of the margin base, or
  * ``exit_time`` on expiry day (the hard exit — never cadence-gated, never waits on margin).
``done_expiry`` then parks the month: one cycle per monthly expiry, whichever way it ended.

NO ADJUSTMENTS AND NO STOP, by design. ``stop_loss_pct`` exists (inherited) and defaults to
0 = off; a butterfly's floor is the debit, so a stop mostly converts a recoverable dip into a
realised loss. ``phase`` stays "butterfly" so the base class's delta-adjustment and iron-fly
machinery never engages.

SIZING AND THRESHOLDS. ``sets`` is the lot-SET count and ``margin_per_set`` the ₹ anchor the
%-target measures against (the owner measured ~₹70,000 per set); 0 falls back to the broker
push exactly like the rest of the family. The target is a % of THAT anchor, not of capital.

Store replay 2021-07-29 → 2026-09-02, 62 monthly cycles, 10 sets at ₹70,000/set, wing 100:

    NIFTY      PE 2/3/4%   338k / 399k / 424k     CE 2/3/4%   545k / 593k / 439k
    BANKNIFTY  PE 2/3%     595k / 760k            CE 2/3%   1,106k / 1,310k

BANKNIFTY CE at a 3% target is the best of them (₹1,310,276, 80.6% win, 2.4% max drawdown,
worst single cycle −₹3,131) and is strongly positive in BOTH the 2021-23 and 2024-26 halves.
Three findings that shaped the defaults. The WEEKLY cadence is worse everywhere (NIFTY CE 3%
falls to ₹122k with a 16.6% drawdown) — a week is not long enough for the index to come back
to the body. Entering LATER to dodge the opening spread costs more than the spread does
(11:00 instead of 09:20: −₹285k on BANKNIFTY, −₹60k on NIFTY). And the TARGET is what makes
it work at all: hold blindly to settlement with no target and the same book loses ₹17k with
a 59% drawdown. SENSEX is not evaluable — the 1-min store holds barely any of it.

Execution is the real risk. Six fills a cycle (three legs, in and out) all near the money,
against an average edge of ~₹21,000 a cycle on the best variant, so measure your own realised
slippage before trusting any of the figures above.

Subclasses DeltaNeutralMonthlyStrategy for the margin freeze/pending wait, the %-of-margin
target, the exit cadences and the serialize spine (the fair_value_calendar precedent).
"""

from __future__ import annotations

from datetime import date, datetime, time

from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close
from .delta_neutral_monthly import DeltaNeutralMonthlyStrategy


def _hhmm(s: str, fallback: time) -> time:
    try:
        h, m = str(s).split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return fallback


class MonthlyButterflyStrategy(DeltaNeutralMonthlyStrategy):
    strategy_id = "monthly_butterfly"
    intraday = True

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        sets: int = 1,
        margin_per_set: float = 0.0,  # ₹ per lot-SET; 0 = derive from the broker push
        # --- the structure (per set) ---
        side: str = "pe",  # pe | ce — one side only; the spec does not mix them
        cycle: str = "monthly",  # monthly = the month's monthly expiry; weekly = the next expiry
        wing_points: float = 100.0,
        body_lots: int = 2,
        wing_lots: int = 1,
        # --- cycle ---
        entry_time: str = "09:20",
        entry_window_end: str = "15:00",
        exit_time: str = "15:15",  # hard exit on expiry day
        profit_target_pct: float = 3.0,  # % of the margin base
        stop_loss_pct: float = 0.0,  # 0 = off (spec gives no stop; the debit is the floor)
        force_entry: bool = False,
        profit_check: str = "tick",
        stop_check: str = "tick",
        pnl_basis: str = "total",
        exit_margin_basis: str = "entry",
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        risk_free_rate: float = 0.065,
        # How far (%) a real order may be pushed THROUGH the touch when it does not fill at
        # the touch. The platform default (3%, SKAS_LIVE_ORDER_PROTECT_PCT) is a square-off
        # setting: on a ₹300 body that is ₹9 a unit, ₹12,600 across a 20-set order. This
        # entry has a five-hour window and can wait a day, so it should never chase. None =
        # the platform default; the DEPLOY sets a tight rung. Read by the manager at
        # LiveBroker injection — deploy-level, not hot-editable (stop + redeploy).
        order_protect_pct: float | None = None,
        **_ignored,
    ):
        super().__init__(
            universe=universe,
            initial_capital=initial_capital,
            underlying=(underlying or (universe[0] if universe else "NIFTY")),
            lots=sets,
            entry_time=entry_time,
            entry_window_end=entry_window_end,
            force_entry=force_entry,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            risk_free_rate=risk_free_rate,
            profit_check=profit_check,
            stop_check=stop_check,
            pnl_basis=pnl_basis,
            exit_margin_basis=exit_margin_basis,
            margin_per_set=margin_per_set,
            min_leg_oi=min_leg_oi,
            lot_overrides=lot_overrides,
        )
        self.sets = max(1, int(sets))
        self.side = str(side or "pe").lower()
        self.cycle = "weekly" if str(cycle or "monthly").lower() == "weekly" else "monthly"
        self.wing_points = float(wing_points)
        self.body_lots = max(1, int(body_lots))
        self.wing_lots = max(1, int(wing_lots))
        self.exit_time = _hhmm(exit_time, time(15, 15))
        self.order_protect_pct = (None if order_protect_pct is None
                                  else max(0.0, float(order_protect_pct)))
        # Replay-harness sizing hint: SHORT lots per lot-set, so margin_per_lot is read as
        # the ₹ for one set's short body (the family convention).
        self.sell_lots = self.body_lots
        self.entry_spot: float | None = None
        self.body_strike: float | None = None

    # ------------------------------------------------------------------ util
    def _size_multiple(self) -> int:
        """Lot-SETS, not the base class's ``lots`` — a manual ``margin_per_set`` scales by
        the number of butterflies held."""
        return max(1, int(self.sets))

    def _index_spot(self, ctx) -> float | None:
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        return float(spot) if spot is not None and not bad_close(spot) else None

    def _has_mark(self, ctx, symbol: str) -> bool:
        has_print = getattr(ctx.market, "has_print", None)
        if has_print is not None and not has_print(symbol):
            return False
        try:
            ctx.close(symbol)
            return True
        except KeyError:
            return False

    def _leg(
        self, expiry: date, k: float, right: str, direction: int, units: float,
        entry: float, per_lot: int,
    ) -> dict:
        sym = make(
            self.underlying, expiry, float(k), right,
            lot_size=per_lot, lot_overrides=self.lot_overrides,
        ).symbol
        return {"symbol": sym, "right": right, "dir": direction, "units": units, "entry": entry}

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()

        live = self._live_legs(ctx)
        if live:
            return self._manage(ctx, live, now)

        # Flat with a cycle still marked open = the engine settled it (held to expiry through
        # a store hole). Park that expiry so the month is done either way.
        if self.phase != "idle":
            self.done_expiry = self.cycle_expiry
            self.phase = "idle"
            self.cycle_expiry = None
            self.legs = []
            self.realized_rolls = 0.0
            self.adjust_realized = 0.0
            self.peak_pct = 0.0

        if self.force_pending or (self.force_entry and self.done_expiry is None):
            got = self._try_enter(ctx, now, today, force=True)
            if got:
                self.force_pending = False
            return got
        if not (self.entry_time <= now.time() <= self.entry_window_end):
            return []
        return self._try_enter(ctx, now, today)

    # ----------------------------------------------------------------- entry
    def _target_expiry(self, ctx, today: date) -> date | None:
        """The expiry this cycle trades: the month's monthly, or simply the next listed
        expiry when ``cycle="weekly"``."""
        expiries = self._listed_expiries(ctx, today)
        if self.cycle == "weekly":
            return min((e for e in expiries if e > today), default=None)
        return self._current_monthly(expiries, today)

    def _try_enter(self, ctx, now: datetime, today: date, force: bool = False) -> list[Signal]:
        exp = self._target_expiry(ctx, today)
        # ``exp > today`` is the spec's "previous expiry + 1 day": on the old expiry's own
        # day the next fly must not open — that session still belongs to the expiring cycle.
        # For the monthly cadence _current_monthly returns THAT date and this blocks it; for
        # the weekly cadence the next expiry is already ahead, so ``done_expiry`` does the
        # same job one line down.
        if exp is None or exp <= today:
            return []
        if not force and self.done_expiry:
            if self.done_expiry == exp.isoformat():
                return []  # one cycle per expiry, however the last one ended
            if today <= date.fromisoformat(self.done_expiry):
                return []  # the session the last cycle expired on is not a new entry day

        spot = self._index_spot(ctx)
        rows = self._chain_rows(ctx, exp.isoformat())
        if spot is None or not rows:
            return []
        body = min(rows, key=lambda k: abs(k - spot))
        up, dn = body + self.wing_points, body - self.wing_points
        right = self.side.upper()
        cells = {k: (rows.get(k) or {}).get(self.side) for k in (body, up, dn)}
        px = {k: self._ltp(c) for k, c in cells.items()}
        if any(v is None for v in px.values()):
            return []  # a wing didn't price today → retry, never a partial butterfly
        if any(not self._oi_ok(cells[k]) for k in cells):
            return []
        try:
            lot = lot_size_for(self.underlying, exp, overrides=self.lot_overrides)
        except KeyError:
            return []

        body_units = float(self.sets * self.body_lots * lot)
        wing_units = float(self.sets * self.wing_lots * lot)
        # WINGS FIRST, BODY LAST — the order is load-bearing live. The executor runs a
        # decision's actions in sequence and a real order that fails ABANDONS the rest of
        # the list (SliceExecutor._run raises; the manager halts). Body first would leave a
        # naked short of body_lots × sets on a wing rejection — the one exposure this
        # structure exists to rule out. Wings first leaves, at worst, two long options whose
        # entire risk is the premium paid. Same cost either way when everything fills.
        legs = [
            self._leg(exp, up, right, 1, wing_units, px[up], lot),
            self._leg(exp, dn, right, 1, wing_units, px[dn], lot),
            self._leg(exp, body, right, -1, body_units, px[body], lot),
        ]
        self.legs = legs
        self.phase = "butterfly"  # never "strangle"/"ironfly" → base adjustments stay off
        self.cycle_expiry = exp.isoformat()
        self.entry_spot = round(spot, 2)
        self.body_strike = body
        self._freeze_margin(ctx, spot)
        return [
            Signal(
                leg["symbol"],
                SignalAction.ENTER_SHORT if leg["dir"] < 0 else SignalAction.ENTER_LONG,
                quantity=int(leg["units"]),
                reason="mbf_entry",
                meta={"multiplier": 1},
            )
            for leg in legs
        ]

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, live: list[dict], now: datetime) -> list[Signal]:
        live = self._adopt_settled(ctx)
        if not live:
            return []  # everything settled → the flat branch parks the expiry next slice
        # HARD time exit, checked FIRST and never cadence-gated: on expiry day the position
        # must come off whether or not a profit sample is due and whether or not the margin
        # base has been pushed yet.
        if self.cycle_expiry:
            exp = date.fromisoformat(self.cycle_expiry)
            if ctx.today() >= exp and now.time() >= self.exit_time:
                # Only close what still PRINTS. When the expiry session itself is missing
                # from the data (2023-06-29 is such a hole) the contracts are already past
                # expiry and unfillable, so an order there is pure noise — settlement banks
                # them at intrinsic instead and _adopt_settled picks it up next slice.
                if all(self._has_mark(ctx, leg["symbol"]) for leg in live):
                    return self._exit_all(live, "mbf_expiry")
                return []
        return super()._manage(ctx, live, now)

    def _adopt_settled(self, ctx) -> list[dict]:
        """Legs the engine already settled (a store hole over expiry) vanish from the
        portfolio. Bank them at intrinsic-vs-spot and drop them from ``legs``, or the base
        _manage's has-print gate defers every decision forever on a dead symbol."""
        gone = [leg for leg in self.legs if not ctx.lots(leg["symbol"])]
        if not gone:
            return self.legs
        spot = self._index_spot(ctx)
        for leg in gone:
            k = float(leg["symbol"].split("|")[2])
            intr = 0.0
            if spot is not None:
                intr = max(0.0, k - spot) if leg["right"] == "PE" else max(0.0, spot - k)
            self.realized_rolls += (intr - leg["entry"]) * leg["units"] * leg["dir"]
        self.legs = [leg for leg in self.legs if ctx.lots(leg["symbol"])]
        return self.legs

    def _exit_all(self, live: list[dict], reason: str) -> list[Signal]:
        """BODY FIRST on the way out — the mirror of the entry order. Buying the short body
        back is the leg that removes the open-ended exposure; a wing close that fails after
        it leaves a long option, a body cover that fails after the wings are gone leaves a
        naked short. Exits walk the full escalation ladder so a failure is rarer than on
        entry, but the order costs nothing and decides what a failure leaves behind."""
        ordered = sorted(live, key=lambda leg: 0 if leg["dir"] < 0 else 1)
        return super()._exit_all(ordered, reason)

    # ------------------------------------------------------------ snapshot hooks
    def exit_rules(self) -> list[str]:
        mlabel = self._margin_label()
        return [
            f"Book profit at +{self.target_pct:g}% of {mlabel} on the whole cycle "
            f"({self._cadence_phrase('profit')})",
            f"Otherwise hold to expiry and close at {self.exit_time.strftime('%H:%M')} "
            "on expiry day",
            "No adjustments and no rolls — the debit paid is the floor",
            "After the exit: flat until the next monthly expiry cycle",
        ]

    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        out = super().basket_status(market, portfolio, margin)
        out.update(
            {
                "kind": "monthly_butterfly",
                "phase": "butterfly" if self.legs else self.phase,
                "side": self.side.upper(),
                "body_strike": self.body_strike,
                "wing_points": self.wing_points,
                "entry_spot": self.entry_spot,
            }
        )
        return out

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        state = super().export_state()
        state.update({"entry_spot": self.entry_spot, "body_strike": self.body_strike})
        return state

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        self.entry_spot = state.get("entry_spot")
        self.body_strike = state.get("body_strike")
