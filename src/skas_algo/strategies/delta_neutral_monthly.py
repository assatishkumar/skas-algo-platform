"""delta_neutral_monthly — 18Δ short strangle with premium-rebalance rolls (BANKNIFTY).

Recurring monthly cycle (owner's spec):
  * ENTER on the 2nd trading day after the previous monthly expiry, in the 10:30–15:00
    window at/after ``entry_time`` (~11:00): SELL the ~18-delta PE and CE of the current
    monthly (delta from per-row implied vol off the LIVE chain).
  * ADJUST whenever |CE_ltp − PE_ltp| > 40% of (CE_ltp + PE_ltp): close the CHEAP side
    and re-sell that same side at the strike whose LTP ≈ the expensive side's LTP.
    NOTE the spec's prose says "the tested side is closed" but its own worked example
    (10/10 → CE 15 / PE 6: close the PE, sell a new PE at ~15) closes the CHEAP side —
    the example is authoritative. Strikes never cross: the roll caps at the other
    side's strike (a straddle at most, never ITM past it).
  * Once a straddle forms, BUY hedges at the straddle's breakevens (K ± combined
    premium, snapped to the strike grid) → IRON FLY; adjustments stop.
  * EXIT at profit ≥ 2.5% of margin deployed (``stop_loss_pct`` exists, default 0=off).
    Expiry settlement is the backstop. After any exit → idle until next cycle's entry
    day (deploy once, runs monthly).

Design notes:
- Live-chain-driven throughout (delta solve + premium-matched rolls) → deploy-only with
  a broker quote source; BANKNIFTY has ~no cached chain history, so there is no backtest
  (CLAUDE.md; paper-first like CPRE/momentum_theta).
- ``margin_base`` (the rupee base for target/stop) tracks the BROKER basket margin ONLY
  (owner rule — the model reads ~2× real and distorts the thresholds): the manager pushes
  the throttled Zerodha basket margin via ``set_broker_margin``; the base freezes on the
  first push after entry and re-freezes after every structural change (roll / hedge).
  Until a broker number arrives the target/stop checks WAIT (monitor shows "pending") —
  adjustments and the EOD/settle paths don't depend on it.
- Adjustments are cooldown-gated (default 15 min) so a whippy day can't churn rolls
  every tick; the straddle is the hard floor regardless of count.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for, selection_step
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import ExitCadenceMixin, bad_close, legs_mtm_pnl

# Grid used to SNAP the iron-fly wing/breakeven-hedge strikes (round((K±credit)/step)*step). NIFTY
# routes through selection_step → 100 (owner rule: NIFTY trades round 100s only), so the snapped
# wings land on 100-multiples present in the 100-only-filtered chain (the lookup finds them).
_STRIKE_STEP = {"BANKNIFTY": 100, "NIFTY": selection_step("NIFTY", 50), "SENSEX": 100}
_EXPIRY_CUTOFF = time(15, 30)


def _hhmm(s: str, fallback: time) -> time:
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return fallback


class DeltaNeutralMonthlyStrategy(ExitCadenceMixin):
    strategy_id = "delta_neutral_monthly"
    intraday = True  # ticks every refresh; entry window / adjustments / exits self-gate

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,
        target_delta: float = 0.18,
        entry_time: str = "11:00",
        entry_window_end: str = "15:00",
        entry_days_after_expiry: int = 2,
        force_entry: bool = False,           # deploy-time: enter next window tick, any day
        adjust_threshold_pct: float = 40.0,  # |CE−PE| vs (CE+PE)
        adjust_cooldown_min: int = 15,
        # Post-iron-fly adjustment (default OFF here per §1 — a running delta_neutral deploy is
        # unchanged on recovery; iron_fly_monthly overrides to True; runtime-togglable via
        # set_ironfly_adjust). When a breakeven is breached, sell a naked ~15-20Δ short on the
        # untested side and roll it as it decays; exit all if the expiry payoff goes fully negative.
        ironfly_adjust: bool = False,
        adjust_target_delta: float = 0.175,   # 15-20Δ midpoint for the untested-side sell
        adjust_close_delta: float = 0.10,     # roll the adjustment leg at ≤10Δ ...
        adjust_close_prem_frac: float = 0.25, # ... OR when its LTP ≤ ¼ of its sold premium
        profit_target_pct: float = 2.5,      # % of margin_base
        stop_loss_pct: float = 0.0,          # 0 = off (spec-faithful)
        risk_free_rate: float = 0.065,
        # Two-cadence model (2026-07-18): profit_check samples the TARGET and the
        # ADJUSTMENT dispatch (owner: profit booking and adjustments share a cadence;
        # adjust_cooldown_min composes on top); stop_check samples the SL. "tick" =
        # every call — the pre-cadence behavior (§1); forms default to "1min".
        profit_check: str = "tick",
        stop_check: str = "tick",
        eod_time: str = "15:20",
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "BANKNIFTY")).upper()
        self.lots = max(1, int(lots))
        self.target_delta = float(target_delta)
        self.entry_time = _hhmm(entry_time, time(11, 0))
        self.entry_window_end = _hhmm(entry_window_end, time(15, 0))
        self.entry_days_after_expiry = int(entry_days_after_expiry)
        self.force_entry = bool(force_entry)
        self.adjust_threshold_pct = float(adjust_threshold_pct)
        self.adjust_cooldown_min = int(adjust_cooldown_min)
        self.ironfly_adjust = bool(ironfly_adjust)
        self.adjust_target_delta = float(adjust_target_delta)
        self.adjust_close_delta = float(adjust_close_delta)
        self.adjust_close_prem_frac = float(adjust_close_prem_frac)
        # Whole-percent units (2.5 = 2.5% of margin_base). Deliberately NOT named
        # profit_target_pct/stop_loss_pct on the instance: the generic snapshot
        # introspection treats those as fractions (ratio-family convention) and would
        # display 250% / ₹25L (bit run 203). Constructor kwarg names are unchanged (§1).
        self.target_pct = float(profit_target_pct)
        self.stop_pct = float(stop_loss_pct)
        self.r = float(risk_free_rate)
        self.profit_check = str(profit_check)
        self.stop_check = str(stop_check)
        self.eod_time = str(eod_time)
        self.min_leg_oi = int(min_leg_oi)
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # ---- state (all persisted) ----
        self.legs: list[dict] = []            # [{symbol, right, dir, units, entry}]
        self.phase: str = "idle"              # idle | strangle | straddle | ironfly
        self.cycle_expiry: str | None = None  # ISO expiry of the open cycle
        self.done_expiry: str | None = None   # last completed cycle (no same-month re-entry)
        self.margin_base: float = 0.0
        self.margin_source: str = ""
        self.last_adjust_at: str | None = None
        self.adjust_count: int = 0
        self.entered_day: str | None = None   # entry attempted/made this day (once/day gate)
        self.force_pending: bool = False       # Live-page force entry (persisted)
        self.adjust_symbol: str | None = None  # the active untested-side adjustment short (persisted)
        self.adjust_realized: float = 0.0      # banked P&L from CLOSED adjustment legs (persisted)
        # Latest broker basket margin pushed by the live manager (NOT persisted — it
        # re-arrives within a tick of recovery). margin_base freezes from this only.
        self._broker_margin: float | None = None
        self._refreeze = False  # structural change → re-base on the next push

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return [self.underlying]

    def set_broker_margin(self, value: float) -> None:
        """Manager push: the real broker basket margin for OUR current legs."""
        if value and value > 0:
            self._broker_margin = float(value)

    def strategy_pnl(self, closes: dict) -> float | None:
        """The MTM measure _manage compares against the target/stop: CURRENT legs only,
        decision-entry basis — the banked adjustment credit (adjust_realized) is NOT part
        of the threshold check, so it isn't part of this display either."""
        return legs_mtm_pnl(self.legs, closes)

    def request_force_entry(self) -> str:
        """Live-page 'Force entry now': next tick sells the 18Δ strangle into the current
        monthly, bypassing the entry-day, window and once-per-day gates."""
        self.force_pending = True
        return "next tick sells the 18Δ strangle on the current monthly"

    # ------------------------------------------------------------ cycle math
    def _listed_expiries(self, ctx, today: date) -> list[date]:
        chain = ctx.option_chain()
        if chain is None:
            return []
        try:
            return sorted(date.fromisoformat(str(e)[:10])
                          for e in chain.expiries(self.underlying, today))
        except Exception:  # pragma: no cover - chain hiccup → no entry this tick
            return []

    def _monthly_of(self, expiries: list[date], y: int, m: int) -> date | None:
        month = [e for e in expiries if (e.year, e.month) == (y, m)]
        return max(month) if month else None

    def _current_monthly(self, expiries: list[date], today: date) -> date | None:
        """The nearest month whose LAST listed expiry (the monthly) is still ahead."""
        y, m = today.year, today.month
        for _ in range(3):
            exp = self._monthly_of(expiries, y, m)
            if exp is not None and exp >= today:
                return exp
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None

    def _entry_day(self, prev_expiry: date) -> date:
        """N trading days (weekday walk — holidays degrade by a day) after the expiry."""
        d, added = prev_expiry, 0
        while added < self.entry_days_after_expiry:
            d += timedelta(days=1)
            if d.weekday() < 5:
                added += 1
        return d

    def _is_entry_day(self, ctx, today: date) -> bool:
        if self.force_entry:
            return True
        expiries = self._listed_expiries(ctx, today - timedelta(days=45))
        prev = max((e for e in expiries if e < today), default=None)
        if prev is None:
            return False
        return today == self._entry_day(prev)

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()

        live = self._live_legs(ctx)
        if live:
            return self._manage(ctx, live, now)

        # Flat. A cycle that just ended (target/settle) parks us until the next entry day.
        if self.phase != "idle":
            self.done_expiry = self.cycle_expiry
            self.phase = "idle"
            self.cycle_expiry = None
            self.adjust_count = 0
            self.last_adjust_at = None

        if self.force_pending:
            got = self._try_enter(ctx, now, today)
            if got:
                self.force_pending = False
            return got
        if self.entered_day == today.isoformat():
            return []
        if not (self.entry_time <= now.time() <= self.entry_window_end):
            return []
        if not self._is_entry_day(ctx, today):
            return []
        return self._try_enter(ctx, now, today)

    def _live_legs(self, ctx) -> list[dict]:
        if not self.legs:
            return []
        if not any(ctx.lots(leg["symbol"]) for leg in self.legs):
            self.legs = []  # engine settled/closed everything
            return []
        return self.legs

    # ----------------------------------------------------------------- entry
    def _chain_rows(self, ctx, expiry_iso: str) -> dict[float, dict] | None:
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(self.underlying, expiry_iso) if chain_fn else None
        if not chain or not chain.get("rows"):
            return None
        return {float(r["strike"]): r for r in chain["rows"]}

    def _ltp(self, row: dict | None) -> float | None:
        v = (row or {}).get("ltp")
        return None if v is None or bad_close(v) else float(v)

    def _oi_ok(self, row: dict | None) -> bool:
        return int((row or {}).get("oi") or 0) >= self.min_leg_oi

    def _t_years(self, expiry: date, now: datetime) -> float:
        # Live now() is IST-aware; backtest/test clocks are naive — build the expiry
        # timestamp in the SAME tz-ness or the subtraction raises (bit run 203).
        exp_dt = datetime(expiry.year, expiry.month, expiry.day,
                          _EXPIRY_CUTOFF.hour, _EXPIRY_CUTOFF.minute, tzinfo=now.tzinfo)
        return max((exp_dt - now).total_seconds(), 0.0) / (365.0 * 86400.0)

    def _leg_delta(self, spot: float, k: float, t: float, right: str,
                   prem: float) -> float | None:
        """Absolute BS delta of a leg, IV solved from its own LTP. None if unsolvable."""
        if t <= 0 or prem is None:
            return None
        iv = bs.implied_vol(prem, spot, k, t, self.r, right)
        if iv is None or iv <= 0:
            return None
        return abs(bs.delta(spot, k, t, self.r, iv, right))

    def _pick_delta_strike(self, rows: dict[float, dict], side: str, spot: float,
                           t: float, target_delta: float | None = None,
                           exclude: set[float] | None = None) -> tuple[float, float] | None:
        """(strike, ltp) whose BS |delta| (IV solved from its own LTP) is nearest
        ``target_delta`` (defaults to ``self.target_delta``). OTM rows only — the target-Δ
        strike is OTM by definition. ``exclude`` strikes are skipped (see _open_untested:
        an untested-side short must never land on a strike the fly already holds, or it MERGES
        into that leg's position and a later roll closes both)."""
        want = self.target_delta if target_delta is None else target_delta
        best = None
        for k, r in rows.items():
            if exclude and k in exclude:
                continue
            if (side == "ce" and k <= spot) or (side == "pe" and k >= spot):
                continue
            leg = r.get(side)
            prem = self._ltp(leg)
            if prem is None or not self._oi_ok(leg):
                continue
            right = "CE" if side == "ce" else "PE"
            d = self._leg_delta(spot, k, t, right, prem)
            if d is None:
                continue
            err = abs(d - want)
            if best is None or err < best[0]:
                best = (err, k, prem)
        return (best[1], best[2]) if best else None

    def _freeze_margin(self, ctx, spot: float) -> None:
        """Mark the rupee base for target/stop as awaiting the BROKER basket margin (the
        only base we track — owner rule; the model reads ~2× real). The manager pushes
        the number via set_broker_margin within ~a tick of the fill; _manage freezes it
        then. spot/ctx kept for signature stability."""
        self._refreeze = True
        if self.margin_source != "broker":
            self.margin_source = "pending"

    def _try_enter(self, ctx, now: datetime, today: date) -> list[Signal]:
        expiries = self._listed_expiries(ctx, today)
        expiry = self._current_monthly(expiries, today)
        if expiry is None or expiry.isoformat() == self.done_expiry:
            return []
        rows = self._chain_rows(ctx, expiry.isoformat())
        if rows is None:
            return []  # chain hiccup — retry within the window
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        if spot is None or bad_close(spot):
            return []
        t = self._t_years(expiry, now)
        ce = self._pick_delta_strike(rows, "ce", float(spot), t)
        pe = self._pick_delta_strike(rows, "pe", float(spot), t)
        if ce is None or pe is None:
            return []
        try:
            per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return []
        units = float(self.lots * per_lot)

        def sym(k: float, right: str) -> str:
            return make(self.underlying, expiry, float(k), right, lot_size=per_lot,
                        lot_overrides=self.lot_overrides).symbol

        self.legs = [
            {"symbol": sym(ce[0], "CE"), "right": "CE", "dir": -1, "units": units,
             "entry": ce[1]},
            {"symbol": sym(pe[0], "PE"), "right": "PE", "dir": -1, "units": units,
             "entry": pe[1]},
        ]
        self.phase = "strangle"
        self.cycle_expiry = expiry.isoformat()
        self.entered_day = today.isoformat()
        self.adjust_count = 0
        self.last_adjust_at = None
        self._freeze_margin(ctx, float(spot))
        return [
            Signal(leg["symbol"], SignalAction.ENTER_SHORT, quantity=int(leg["units"]),
                   reason="dnm_entry", meta={"multiplier": 1})
            for leg in self.legs
        ]

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, live: list[dict], now: datetime) -> list[Signal]:
        # P&L exits first (any phase).
        has_print = getattr(ctx.market, "has_print", None)
        pnl = 0.0
        marks: dict[str, float] = {}
        for leg in live:
            if has_print is not None and not has_print(leg["symbol"]):
                return []  # a stale mark would make the P&L judgement dishonest
            try:
                cur = ctx.close(leg["symbol"])
            except KeyError:
                return []
            marks[leg["symbol"]] = cur
            pnl += (cur - leg["entry"]) * leg["units"] * leg["dir"]
        # Freeze / re-freeze the threshold base from the latest BROKER margin push.
        # (Also upgrades runs recovered with an old "model" base — e.g. run 203.)
        if self._broker_margin and (self._refreeze or self.margin_source != "broker"):
            self.margin_base = self._broker_margin
            self.margin_source = "broker"
            self._refreeze = False
        # Cadence-sampled ONCE, here — after the print/pnl/margin-freeze guards above
        # (mixin rule #1: _due consumes its window; sampling before an early return
        # would eat a stop slot). due_profit gates the target AND the adjustment
        # dispatch below (they share the profit/adjust cadence by design).
        due_profit = self._due("profit", now)
        due_stop = self._due("stop", now)
        if self.margin_source == "broker" and self.margin_base > 0:
            if due_profit and pnl >= self.margin_base * self.target_pct / 100.0:
                return self._exit_all(live, "target")
            if due_stop and self.stop_pct > 0 and pnl <= -self.margin_base * self.stop_pct / 100.0:
                return self._exit_all(live, "stop")

        if not due_profit:
            return []  # adjustments ride the profit/adjust cadence
        # Iron fly: run the post-formation adjustment when enabled (else ride terminal).
        if self.phase == "ironfly":
            if not self.ironfly_adjust or self._in_cooldown(now):
                return []
            return self._adjust_ironfly(ctx, live, marks, now)
        if self.phase != "strangle":
            return []  # other phases → ride to target/settle
        if self._in_cooldown(now):
            return []
        return self._maybe_adjust(ctx, live, marks, now)

    def _in_cooldown(self, now: datetime) -> bool:
        if self.last_adjust_at is None:
            return False
        last = datetime.fromisoformat(self.last_adjust_at)
        # Normalize tz-ness before subtracting (live=aware, backtest/tests=naive).
        if (last.tzinfo is None) != (now.tzinfo is None):
            last = last.replace(tzinfo=now.tzinfo)
        return (now - last).total_seconds() < self.adjust_cooldown_min * 60

    def _maybe_adjust(self, ctx, live: list[dict], marks: dict[str, float],
                      now: datetime) -> list[Signal]:
        ce_leg = next((leg for leg in live if leg["right"] == "CE" and leg["dir"] < 0), None)
        pe_leg = next((leg for leg in live if leg["right"] == "PE" and leg["dir"] < 0), None)
        if ce_leg is None or pe_leg is None:
            return []
        ce_ltp, pe_ltp = marks[ce_leg["symbol"]], marks[pe_leg["symbol"]]
        total = ce_ltp + pe_ltp
        if total <= 0 or abs(ce_ltp - pe_ltp) <= total * self.adjust_threshold_pct / 100.0:
            return []

        cheap, rich = (pe_leg, ce_leg) if pe_ltp < ce_ltp else (ce_leg, pe_leg)
        rich_ltp = marks[rich["symbol"]]
        side = "pe" if cheap["right"] == "PE" else "ce"
        expiry = date.fromisoformat(self.cycle_expiry)  # cycle always set while holding
        rows = self._chain_rows(ctx, self.cycle_expiry)
        if rows is None:
            return []
        rich_strike = float(rich["symbol"].split("|")[2])
        pick = self._match_premium_strike(rows, side, rich_ltp, rich_strike)
        if pick is None:
            return []
        new_k, new_ltp = pick
        cheap_strike = float(cheap["symbol"].split("|")[2])
        if new_k == cheap_strike:
            return []  # nearest match IS the current strike — nothing to roll

        per_lot = int(cheap["units"] // self.lots) or 1
        new_sym = make(self.underlying, expiry, float(new_k), cheap["right"],
                       lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
        signals = [
            Signal(cheap["symbol"], SignalAction.EXIT_ALL, reason="dnm_roll"),
            Signal(new_sym, SignalAction.ENTER_SHORT, quantity=int(cheap["units"]),
                   reason="dnm_roll", meta={"multiplier": 1}),
        ]
        self.legs = [leg for leg in self.legs if leg["symbol"] != cheap["symbol"]]
        self.legs.append({"symbol": new_sym, "right": cheap["right"], "dir": -1,
                          "units": cheap["units"], "entry": new_ltp})
        self.adjust_count += 1
        self.last_adjust_at = now.isoformat()

        if new_k == rich_strike:
            # Straddle formed → hedge both breakevens NOW (same decision) → iron fly.
            combined = rich_ltp + new_ltp
            step = _STRIKE_STEP.get(self.underlying, 100)
            up_k = round((rich_strike + combined) / step) * step
            dn_k = round((rich_strike - combined) / step) * step
            for k, right in ((up_k, "CE"), (dn_k, "PE")):
                row = rows.get(float(k), {}).get(right.lower())
                prem = self._ltp(row) or 0.0
                hedge_sym = make(self.underlying, expiry, float(k), right,
                                 lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
                signals.append(Signal(hedge_sym, SignalAction.ENTER_LONG,
                                      quantity=int(cheap["units"]), reason="dnm_ironfly",
                                      meta={"multiplier": 1}))
                self.legs.append({"symbol": hedge_sym, "right": right, "dir": 1,
                                  "units": cheap["units"], "entry": prem})
            self.phase = "ironfly"
        else:
            self.phase = "strangle"

        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = (spot_fn(self.underlying) if spot_fn else None) or rich_strike
        self._freeze_margin(ctx, float(spot))
        return signals

    def _match_premium_strike(self, rows: dict[float, dict], side: str,
                              target_ltp: float, cap_strike: float):
        """(strike, ltp) on ``side`` whose LTP is nearest ``target_ltp``, hard-capped at
        the other side's strike (puts stay ≤ cap, calls stay ≥ cap — never crossing)."""
        best = None
        for k, r in rows.items():
            if side == "pe" and k > cap_strike:
                continue
            if side == "ce" and k < cap_strike:
                continue
            leg = r.get(side)
            prem = self._ltp(leg)
            if prem is None or not self._oi_ok(leg):
                continue
            err = abs(prem - target_ltp)
            if best is None or err < best[0]:
                best = (err, k, prem)
        return (best[1], best[2]) if best else None

    # ---------------------------------------------------- post-iron-fly adjustment
    def set_ironfly_adjust(self, on: bool) -> str:
        """Live toggle of the post-iron-fly adjustment (persisted via export_state, so it
        survives a restart)."""
        self.ironfly_adjust = bool(on)
        return f"iron-fly adjustment {'ON' if self.ironfly_adjust else 'OFF'}"

    def _ironfly_breakevens(self) -> tuple[float | None, float | None]:
        """(be_lo, be_hi) of the CORE iron fly = short strike K ± net credit (Σ short entries −
        Σ long entries, in points). Excludes the active adjustment leg. (None, None) if the
        core straddle isn't present."""
        shorts = [leg for leg in self.legs if leg["dir"] < 0 and leg["symbol"] != self.adjust_symbol]
        longs = [leg for leg in self.legs if leg["dir"] > 0]
        k_ce = next((float(leg["symbol"].split("|")[2]) for leg in shorts if leg["right"] == "CE"), None)
        k_pe = next((float(leg["symbol"].split("|")[2]) for leg in shorts if leg["right"] == "PE"), None)
        if k_ce is None or k_pe is None:
            return None, None
        k = (k_ce + k_pe) / 2.0  # equal for a straddle; average is robust
        net_credit = sum(leg["entry"] for leg in shorts) - sum(leg["entry"] for leg in longs)
        return k - net_credit, k + net_credit

    def _payoff_max(self, legs: list[dict], spot: float) -> float:
        """Max P&L-at-expiry over the spot grid for the open ``legs`` PLUS the banked credit
        from closed adjustment legs. The payoff is piecewise-linear, so its max sits at a leg
        strike (or a wide endpoint) — evaluating those is exact."""
        strikes = [float(leg["symbol"].split("|")[2]) for leg in legs]
        grid = sorted(set(strikes + [0.5 * spot, spot, 1.5 * spot]))
        best: float | None = None
        for s in grid:
            pnl = self.adjust_realized
            for leg in legs:
                k = float(leg["symbol"].split("|")[2])
                pnl += leg["dir"] * (bs.intrinsic(leg["right"], s, k) - leg["entry"]) * leg["units"]
            if best is None or pnl > best:
                best = pnl
        return best if best is not None else self.adjust_realized

    def _open_untested(self, ctx, rows: dict[float, dict], spot: float, t: float,
                       now: datetime) -> list[Signal]:
        """Sell one naked ~adjust_target_delta short on the UNTESTED side, but only if a
        breakeven is breached (else no-op — spot is back inside the fly)."""
        be_lo, be_hi = self._ironfly_breakevens()
        if be_lo is None:
            return []
        if spot > be_hi:
            side = "pe"          # call side tested → sell the untested PUT
        elif spot < be_lo:
            side = "ce"          # put side tested → sell the untested CALL
        else:
            return []
        # NEVER re-use a strike the fly already holds on this side — the short would merge into
        # that leg's position (same symbol) and a later adjustment-roll would EXIT_ALL the whole
        # merged position, closing the straddle short too (the run-#203 naked-call blow-up).
        right = "CE" if side == "ce" else "PE"
        held = {float(leg["symbol"].split("|")[2]) for leg in self.legs if leg["right"] == right}
        pick = self._pick_delta_strike(rows, side, spot, t, self.adjust_target_delta, exclude=held)
        if pick is None:
            return []
        k, ltp = pick
        right = "CE" if side == "ce" else "PE"
        units = next((leg["units"] for leg in self.legs if leg["dir"] < 0
                      and leg["symbol"] != self.adjust_symbol), float(self.lots))
        per_lot = int(units // self.lots) or 1
        sym = make(self.underlying, date.fromisoformat(self.cycle_expiry), float(k), right,
                   lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
        self.legs.append({"symbol": sym, "right": right, "dir": -1, "units": units, "entry": ltp})
        self.adjust_symbol = sym
        self.last_adjust_at = now.isoformat()
        self.adjust_count += 1
        self._freeze_margin(ctx, spot)
        return [Signal(sym, SignalAction.ENTER_SHORT, quantity=int(units), reason="ifm_adjust",
                       meta={"multiplier": 1})]

    def _adjust_ironfly(self, ctx, live: list[dict], marks: dict[str, float],
                        now: datetime) -> list[Signal]:
        """Repair the iron fly once a breakeven is breached: sell a naked ~15-20Δ short on the
        untested side and harvest/roll it; exit ALL if no expiry outcome stays profitable."""
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        if spot is None or bad_close(spot):
            return []
        spot = float(spot)
        # 1. Safety exit (EVERY tick, not cooldown-gated): if the whole expiry payoff is < 0,
        #    no spot leaves us positive → close everything.
        if self._payoff_max(self.legs, spot) < 0:
            return self._exit_all(live, "ironfly_payoff_neg")
        if self._in_cooldown(now):
            return []
        rows = self._chain_rows(ctx, self.cycle_expiry)
        if rows is None:
            return []
        t = self._t_years(date.fromisoformat(self.cycle_expiry), now)
        # 2. Manage the active untested-side short: roll it once it has decayed.
        if self.adjust_symbol:
            leg = next((leg for leg in self.legs if leg["symbol"] == self.adjust_symbol), None)
            if leg is None:
                self.adjust_symbol = None
            else:
                ltp = marks.get(leg["symbol"])
                d = (self._leg_delta(spot, float(leg["symbol"].split("|")[2]), t,
                                     leg["right"], ltp) if ltp is not None else None)
                decayed = ((d is not None and d <= self.adjust_close_delta)
                           or (ltp is not None and leg["entry"] > 0
                               and ltp <= self.adjust_close_prem_frac * leg["entry"]))
                if not decayed:
                    return []  # hold the adjustment leg
                # close it (bank the harvested credit) → re-sell below if still breached
                self.adjust_realized += (leg["entry"] - (ltp or 0.0)) * leg["units"]
                self.legs = [leg for leg in self.legs if leg["symbol"] != self.adjust_symbol]
                self.adjust_symbol = None
                self.last_adjust_at = now.isoformat()
                self.adjust_count += 1
                self._freeze_margin(ctx, spot)
                close = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason="ifm_adjust_roll")]
                return close + self._open_untested(ctx, rows, spot, t, now)
        # 3. No active adjustment leg → open one if a breakeven is breached.
        return self._open_untested(ctx, rows, spot, t, now)

    def _exit_all(self, live: list[dict], reason: str) -> list[Signal]:
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in live]
        self.done_expiry = self.cycle_expiry
        self.legs = []
        self.phase = "idle"
        self.cycle_expiry = None
        self.adjust_symbol = None
        self.adjust_realized = 0.0
        return sigs

    # ------------------------------------------------------------ snapshot hooks
    def exit_amounts(self) -> tuple[float | None, float | None]:
        """Rupee target/stop for the tile — from the FROZEN broker margin only."""
        if self.margin_source != "broker" or self.margin_base <= 0:
            return None, None
        target = self.margin_base * self.target_pct / 100.0
        stop = self.margin_base * self.stop_pct / 100.0 if self.stop_pct > 0 else None
        return target, stop

    def exit_rules(self) -> list[str]:
        rules = [f"Book profit at +{self.target_pct:g}% of broker margin "
                 f"({self._cadence_phrase('profit')})"]
        if self.stop_pct > 0:
            rules.append(f"Stop out at −{self.stop_pct:g}% of broker margin "
                         f"({self._cadence_phrase('stop')})")
        rules.append(f"Adjust when |CE−PE| > {self.adjust_threshold_pct:g}% of combined "
                     "(cheap side rolls; straddle max → iron fly)")
        if self.ironfly_adjust:
            rules.append("Iron fly: on a breakeven breach, sell ~15-20Δ on the untested side "
                         "and roll it (≤10Δ / ≤¼ premium); exit all if the payoff turns fully "
                         "negative")
        return rules

    # --------------------------------------------------------------- monitor
    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        out: dict = {
            "kind": "delta_neutral",
            "phase": self.phase,
            "legs": [dict(leg) for leg in self.legs],
            "margin_base": self.margin_base,
            "margin_source": self.margin_source,
            "target_amt": self.margin_base * self.target_pct / 100.0,
            "stop_amt": (self.margin_base * self.stop_pct / 100.0)
            if self.stop_pct > 0 else None,
            "adjust_count": self.adjust_count,
            "cycle_expiry": self.cycle_expiry,
            "ironfly_adjust": self.ironfly_adjust,
            "adjust_symbol": self.adjust_symbol,
        }
        try:
            shorts = [leg for leg in self.legs if leg["dir"] < 0]
            if len(shorts) == 2:
                a, b = (market.close(shorts[0]["symbol"]), market.close(shorts[1]["symbol"]))
                if a + b > 0:
                    out["imbalance_pct"] = round(abs(a - b) / (a + b) * 100.0, 1)
                    out["threshold_pct"] = self.adjust_threshold_pct
        except Exception:  # pragma: no cover - monitoring never breaks a snapshot
            pass
        return out

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "legs": [dict(leg) for leg in self.legs],
            "phase": self.phase,
            "cycle_expiry": self.cycle_expiry,
            "done_expiry": self.done_expiry,
            "margin_base": self.margin_base,
            "margin_source": self.margin_source,
            "last_adjust_at": self.last_adjust_at,
            "adjust_count": self.adjust_count,
            "entered_day": self.entered_day,
            "force_pending": self.force_pending,
            # Persist the toggle + adjustment state so a runtime toggle and an in-flight
            # untested-side short survive a restart/recovery.
            "ironfly_adjust": self.ironfly_adjust,
            "adjust_symbol": self.adjust_symbol,
            "adjust_realized": self.adjust_realized,
        }

    def load_state(self, state: dict) -> None:
        self.legs = [dict(leg) for leg in state.get("legs", [])]
        self.phase = state.get("phase", "idle")
        self.cycle_expiry = state.get("cycle_expiry")
        self.done_expiry = state.get("done_expiry")
        self.margin_base = float(state.get("margin_base", 0.0))
        self.margin_source = state.get("margin_source", "")
        if self.margin_source == "model":  # pre-broker-only state → re-base on next push
            self._refreeze = True
        self.last_adjust_at = state.get("last_adjust_at")
        self.adjust_count = int(state.get("adjust_count", 0))
        self.entered_day = state.get("entered_day")
        self.force_pending = bool(state.get("force_pending", False))
        # Overlay the persisted toggle over the constructor default (so a delta_neutral run
        # the owner turned ON stays ON; a fresh deploy keeps its constructor default).
        if "ironfly_adjust" in state:
            self.ironfly_adjust = bool(state.get("ironfly_adjust"))
        self.adjust_symbol = state.get("adjust_symbol")
        self.adjust_realized = float(state.get("adjust_realized", 0.0))
