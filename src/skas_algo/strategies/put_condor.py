"""put_condor — a monthly LONG put condor with independently switchable adjustments.

On the FIRST TRADING DAY of each month, buy a put condor on that month's monthly expiry
(owner spec, 2026-08-11). At spot 25000 with 200-pt spacing:

    LONG  24800 PE   ← long_hi   (nearest to spot)
    SHORT 24600 PE   ← short_hi
    SHORT 24400 PE   ← short_lo
    LONG  24200 PE   ← long_lo

The expiry payoff is a tent: 0 above ``long_hi``, rising to +spacing at ``short_hi``, flat
across the two shorts, falling back to 0 at ``long_lo``, flat below. No-arbitrage forces a
NET DEBIT, so **max loss = the debit** and **max profit = spacing − debit**. It needs the
index to drift down into the tent by expiry.

Measured on the 1-min store (58 monthly cycles, Aug-2021 → Jul-2026, unadjusted, held to
expiry, 1 lot): 24% hit rate, +₹558/cycle, debit 18-38 pts (~₹1,800), max profit ~170 pts.
A mildly positive lottery — three small losses for one large win. The ADJUSTMENTS are where
the value has to come from, which is why every rule here is a switchable parameter rather
than hard-coded: the backtest sweeps them factorially and the data picks the policy.

Thresholds are a **% of MAX LOSS**, not of broker margin. A defined-risk condor's margin is
tiny live (~₹30k → a 3% stop of ₹900 fires daily) and huge in the replay's shorts-only model
(~₹422k → a 3% stop of ₹12,675 exceeds the ₹11,000 max profit and can never fire). Max loss
is the structure's own unit and is identical in both.

Deploy-only is NOT wired yet — backtest-first (the straddle_btst precedent).
"""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.options.contract_specs import lot_size_for, selection_step
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction
from skas_algo.live.holidays import previous_trading_day
from skas_algo.strategies.delta_neutral_monthly import DeltaNeutralMonthlyStrategy

from ._options_common import bad_close


