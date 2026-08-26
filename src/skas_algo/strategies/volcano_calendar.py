"""volcano_calendar — monthly PE butterfly + CE calendar with a center-credit cap (NIFTY).

The owner's video deck (2026-08-25). Five legs across two monthly expiries, opened once a
month on the LAST FRIDAY at 15:16 IST; the expiry payoff draws two green peaks with a valley
at spot — the "volcano". Per lot-set (6 lots), with ATM = spot rounded to the selection grid:

  * BUY  1  ATM        PE   near monthly     ┐
  * SELL 2  ATM − 400  PE   near monthly     │  the PE butterfly
  * BUY  1  ATM − 800  PE   near monthly     ┘
  * SELL 1  ATM + 200  CE   near monthly     ┐  the CE calendar — ONE shared strike,
  * BUY  1  same K     CE   FAR monthly      ┘  both legs move together

EXPIRIES SKIP THE ENTRY MONTH. Entry on the last Friday of April trades the MAY monthly
(near) against the JUNE monthly (far) — the deck's "monthly & bi-monthly". April's own
expiry (a few days out) is never touched.

THE 4% CENTER-CREDIT RULE (deck band 3-4%; the owner's explicit ask). "Max credit at
middle" = the structure's P&L if spot ends the NEAR expiry exactly where it is now — the
valley between the two peaks. While that exceeds ``max_credit_pct`` of margin, the CE
calendar moves ONE strike (the 100 grid) further out and the payoff is recomputed. The far
CE is alive at the near expiry, so it is BS-priced with the residual month of time and an
IV solved from its OWN quoted premium (near legs are plain intrinsic). An unsolvable IV or
an unpriced strike defers the entry to the next tick — never enter on an unverifiable rule.

MARGIN IS THE MANUAL ANCHOR, BY CONSTRUCTION. The rule needs a margin denominator BEFORE
the order exists, so there is no broker basket margin to read yet — ``margin_per_set``
(owner-measured on the Kite basket calculator, deploy default ₹1.9L) is the only number
available at that moment, and it also feeds the ±2% target/stop (margin_source="manual",
thresholds live from the first tick — no broker-push wait). With margin_per_set=0 the walk
is SKIPPED with a strategy_alert and the strike stays at ATM+200; target/stop then wait for
the broker push as usual.

CYCLE EXIT (owner decision): if neither the +2% target nor the −2% stop fires, close ALL
FIVE legs — the far CE included — on the near-expiry day at ``cycle_exit_time`` (15:15).
The next cycle opens on the following last Friday. A hard time exit: checked FIRST in
_manage and never cadence-gated.

Subclasses DeltaNeutralMonthlyStrategy (the fair_value_calendar precedent): phase stays
"volcano" (∉ {strangle, ironfly}) so the base's delta-adjustment machinery never engages,
while the margin freeze, %-of-margin target/stop/trail, cadences and the serialize spine
are inherited. DEPLOY-ONLY, broker source required — and no backtest: the 1-min store
captures only ≤40-DTE expiries, and the far CE is ~60 DTE at entry, so a store replay
cannot price it (the double_diagonal_calendar call).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for, selection_step
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction
from skas_algo.live.holidays import is_nse_holiday

from ._options_common import bad_close
from .delta_neutral_monthly import _EXPIRY_CUTOFF, DeltaNeutralMonthlyStrategy, _hhmm


def last_trading_friday(y: int, m: int) -> date:
    """The last Friday of (y, m), walked BACK while it lands on a weekend-adjacent holiday.

    No such helper exists anywhere in the repo (grep confirms). The walk uses plain
    day-steps rather than previous_trading_day so a holiday Friday resolves to Thursday —
    the session the market actually trades — not to the Friday before."""
    last = date(y, m, calendar.monthrange(y, m)[1])
    fri = last - timedelta(days=(last.weekday() - 4) % 7)
    while fri.weekday() >= 5 or is_nse_holiday(fri):
        fri -= timedelta(days=1)
    return fri


class VolcanoCalendarStrategy(DeltaNeutralMonthlyStrategy):
    strategy_id = "volcano_calendar"
    intraday = True

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,                       # lot-SETS (1 set = the 5-leg, 6-lot structure)
        margin_per_set: float = 0.0,         # ₹ per set; deploy sends ~190000 (see module doc)
        # --- the structure (deck offsets, in points from ATM) ---
        wing_1: float = 400.0,               # butterfly short strikes: ATM − wing_1
        wing_2: float = 800.0,               # butterfly far wing:      ATM − wing_2
        ce_offset: float = 200.0,            # CE calendar base strike: ATM + ce_offset
        # --- the center-credit cap ---
        max_credit_pct: float = 4.0,         # payoff-at-spot on near expiry, % of margin
        max_ce_shifts: int = 6,              # bound the strike walk (a bad IV must not spin)
        # --- cycle management ---
        entry_time: str = "15:16",           # deck: last Friday, 3:16 PM
        entry_window_end: str = "15:29",     # retry window within the day
        cycle_exit_time: str = "15:15",      # near-expiry-day close-all (owner decision)
        profit_target_pct: float = 2.0,      # deck: 2% on deployed capital
        stop_loss_pct: float = 2.0,          # deck: 2% on deployed capital
        force_entry: bool = False,
        profit_check: str = "tick",
        stop_check: str = "tick",
        pnl_basis: str = "open_legs",        # one shot, no rolls — nothing banked mid-cycle
        exit_margin_basis: str = "entry",
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        risk_free_rate: float = 0.065,
        **_ignored,
    ):
        super().__init__(
            universe=universe,
            initial_capital=initial_capital,
            underlying=(underlying or (universe[0] if universe else "NIFTY")),
            lots=lots,
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
        self.wing_1 = float(wing_1)
        self.wing_2 = float(wing_2)
        self.ce_offset = float(ce_offset)
        self.max_credit_pct = float(max_credit_pct)
        self.max_ce_shifts = max(0, int(max_ce_shifts))
        self.cycle_exit_time = _hhmm(cycle_exit_time, time(15, 15))

        # ---- cycle state (persisted) ----
        self.near_expiry: str | None = None
        self.far_expiry: str | None = None
        self.entered_month: str | None = None  # "YYYY-MM" — one cycle per calendar month
        self.ce_strike: float | None = None    # where the walk landed (reporting)
        self.entry_center_pct: float | None = None  # the accepted center credit (reporting)
        # Self-clearing (re-derived on every entry attempt) — the Live banner reads it.
        self.strategy_alert: str | None = None

    # ------------------------------------------------------------ live hooks
    def request_force_entry(self) -> str:
        self.force_pending = True
        return "next tick builds the volcano (PE butterfly + CE calendar) at current spot"

    def _index_spot(self, ctx) -> float | None:
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        return float(spot) if spot is not None and not bad_close(spot) else None

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()

        live = self._live_legs(ctx)
        if live:
            return self._manage(ctx, live, now)

        # Flat after an engine-side close (settlement) — clear the cycle fields but leave
        # ``entered_month`` ALONE: it is stamped at ENTRY and owns that month. A cycle
        # entered on April's last Friday closes on the MAY expiry (the 26th) — stamping
        # May here would block the May-29 last-Friday entry and silently skip a month.
        # The last-Friday gate already prevents any premature re-entry.
        if self.phase != "idle":
            self.done_expiry = self.cycle_expiry
            self.phase = "idle"
            self.cycle_expiry = None
            self.near_expiry = None
            self.far_expiry = None
            self.ce_strike = None
            self.entry_center_pct = None
            self.peak_pct = 0.0

        if self.force_pending or (self.force_entry and self.entered_month is None):
            got = self._try_enter(ctx, now, today, force=True)
            if got:
                self.force_pending = False
            return got
        if self.entered_day == today.isoformat():
            return []
        if not (self.entry_time <= now.time() <= self.entry_window_end):
            return []
        if self.entered_month == today.strftime("%Y-%m"):
            return []  # one cycle per calendar month
        # Last-Friday gate, >= not == (the fvc catch-up idiom): a Friday missed to a
        # holiday shift or a restart still enters on the next session of the SAME month.
        if today < last_trading_friday(today.year, today.month):
            return []
        return self._try_enter(ctx, now, today)

    # ----------------------------------------------------------------- entry
    def _pick_expiries(self, ctx, today: date) -> tuple[date | None, date | None]:
        """(near, far) = the monthlies of the NEXT month and the one after. The entry
        month's own expiry (days away on a last-Friday entry) is always skipped — the
        deck's example: entry 24 Apr '26 → 26 May (near) + 30 Jun (far)."""
        expiries = self._listed_expiries(ctx, today)
        y1, m1 = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        y2, m2 = (y1 + 1, 1) if m1 == 12 else (y1, m1 + 1)
        return self._monthly_of(expiries, y1, m1), self._monthly_of(expiries, y2, m2)

    def _grid_step(self) -> float:
        return float(selection_step(self.underlying) or 100)

    def _payoff_on_near_expiry(
        self, legs: list[tuple[str, float, int, float, float, bool]],
        s: float, iv_far: float, t_resid: float,
    ) -> float:
        """P&L of candidate legs with spot at ``s`` ON the near expiry. Near legs are
        intrinsic; the far CE still has ``t_resid`` years of life, so it is BS-priced at
        the IV solved from its own entry premium. Deliberately NOT the base's _payoff_at —
        that intrinsics every leg (and seeds with adjust_realized), which would misprice
        the calendar's whole point.

        legs: (right, strike, dir, units, entry, is_far)."""
        pnl = 0.0
        for right, k, direction, units, entry, is_far in legs:
            if is_far:
                val = bs.price(s, k, t_resid, self.r, iv_far, right)
            else:
                val = bs.intrinsic(right, s, k)
            pnl += direction * (val - entry) * units
        return pnl

    def _try_enter(self, ctx, now: datetime, today: date, force: bool = False) -> list[Signal]:
        if not force and self.entered_month == today.strftime("%Y-%m"):
            return []
        self.strategy_alert = None  # re-derived below — the platform's self-clearing idiom
        spot = self._index_spot(ctx)
        if spot is None:
            return []
        near_e, far_e = self._pick_expiries(ctx, today)
        if near_e is None or far_e is None:
            return []
        rows_near = self._chain_rows(ctx, near_e.isoformat())
        rows_far = self._chain_rows(ctx, far_e.isoformat())
        if rows_near is None or rows_far is None:
            return []
        try:
            near_lot = lot_size_for(self.underlying, near_e, overrides=self.lot_overrides)
            far_lot = lot_size_for(self.underlying, far_e, overrides=self.lot_overrides)
        except KeyError:
            return []

        step = self._grid_step()
        atm = round(spot / step) * step

        # ---- the PE butterfly (all near expiry): every leg must price, or defer ----
        pe_strikes = (atm, atm - self.wing_1, atm - self.wing_2)
        pe_prems: list[float] = []
        for k in pe_strikes:
            leg = (rows_near.get(k) or {}).get("pe")
            prem = self._ltp(leg)
            if prem is None or not self._oi_ok(leg):
                return []
            pe_prems.append(prem)

        # ---- the CE calendar strike walk (the 4% center-credit rule) ----
        manual = self._manual_margin()
        t_far_now = self._t_years(far_e, now)
        # Residual life of the far CE at the near expiry — same tz discipline as _t_years.
        near_cut = datetime(near_e.year, near_e.month, near_e.day,
                            _EXPIRY_CUTOFF.hour, _EXPIRY_CUTOFF.minute, tzinfo=now.tzinfo)
        far_cut = datetime(far_e.year, far_e.month, far_e.day,
                           _EXPIRY_CUTOFF.hour, _EXPIRY_CUTOFF.minute, tzinfo=now.tzinfo)
        t_resid = max((far_cut - near_cut).total_seconds(), 0.0) / (365.0 * 86400.0)

        ce_k = atm + self.ce_offset
        center_pct: float | None = None
        for _shift in range(self.max_ce_shifts + 1):
            near_ce = (rows_near.get(ce_k) or {}).get("ce")
            far_ce = (rows_far.get(ce_k) or {}).get("ce")
            near_prem = self._ltp(near_ce)
            far_prem = self._ltp(far_ce)
            if (near_prem is None or far_prem is None
                    or not self._oi_ok(near_ce) or not self._oi_ok(far_ce)):
                return []  # the candidate strike must price on BOTH expiries → retry
            if manual <= 0:
                # No denominator exists before the order does (broker margin is pushed
                # only after a fill) — the rule cannot run. Say so once, take the base
                # strike; target/stop wait for the broker push as usual.
                self.strategy_alert = (
                    "center-credit rule skipped: margin_per_set is 0, and no broker margin "
                    "exists before entry — set margin_per_set to enable the 4% walk"
                )
                break
            iv_far = bs.implied_vol(far_prem, spot, ce_k, t_far_now, self.r, "CE")
            if iv_far is None or iv_far <= 0:
                return []  # never enter on an unverifiable rule → retry next tick
            candidate = [
                ("PE", pe_strikes[0], 1, 1.0, pe_prems[0], False),
                ("PE", pe_strikes[1], -1, 2.0, pe_prems[1], False),
                ("PE", pe_strikes[2], 1, 1.0, pe_prems[2], False),
                ("CE", ce_k, -1, 1.0, near_prem, False),
                ("CE", ce_k, 1, 1.0, far_prem, True),
            ]
            # Per-set payoff vs per-set margin — the lot multiplier cancels, so evaluate
            # one lot-set against margin_per_set directly.
            payoff = self._payoff_on_near_expiry(candidate, spot, iv_far, t_resid) * near_lot
            center_pct = 100.0 * payoff / self.margin_per_set
            if center_pct <= self.max_credit_pct:
                break
            ce_k += step
        else:
            # Walked max_ce_shifts strikes and the center is still too rich — abnormal
            # (an IV spike); defer rather than enter a shape the deck never described.
            self.strategy_alert = (
                f"entry deferred: center credit still >{self.max_credit_pct:g}% after "
                f"{self.max_ce_shifts} CE shifts (last {center_pct:.1f}% at {ce_k:g})"
            )
            return []

        # re-read the accepted strike's premiums (the loop's locals hold them)
        near_prem = self._ltp((rows_near.get(ce_k) or {}).get("ce"))
        far_prem = self._ltp((rows_far.get(ce_k) or {}).get("ce"))
        if near_prem is None or far_prem is None:
            return []

        n = max(1, int(self.lots))
        legs = [
            self._leg(near_e, pe_strikes[0], "PE", 1, float(n * near_lot), pe_prems[0], near_lot),
            self._leg(near_e, pe_strikes[1], "PE", -1, float(2 * n * near_lot), pe_prems[1], near_lot),
            self._leg(near_e, pe_strikes[2], "PE", 1, float(n * near_lot), pe_prems[2], near_lot),
            self._leg(near_e, ce_k, "CE", -1, float(n * near_lot), near_prem, near_lot),
            self._leg(far_e, ce_k, "CE", 1, float(n * far_lot), far_prem, far_lot),
        ]

        self.legs = legs
        self.phase = "volcano"  # ∉ {strangle, ironfly} → base adjustments stay off
        self.near_expiry = near_e.isoformat()
        self.far_expiry = far_e.isoformat()
        self.cycle_expiry = near_e.isoformat()  # the cycle's terminal date
        self.ce_strike = float(ce_k)
        self.entry_center_pct = round(center_pct, 2) if center_pct is not None else None
        self.entered_day = today.isoformat()
        self.entered_month = today.strftime("%Y-%m")
        self._freeze_margin(ctx, spot)  # no-op with a manual anchor; arms the push else
        return [
            Signal(
                leg["symbol"],
                SignalAction.ENTER_SHORT if leg["dir"] < 0 else SignalAction.ENTER_LONG,
                quantity=int(leg["units"]),
                reason="volcano_entry",
                meta={"multiplier": 1},
            )
            for leg in legs
        ]

    def _leg(
        self, expiry: date, k: float, right: str, direction: int, units: float,
        entry: float, per_lot: int,
    ) -> dict:
        sym = make(
            self.underlying, expiry, float(k), right,
            lot_size=per_lot, lot_overrides=self.lot_overrides,
        ).symbol
        return {"symbol": sym, "right": right, "dir": direction, "units": units, "entry": entry}

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, live: list[dict], now: datetime) -> list[Signal]:
        # The near-expiry close-all comes FIRST — a hard time exit is never cadence-gated
        # and never waits on a pending margin (the platform's cadence rule).
        if self.near_expiry:
            today = ctx.today()
            ne = date.fromisoformat(self.near_expiry)
            if today > ne:
                # Past the near expiry and still holding (missed exit / restart) — the
                # short legs are settling or settled; get flat NOW.
                return self._exit_all(live, "volcano_cycle_end_late")
            if today == ne and now.time() >= self.cycle_exit_time:
                return self._exit_all(live, "volcano_cycle_end")
        # No entered_month stamp on ANY close — entry owns the month latch (see on_slice).
        return super()._manage(ctx, live, now)

    # ------------------------------------------------------------ snapshot hooks
    def exit_rules(self) -> list[str]:
        mlabel = ("margin_per_set × lots" if self.margin_per_set > 0
                  else ("entry margin" if self.exit_margin_basis == "entry" else "broker margin"))
        return [
            f"Book profit at +{self.target_pct:g}% of {mlabel} "
            f"({self._cadence_phrase('profit')})",
            f"Stop out at −{self.stop_pct:g}% of {mlabel} "
            f"({self._cadence_phrase('stop')})",
            f"Neither by the near monthly's expiry → close ALL legs (far CE included) at "
            f"{self.cycle_exit_time.strftime('%H:%M')} on expiry day",
            "After any exit: flat until the next month's last Friday, "
            f"{self.entry_time.strftime('%H:%M')}",
        ]

    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        out = super().basket_status(market, portfolio, margin)
        out.update(
            {
                "kind": "volcano_calendar",
                "phase": "volcano" if self.legs else self.phase,
                "near_expiry": self.near_expiry,
                "far_expiry": self.far_expiry,
                "ce_strike": self.ce_strike,
                "entry_center_pct": self.entry_center_pct,
            }
        )
        return out

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        state = super().export_state()
        state.update(
            {
                "near_expiry": self.near_expiry,
                "far_expiry": self.far_expiry,
                "entered_month": self.entered_month,
                "ce_strike": self.ce_strike,
                "entry_center_pct": self.entry_center_pct,
            }
        )
        return state

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        self.near_expiry = state.get("near_expiry")
        self.far_expiry = state.get("far_expiry")
        self.entered_month = state.get("entered_month")
        self.ce_strike = state.get("ce_strike")
        self.entry_center_pct = state.get("entry_center_pct")