class PutCondorStrategy(DeltaNeutralMonthlyStrategy):
    strategy_id = "put_condor"
    intraday = True

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,
        # ---- structure ----
        spacing: int = 200,             # points between adjacent strikes
        first_long_offset: int = 0,     # 0 = one spacing below the ATM (the spec's example)
        # ---- entry ----
        entry_time: str = "09:20",
        entry_window_end: str = "15:00",
        force_entry: bool = False,
        # ---- exits, as a % of MAX LOSS (the debit) ----
        target_pct_of_max_loss: float = 100.0,   # +1x the debit
        stop_pct_of_max_loss: float = 0.0,       # 0 = off (the structure is defined-risk)
        hold_to_expiry: bool = False,
        payoff_neg_exit: bool = False,
        # ---- adjustments (each independently switchable for the sweep) ----
        down_breach_action: str = "roll_long",   # none | roll_long | recenter
        long_roll_step: int = 100,
        loss_repair: str = "roll_short_up",      # none | roll_short_up
        repair_trigger_pct: float = 50.0,        # % of max loss
        short_roll_step: int = 100,
        max_adjusts: int = 2,                    # per rule, per cycle
        adjust_cooldown_min: int = 15,
        # ---- cadence / misc ----
        profit_check: str = "tick",
        stop_check: str = "tick",
        adjust_check: str | None = None,
        eod_time: str = "15:20",
        min_leg_oi: int = 1,
        risk_free_rate: float = 0.065,
        lot_overrides: dict | None = None,
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
            adjust_cooldown_min=adjust_cooldown_min,
            ironfly_adjust=False,        # the inherited fly adjustment never applies here
            profit_target_pct=0.0,       # our thresholds are %-of-max-loss, not %-of-margin
            stop_loss_pct=0.0,
            risk_free_rate=risk_free_rate,
            profit_check=profit_check,
            stop_check=stop_check,
            adjust_check=adjust_check,
            eod_time=eod_time,
            min_leg_oi=min_leg_oi,
            lot_overrides=lot_overrides,
        )
        step = selection_step(self.underlying, 100)
        if spacing <= 0 or spacing % step:
            # NIFTY may only SELECT 100-multiples (owner rule), so the spec's "200-250" is
            # 200 or 300 here — 250 is not representable. Snap rather than silently
            # building a condor on strikes the chain view will have filtered away.
            spacing = max(step, round(spacing / step) * step)
        self.spacing = int(spacing)
        self.first_long_offset = int(first_long_offset or self.spacing)
        self.target_pct_of_max_loss = float(target_pct_of_max_loss)
        self.stop_pct_of_max_loss = float(stop_pct_of_max_loss)
        self.hold_to_expiry = bool(hold_to_expiry)
        self.payoff_neg_exit = bool(payoff_neg_exit)
        self.down_breach_action = str(down_breach_action or "none")
        self.long_roll_step = int(long_roll_step)
        self.loss_repair = str(loss_repair or "none")
        self.repair_trigger_pct = float(repair_trigger_pct)
        self.short_roll_step = int(short_roll_step)
        self.max_adjusts = int(max_adjusts)

        # ---- state (persisted) ----
        self.last_session: str | None = None   # last date with a usable chain (entry detector)
        self.entry_max_loss: float = 0.0   # ₹, recorded at entry for reporting
        self.n_long_rolls: int = 0
        self.n_short_rolls: int = 0

    # ------------------------------------------------------------- entry day
    def _is_entry_day(self, ctx, today: date) -> bool:
        """First USABLE session of a new calendar month, judged from the sessions this
        strategy has actually observed — not from the holiday table.

        `live/holidays.py` only carries 2026, so `previous_trading_day` degrades to "previous
        weekday" for earlier years: on 2022-03-02 (the first session after Mahashivratri) a
        calendar test reports "not the first of the month" and March 2022 is silently skipped.
        Muhurat does the same damage from the other side — 2024-11-01 is a captured day whose
        only bars are 18:00-19:00, so it looks like a session, consumes November's slot, and
        November 2024 vanishes. Tracking the last session on which we actually saw a priceable
        chain is holiday-table-free and behaves identically in replay and live.

        Cold start (nothing observed yet) falls back to the calendar so a mid-month deploy
        does not fire on its very first tick.
        """
        if not self.last_session:
            return previous_trading_day(today).month != today.month
        prev = date.fromisoformat(self.last_session)
        return (today.year, today.month) != (prev.year, prev.month)

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()

        live = self._live_legs(ctx)
        if live:
            # Defence in depth against an uncloseable book: if the contract has expired but
            # our legs never cleared, abandon them rather than hold a dead position for the
            # rest of the run. This must precede _manage's print guard — an expired contract
            # has no marks, so _manage would return [] forever and never reach an exit.
            if self.cycle_expiry and today > date.fromisoformat(self.cycle_expiry):
                return self._exit_all(live, "pc_expired_stale")
            self._note_session(ctx, today)
            return self._manage(ctx, live, now)

        if self.phase != "idle":          # a cycle just ended → park until next month
            self.done_expiry = self.cycle_expiry
            self.phase = "idle"
            self.cycle_expiry = None
            self.adjust_count = 0
            self.last_adjust_at = None
            self.adjust_realized = 0.0
            self.realized_rolls = 0.0
            self.entry_max_loss = 0.0
            self.n_long_rolls = self.n_short_rolls = 0

        if self.force_pending:
            got = self._try_enter(ctx, now, today)
            if got:
                self.force_pending = False
            return got
        if self.entered_day == today.isoformat():
            return []
        if not (self.entry_time <= now.time() <= self.entry_window_end):
            return []
        # Evaluate the month test BEFORE stamping (else it compares today with itself), and
        # stamp only once a real spot proves this is a live in-hours session.
        entry_day = self._is_entry_day(ctx, today)
        if not self._note_session(ctx, today):
            return []                      # no usable spot — not a session; month unchanged
        if not (self.force_entry or entry_day):
            return []
        return self._try_enter(ctx, now, today)

    def _note_session(self, ctx, today: date) -> bool:
        """Record today as an observed session iff a real spot exists. Returns usability."""
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        if spot is None or bad_close(spot):
            return False
        self.last_session = today.isoformat()
        return True

    # ----------------------------------------------------------------- entry
    def _try_enter(self, ctx, now: datetime, today: date) -> list[Signal]:
        expiries = self._listed_expiries(ctx, today)
        expiry = self._current_monthly(expiries, today)
        if expiry is None or expiry.isoformat() == self.done_expiry:
            return []
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(self.underlying, expiry.isoformat()) if chain_fn else None
        if not chain or not chain.get("rows"):
            return []
        rows = {float(r["strike"]): r for r in chain["rows"]}
        spot = chain.get("spot")
        if spot is None or bad_close(spot):
            return []
        # Anchor on the CHAIN's atm_strike, not round(spot/step): the replay's spot is
        # de-carried put-call parity and the live one is the cash index, and those differ by
        # ~20 pts — enough to land the whole tent one strike apart between the two modes.
        # Both paths recompute atm_strike as the nearest surviving 100-multiple, so it is the
        # same code on both sides. (Same failure that moved a straddle strike, 2026-07-16.)
        atm = chain.get("atm_strike")
        step = selection_step(self.underlying, 100)
        if atm is None or bad_close(atm):
            atm = round(float(spot) / step) * step
        atm = float(atm)
        long_hi = atm - self.first_long_offset
        short_hi = long_hi - self.spacing
        short_lo = short_hi - self.spacing
        long_lo = short_lo - self.spacing
        try:
            per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return []
        units = float(self.lots * per_lot)

        spec = [(long_hi, 1), (short_hi, -1), (short_lo, -1), (long_lo, 1)]
        legs = []
        for k, direction in spec:
            row = rows.get(float(k))
            prem = self._ltp((row or {}).get("pe"))
            if prem is None or not self._oi_ok((row or {}).get("pe")):
                return []          # all-or-nothing: never half-enter a condor
            legs.append(self._leg(expiry, k, "PE", direction, units, prem, per_lot))

        self.legs = legs
        self.phase = "condor"
        self.cycle_expiry = expiry.isoformat()
        self.entered_day = today.isoformat()
        self.adjust_count = 0
        self.last_adjust_at = None
        self.adjust_realized = 0.0
        self.n_long_rolls = self.n_short_rolls = 0
        self.entry_max_loss = -self._payoff_min(legs, float(spot))
        self._freeze_margin(ctx, float(spot))
        # LONGS FIRST: signal order is honoured, so a partial fill leaves the book
        # over-hedged rather than short a naked put.
        return [
            Signal(leg["symbol"],
                   SignalAction.ENTER_LONG if leg["dir"] > 0 else SignalAction.ENTER_SHORT,
                   quantity=int(leg["units"]), reason="pc_entry", meta={"multiplier": 1})
            for leg in sorted(legs, key=lambda x: -x["dir"])
        ]

    def _leg(self, expiry, k, right, direction, units, entry, per_lot) -> dict:
        sym = make(self.underlying, expiry, float(k), right,
                   lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
        return {"symbol": sym, "right": right, "dir": direction,
                "units": units, "entry": entry}

    # ------------------------------------------------------------- structure
    def _k(self, leg: dict) -> float:
        return float(leg["symbol"].split("|")[2])

    def _sorted_legs(self) -> list[dict]:
        return sorted(self.legs, key=self._k, reverse=True)   # high strike → low

    def _held_strikes(self, right: str) -> set[float]:
        return {self._k(x) for x in self.legs if x["right"] == right}

    def is_defined_risk(self) -> bool:
        """Do the puts net to zero units? If not, the payoff is UNBOUNDED beyond the outermost
        strike and the grid's min understates the risk by an arbitrary amount — the exact
        shape of the run-#203 naked-leg blow-up. max_loss() refuses to answer when this fails."""
        net = sum(x["dir"] * x["units"] for x in self.legs if x["right"] == "PE")
        return abs(net) < 1e-9

    def max_loss(self, spot: float) -> float:
        """₹ the structure can lose at expiry, recomputed from the CURRENT legs — so it stays
        right after an adjustment has reshaped the book. 0 when the book is not defined-risk
        (the callers treat 0 as "thresholds disarmed" rather than trusting a wrong number)."""
        if not self.legs or not self.is_defined_risk():
            return 0.0
        return max(0.0, -self._payoff_min(self.legs, spot))

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, legs: list[dict], now) -> list[Signal]:
        has_print = getattr(ctx.market, "has_print", None)
        marks: dict[str, float] = {}
        for leg in legs:
            if has_print is not None and not has_print(leg["symbol"]):
                return []                      # stale mark — never judge on it
            try:
                marks[leg["symbol"]] = ctx.close(leg["symbol"])
            except KeyError:
                return []
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        if spot is None or bad_close(spot):
            return []
        spot = float(spot)

        pnl = self.adjust_realized + sum(
            (marks[x["symbol"]] - x["entry"]) * x["units"] * x["dir"] for x in legs)
        ml = self.max_loss(spot) or self.entry_max_loss

        # _due CONSUMES its window, so sample each kind ONCE, after every guard above.
        due_profit = self._due("profit", now)
        due_stop = self._due("stop", now)
        due_adjust = self._due("adjust", now)

        if not self.hold_to_expiry and ml > 0:
            if due_profit and self.target_pct_of_max_loss > 0 \
                    and pnl >= ml * self.target_pct_of_max_loss / 100.0:
                return self._exit_all(legs, "pc_target")
            if due_stop and self.stop_pct_of_max_loss > 0 \
                    and pnl <= -ml * self.stop_pct_of_max_loss / 100.0:
                return self._exit_all(legs, "pc_stop")
        if due_adjust:
            return self._maybe_adjust(ctx, legs, marks, spot, pnl, ml, now)
        return []

    # ----------------------------------------------------------- adjustments
    def _maybe_adjust(self, ctx, legs, marks, spot, pnl, ml, now) -> list[Signal]:
        if self.payoff_neg_exit and self._payoff_max(self.legs, spot) < 0:
            return self._exit_all(legs, "pc_payoff_neg")   # whole payoff under water
        if self._in_cooldown(now):
            return []
        rows = self._chain_rows(ctx, self.cycle_expiry) or {}
        ordered = self._sorted_legs()
        if len(ordered) < 4:
            return []
        long_hi, short_hi, short_lo, _long_lo = ordered

        # RULE A — the market has come down to the upper short. Close the appreciated upper
        # long (banking it, which is what makes the upside lossless) and re-establish a
        # cheaper long below it so the downside stays bounded.
        if (self.down_breach_action != "none" and self.n_long_rolls < self.max_adjusts
                and spot <= self._k(short_hi)):
            if self.down_breach_action == "recenter":
                return self._recenter(rows, marks, now)
            got = self._roll_leg(rows, marks, long_hi,
                                 self._k(long_hi) - self.long_roll_step, spot, now,
                                 "pc_adjust_long")
            if got:
                self.n_long_rolls += 1
                return got

        # RULE C — an adverse move has cost half the max loss. Roll the LOWER short up for a
        # credit, recovering cost at the price of a bounded floor below the lower long.
        if (self.loss_repair == "roll_short_up" and self.n_short_rolls < self.max_adjusts
                and ml > 0 and pnl <= -ml * self.repair_trigger_pct / 100.0):
            got = self._roll_leg(rows, marks, short_lo,
                                 self._k(short_lo) + self.short_roll_step, spot, now,
                                 "pc_adjust_short")
            if got:
                self.n_short_rolls += 1
                return got
        return []

    def _roll_leg(self, rows, marks, leg, new_k, spot, now, reason) -> list[Signal]:
        """Close ``leg`` and reopen the same side at ``new_k``, in ONE slice.

        Refuses a destination the structure already holds on that right: same strike + right
        = same SYMBOL, which merges into the existing position so a later EXIT_ALL closes
        both legs (the run-#203 naked-leg blow-up). With the defaults the new long lands
        between existing strikes, but a user-set step could collide — guard, don't assume.
        """
        new_k = float(round(new_k / selection_step(self.underlying, 100))
                      * selection_step(self.underlying, 100))
        if new_k in self._held_strikes(leg["right"]) or new_k <= 0:
            return []
        row = rows.get(new_k)
        prem = self._ltp((row or {}).get("pe"))
        if prem is None or not self._oi_ok((row or {}).get("pe")):
            return []
        mark = marks.get(leg["symbol"])
        if mark is None:
            return []
        # bank the closed leg on the same decision basis the thresholds use
        self.adjust_realized += (mark - leg["entry"]) * leg["units"] * leg["dir"]
        self.legs = [x for x in self.legs if x["symbol"] != leg["symbol"]]
        per_lot = int(leg["units"] // self.lots) or 1
        expiry = date.fromisoformat(self.cycle_expiry)
        fresh = self._leg(expiry, new_k, leg["right"], leg["dir"], leg["units"], prem, per_lot)
        self.legs.append(fresh)
        self.last_adjust_at = now.isoformat()
        self.adjust_count += 1
        self._refreeze = True   # the book changed — re-take the broker margin on the next push
        return [
            Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason),
            Signal(fresh["symbol"],
                   SignalAction.ENTER_LONG if fresh["dir"] > 0 else SignalAction.ENTER_SHORT,
                   quantity=int(fresh["units"]), reason=reason, meta={"multiplier": 1}),
        ]

    def _recenter(self, rows, marks, now) -> list[Signal]:
        """Roll the WHOLE condor down one spacing, keeping the tent over the market instead of
        merely capping the loss. The alternative down-breach policy in the sweep."""
        out: list[Signal] = []
        for leg in list(self._sorted_legs()):
            new_k = self._k(leg) - self.spacing
            row = rows.get(float(new_k))
            prem = self._ltp((row or {}).get("pe"))
            mark = marks.get(leg["symbol"])
            if prem is None or mark is None or not self._oi_ok((row or {}).get("pe")):
                return []                       # all-or-nothing: a partial recenter is junk
        for leg in list(self._sorted_legs()):
            new_k = float(self._k(leg) - self.spacing)
            mark = marks[leg["symbol"]]
            prem = self._ltp(rows[new_k].get("pe"))
            self.adjust_realized += (mark - leg["entry"]) * leg["units"] * leg["dir"]
            per_lot = int(leg["units"] // self.lots) or 1
            fresh = self._leg(date.fromisoformat(self.cycle_expiry), new_k, leg["right"],
                              leg["dir"], leg["units"], prem, per_lot)
            self.legs = [x for x in self.legs if x["symbol"] != leg["symbol"]]
            self.legs.append(fresh)
            out.append(Signal(leg["symbol"], SignalAction.EXIT_ALL, reason="pc_adjust_long"))
            out.append(Signal(fresh["symbol"],
                              SignalAction.ENTER_LONG if fresh["dir"] > 0
                              else SignalAction.ENTER_SHORT,
                              quantity=int(fresh["units"]), reason="pc_adjust_long",
                              meta={"multiplier": 1}))
        self.n_long_rolls += 1
        self.last_adjust_at = now.isoformat()
        self.adjust_count += 1
        self._refreeze = True
        return out

    # ------------------------------------------------------------------ exit
    def _exit_all(self, live, reason) -> list[Signal]:
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in live]
        self.done_expiry = self.cycle_expiry
        self.legs = []
        self.phase = "idle"
        self.cycle_expiry = None
        self.adjust_realized = 0.0
        self.realized_rolls = 0.0
        self.entry_max_loss = 0.0
        self.n_long_rolls = self.n_short_rolls = 0
        if self.exit_margin_basis == "entry":
            self.margin_base = 0.0
            self.margin_source = ""
        return sigs

    # ------------------------------------------------------------- monitoring
    def exit_amounts(self) -> tuple[float | None, float | None]:
        ml = self.entry_max_loss
        if ml <= 0 or self.hold_to_expiry:
            return None, None
        tgt = ml * self.target_pct_of_max_loss / 100.0 if self.target_pct_of_max_loss > 0 else None
        stp = ml * self.stop_pct_of_max_loss / 100.0 if self.stop_pct_of_max_loss > 0 else None
        return tgt, stp

    def exit_rules(self) -> list[str]:
        rules = [f"Long put condor, {self.spacing}-pt spacing — max loss is the debit paid"]
        if self.hold_to_expiry:
            rules.append("Held to expiry — no profit target or stop")
        else:
            if self.target_pct_of_max_loss > 0:
                rules.append(f"Book at +{self.target_pct_of_max_loss:g}% of max loss "
                             f"({self._cadence_phrase('profit')})")
            if self.stop_pct_of_max_loss > 0:
                rules.append(f"Stop at −{self.stop_pct_of_max_loss:g}% of max loss "
                             f"({self._cadence_phrase('stop')})")
        if self.down_breach_action == "roll_long":
            rules.append(f"On a touch of the upper short: close the upper long, re-buy "
                         f"{self.long_roll_step} pts lower ({self._cadence_phrase('adjust')})")
        elif self.down_breach_action == "recenter":
            rules.append(f"On a touch of the upper short: roll the whole condor down "
                         f"{self.spacing} pts")
        if self.loss_repair == "roll_short_up":
            rules.append(f"At −{self.repair_trigger_pct:g}% of max loss: roll the lower short "
                         f"up {self.short_roll_step} pts for a credit")
        if self.payoff_neg_exit:
            rules.append("Exit if the whole expiry payoff turns negative")
        return rules

    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        spot = getattr(market, "index_spot", lambda _u: None)(self.underlying)
        out = {
            "kind": "condor", "phase": self.phase,
            "underlying": self.underlying, "spot": spot,
            "legs": [dict(x) for x in self.legs],
            "cycle_expiry": self.cycle_expiry,
            "entry_max_loss": round(self.entry_max_loss, 2),
            "adjust_realized": round(self.adjust_realized, 2),
            "long_rolls": self.n_long_rolls, "short_rolls": self.n_short_rolls,
        }
        try:
            if self.legs and spot:
                out["max_loss"] = round(self.max_loss(float(spot)), 2)
                out["max_profit"] = round(self._payoff_max(self.legs, float(spot)), 2)
                # the payoff curve the spec says to adjust off, for the monitor tile
                out["payoff"] = [
                    {"spot": round(s, 2), "expiry_pnl": round(self._payoff_at(self.legs, s), 2)}
                    for s in self._payoff_grid(self.legs, float(spot))
                ]
        except Exception:  # pragma: no cover - monitoring never breaks a snapshot
            pass
        return out

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        st = super().export_state()
        st.update({
            "last_session": self.last_session,
            "entry_max_loss": self.entry_max_loss,
            "n_long_rolls": self.n_long_rolls,
            "n_short_rolls": self.n_short_rolls,
        })
        return st

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        self.last_session = state.get("last_session")
        self.entry_max_loss = float(state.get("entry_max_loss", 0.0) or 0.0)
        self.n_long_rolls = int(state.get("n_long_rolls", 0) or 0)
        self.n_short_rolls = int(state.get("n_short_rolls", 0) or 0)
