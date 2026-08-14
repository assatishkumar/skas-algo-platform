"""intraday_strangle_combo — two-index OTM3 intraday strangle with per-leg re-entry.

Owner's spec deck (2026-08-12). Sell 1 lot OTM3 CE + 1 lot OTM3 PE on the current weekly at
09:16, flat by 15:25, never overnight. The index rotates by weekday across NIFTY and SENSEX:

    Mon NIFTY · Tue BOTH · Wed SENSEX · Thu SENSEX · Fri NIFTY

What makes this unlike anything else here: **the two legs are managed completely
independently.** Each carries a 40% stop and a 70% target *on its own entry premium*, and one
leg exiting NEVER touches the other ("non-negotiable" in the deck). A leg that exits re-enters
immediately at a freshly computed OTM3, capped at 2 SL re-entries AND 2 target re-entries per
leg — separate counters, so a side can legitimately trade up to 1 + 2 + 2 = 5 times a day.

Risk by index (deck, Phase 04): NIFTY carries an overall MTM stop of ``mtm_stop_per_lot``
(₹1,500/lot) — on breach, close that index's book and stop it for the day. SENSEX has none
(0 = off); only the per-leg 40% stops apply there.

Design notes:
- **OTM3 counts LISTING steps, not selection steps** — see ``_LISTING_STEP`` below. This is the
  one place the strategy departs from the platform-wide NIFTY-100 rule, and it is why v1 is
  BACKTEST-ONLY (CLAUDE.md §8 + the plan): the live chain coarsens NIFTY to 100s at
  ``_coarsen_chain``, so a live deploy would silently place 100-step strikes. The replay
  harness force-enables its ``allow_fifty_strikes`` escape hatch for this strategy id.
- **No profit-cadence knob** (the ``weekly_intraday_straddle`` precedent): the per-leg SL/target
  IS the strategy, so it samples every tick. Only the overall MTM stop is cadence-gated.
- The rupee MTM stop is a rare gift: every other options strategy here anchors thresholds to
  broker margin, which reads ~1.5-2× the model margin in replay and makes backtest-vs-live
  stops incomparable. ₹1,500 is ₹1,500 in both.
- ``max_holding``/trailing: none, ever (deck, Phase 02).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from skas_algo.engine.options.contract_specs import expiry_weekday_for, lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import ExitCadenceMixin, bad_close

# Strike granularity to COUNT OTM steps on — the exchange's LISTING grid, deliberately NOT
# contract_specs.selection_step (which coarsens NIFTY to 100s under the owner's 2026-07
# round-strikes rule). The deck's worked example is explicit: spot 25000 → OTM3 PE = 24850,
# i.e. three 50-point steps. Owner-confirmed 2026-08-12, and the reason this strategy is
# backtest-only until a live carve-out is separately agreed.
_LISTING_STEP: dict[str, int] = {"NIFTY": 50, "SENSEX": 100, "BANKNIFTY": 100}

# Weekday → indices traded (Mon=0 … Fri=4). Expiry-anchored: NIFTY expires Tuesday, SENSEX
# Thursday, so each index runs on its expiry day and the run-up, overlapping on Tuesday.
_DEFAULT_SCHEDULE: dict[int, list[str]] = {
    0: ["NIFTY"],
    1: ["NIFTY", "SENSEX"],
    2: ["SENSEX"],
    3: ["SENSEX"],
    4: ["NIFTY"],
}

_RIGHTS = ("CE", "PE")


def _hhmm(s: str, fallback: time) -> time:
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return fallback


def _parse_schedule(raw) -> dict[int, list[str]]:
    """Normalize a weekday→indices map. Params arrive from JSON, so keys may be strings."""
    if not raw:
        return {k: list(v) for k, v in _DEFAULT_SCHEDULE.items()}
    out: dict[int, list[str]] = {}
    for k, v in dict(raw).items():
        try:
            day = int(k)
        except (TypeError, ValueError):
            continue
        names = [str(x).upper() for x in (v if isinstance(v, (list, tuple)) else [v]) if x]
        if names:
            out[day] = names
    return out or {k: list(v) for k, v in _DEFAULT_SCHEDULE.items()}


class IntradayStrangleComboStrategy(ExitCadenceMixin):
    strategy_id = "intraday_strangle_combo"
    intraday = True  # ticks every refresh; the session window + per-leg exits self-gate
    # OTM3 is counted on the exchange's LISTING grid (NIFTY 50s), so the live view must NOT
    # coarsen the chain to 100s for this strategy — see _LISTING_STEP. The manager reads this
    # flag when it builds the options market view; every other strategy keeps the 100s rule.
    needs_listing_grid = True

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlyings: list[str] | None = None,
        day_schedule: dict | None = None,   # weekday(0=Mon) -> [index, …]
        lots: int = 1,
        otm_steps: int = 3,                 # "OTM3" — steps on the LISTING grid
        entry_time: str = "09:16",
        exit_time: str = "15:25",           # hard square-off, never carried
        # Not in the deck (my addition): a re-entry this late has no room to work, and the
        # deck's "immediately" says nothing about a cutoff. Set = exit_time to disable.
        reentry_cutoff: str = "15:00",
        leg_stop_pct: float = 40.0,         # premium ≥ entry × 1.40 → stop that leg
        leg_target_pct: float = 70.0,       # premium ≤ entry × 0.30 → book that leg
        max_sl_reentries: int = 2,
        max_target_reentries: int = 2,
        # What to do when a leg's exit fires but the freshly recomputed OTM3 is the SAME
        # strike it is already on — i.e. there is nothing to reposition TO. Spot has to move
        # half a grid step (25 pts NIFTY, 50 SENSEX) before the strike shifts at all.
        #   "reenter" — the deck as written: book it and re-sell the same strike. Repositions
        #               nothing; the only lasting effect is re-basing the stop to the current
        #               (worse) price, which on a trend re-arms the same losing trade wider.
        #               SENSEX 2026-08-13: three CE attempts in 11 min cost -Rs2,064 where
        #               one stop would have cost -Rs870.
        #   "skip"    — book it, then stay FLAT and armed; enter when the strike moves.
        #   "hold"    — do not exit at all; carry the leg until the strike moves, then roll
        #               there. NOTE this DEFERS the 40% stop: while the strike is pinned the
        #               leg's loss is uncapped, and only the overall MTM stop backstops it
        #               (which is off on SENSEX by the deck) — read the sweep before using it.
        # Default "reenter" (§1 — a recovered deploy is unchanged).
        same_strike_action: str = "reenter",
        # Overall MTM stop in RUPEES PER LOT, per index. NIFTY 1500; SENSEX 0 = off (deck).
        mtm_stop_per_lot: dict | float | None = None,
        stop_check: str = "tick",           # cadence for the MTM stop only (§1 default)
        eod_time: str = "15:20",
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.schedule = _parse_schedule(day_schedule)
        scheduled = [u for names in self.schedule.values() for u in names]
        # Precedence: explicit `underlyings` (the replay harness pins it to the one index it
        # is replaying) → then `universe`, which is how a DERIV DEPLOY passes its underlying
        # (manager builds the strategy with universe=[config.underlying]) → only then the
        # whole schedule. Without the universe step, deploying "…_sensex" and "…_nifty" as
        # two runs gave BOTH of them both indices, so each would have doubled the other's
        # book on every shared day (caught on the 2026-08-13 forward test, before entry).
        self.underlyings = [
            u.upper() for u in (underlyings or universe or list(dict.fromkeys(scheduled)))
        ]

        self.lots = max(1, int(lots))
        self.otm_steps = max(1, int(otm_steps))
        self.entry_time = _hhmm(entry_time, time(9, 16))
        self.exit_time = _hhmm(exit_time, time(15, 25))
        self.reentry_cutoff = _hhmm(reentry_cutoff, time(15, 0))
        self.leg_stop_pct = float(leg_stop_pct)
        self.leg_target_pct = float(leg_target_pct)
        self.max_sl_reentries = max(0, int(max_sl_reentries))
        self.max_target_reentries = max(0, int(max_target_reentries))
        act = str(same_strike_action or "reenter").lower()
        self.same_strike_action = act if act in ("reenter", "skip", "hold") else "reenter"
        if isinstance(mtm_stop_per_lot, dict):
            self.mtm_stop_per_lot = {str(k).upper(): float(v) for k, v in mtm_stop_per_lot.items()}
        elif mtm_stop_per_lot is None:
            self.mtm_stop_per_lot = {"NIFTY": 1500.0, "SENSEX": 0.0}
        else:  # a bare number applies to every index
            self.mtm_stop_per_lot = {u: float(mtm_stop_per_lot) for u in self.underlyings}
        self.stop_check = str(stop_check)
        self.eod_time = str(eod_time)
        self.min_leg_oi = int(min_leg_oi)
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # ---- per-(underlying, right) state, all persisted ----
        # Nothing in here may be read or written across sides: leg independence is the
        # deck's one "non-negotiable", and a shared field is how that quietly breaks.
        self.sides: dict[str, dict[str, dict]] = {
            u: {r: self._fresh_side() for r in _RIGHTS} for u in self.underlyings
        }
        self.day: dict[str, str | None] = {u: None for u in self.underlyings}
        self.realized: dict[str, float] = {u: 0.0 for u in self.underlyings}
        self.stopped_day: dict[str, str | None] = {u: None for u in self.underlyings}

    @staticmethod
    def _fresh_side() -> dict:
        return {"leg": None, "sl_reentries": 0, "tgt_reentries": 0, "closed_for_day": False,
                # a re-entry owed but not yet placed ("sl"/"tgt"), and the strike it must
                # NOT be placed on — both only ever set when skip_same_strike_reentry bites
                "pending": None, "blocked_strike": None}

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return list(self.underlyings)

    def strategy_pnl(self, closes: dict) -> float | None:
        """Today's P&L on the DECISION basis: everything banked today plus the open legs."""
        total = sum(self.realized.values())
        seen = False
        for u in self.underlyings:
            for r in _RIGHTS:
                leg = self.sides[u][r]["leg"]
                if leg is None:
                    continue
                cur = closes.get(leg["symbol"])
                if cur is None or bad_close(cur):
                    continue
                total += (leg["entry"] - float(cur)) * leg["units"]  # short leg
                seen = True
        return total if (seen or total) else None

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now = ctx.now()
        today = ctx.today()
        signals: list[Signal] = []
        # Iterate EVERY underlying, not just today's scheduled ones: a book left open by a
        # schedule edit (or a mid-day param change) must still get its 15:25 square-off.
        for u in self.underlyings:
            self._roll_day(u, today)
            signals += self._run_underlying(ctx, u, now, today)
        return signals

    def _roll_day(self, u: str, today: date) -> None:
        """New session → fresh counters. Legs never survive a day (15:25 is unconditional)."""
        iso = today.isoformat()
        if self.day.get(u) == iso:
            return
        self.day[u] = iso
        self.realized[u] = 0.0
        self.stopped_day[u] = None
        self.sides[u] = {r: self._fresh_side() for r in _RIGHTS}

    def _run_underlying(self, ctx, u: str, now: datetime, today: date) -> list[Signal]:
        sides = self.sides[u]
        open_sides = [r for r in _RIGHTS if sides[r]["leg"] is not None
                      and ctx.lots(sides[r]["leg"]["symbol"])]
        # The engine settled/closed something behind our back → drop the stale record.
        for r in _RIGHTS:
            if sides[r]["leg"] is not None and r not in open_sides:
                sides[r]["leg"] = None

        # 1. Hard time exit — FIRST, and gated on nothing (the intraday_straddle rule).
        if now.time() >= self.exit_time:
            out = self._exit_sides(ctx, u, open_sides, "isc_eod")
            for r in _RIGHTS:
                sides[r]["closed_for_day"] = True
            return out

        # 2. Overall MTM stop for this index (rupees per lot; 0 = off, SENSEX).
        stop = self._exit_sides(ctx, u, open_sides, "isc_mtm_stop") if self._mtm_breached(
            ctx, u, open_sides, now) else []
        if stop:
            self.stopped_day[u] = today.isoformat()
            for r in _RIGHTS:
                sides[r]["closed_for_day"] = True
            return stop
        if self.stopped_day.get(u) == today.isoformat():
            return []

        signals: list[Signal] = []
        for r in _RIGHTS:
            signals += self._run_side(ctx, u, r, now, today)
        return signals

    # ------------------------------------------------------------ one side
    def _run_side(self, ctx, u: str, right: str, now: datetime, today: date) -> list[Signal]:
        """The whole per-leg lifecycle for ONE side. Touches only ``self.sides[u][right]``."""
        side = self.sides[u][right]
        if side["closed_for_day"]:
            return []
        out: list[Signal] = []

        leg = side["leg"]
        if leg is not None:
            cur = self._mark(ctx, leg["symbol"])
            if cur is None:
                return []  # no fresh print — never judge a 40% stop on a stale mark
            hit = None
            if cur >= leg["entry"] * (1 + self.leg_stop_pct / 100.0):
                hit = "sl"
            elif cur <= leg["entry"] * (1 - self.leg_target_pct / 100.0):
                hit = "tgt"
            if hit is None:
                return []
            if self.same_strike_action in ("hold", "skip"):
                want = self._current_otm_strike(ctx, u, right, today)
                if self.same_strike_action == "hold" and want is not None \
                        and want == leg["strike"]:
                    # Nothing to roll TO yet — carry the leg and re-check next tick. The
                    # exit is DEFERRED, not cancelled: the moment the strike moves, the
                    # normal path below books this leg and opens the new one.
                    return []
            # Book it. The two thresholds sit on opposite sides of entry, so one price can
            # never satisfy both — no intra-bar ordering question to resolve.
            self.realized[u] += (leg["entry"] - cur) * leg["units"]
            side["leg"] = None
            out.append(Signal(leg["symbol"], SignalAction.EXIT_ALL,
                              reason=("isc_leg_sl" if hit == "sl" else "isc_leg_target")))
            key = "sl_reentries" if hit == "sl" else "tgt_reentries"
            cap = self.max_sl_reentries if hit == "sl" else self.max_target_reentries
            if side[key] >= cap or now.time() >= self.reentry_cutoff:
                # Budget spent, or too late for a re-entry to have room before the square-off.
                side["closed_for_day"] = True
                return out
            side["pending"] = hit
            side["blocked_strike"] = (leg["strike"]
                                      if self.same_strike_action == "skip" else None)
            # fall through: the re-entry is attempted in THIS slice, so "re-enter
            # immediately" still holds. ORDER IS LOAD-BEARING — the exit Signal is already
            # in `out` and must precede the entry, because EXIT_ALL resolves against the
            # PRE-action book (overrides.py:143): entry-first would have the close swallow
            # the re-opened lot when the strike is unchanged (the run-#203 merge bug).

        # ---- flat: the day's first entry, or a re-entry owed from an earlier exit ----
        pending = side["pending"]
        if pending is None:
            if not self._scheduled(u, today) or now.time() < self.entry_time:
                return out
            if now.time() >= self.reentry_cutoff:
                return out    # deployed mid-session — too late to start the day
        elif now.time() >= self.reentry_cutoff:
            side["closed_for_day"] = True   # the owed re-entry ran out of day
            return out

        got = self._enter_side(ctx, u, right, today, blocked=side["blocked_strike"])
        if got and pending is not None:
            # The budget is spent when the re-entry actually LANDS, not when it is owed —
            # a skipped or unpriceable attempt must not silently burn one.
            side["sl_reentries" if pending == "sl" else "tgt_reentries"] += 1
            side["pending"] = None
            side["blocked_strike"] = None
        return out + got

    def _enter_side(self, ctx, u: str, right: str, today: date,
                    blocked: float | None = None) -> list[Signal]:
        """Sell one OTM3 leg. All-or-nothing: an absent or unpriceable strike SKIPS (and is
        retried next tick) — never substitutes a nearby strike behind the owner's back."""
        expiry = self._nearest_expiry(ctx, u, today)
        if expiry is None:
            return []
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(u, expiry.isoformat()) if chain_fn else None
        if not chain or not chain.get("rows") or chain.get("spot") is None:
            return []
        rows = {float(r["strike"]): r for r in chain["rows"]}
        strike = self._otm_strike(u, float(chain["spot"]), right)
        if blocked is not None and strike == blocked:
            return []   # skip_same_strike_reentry: nothing to reposition to yet — stay flat
                        # and armed; the owed re-entry lands as soon as the strike moves.
        row = rows.get(strike)
        if row is None:
            return []
        quote = (row.get(right.lower()) or {})
        prem = quote.get("ltp")
        if prem is None or bad_close(prem) or float(prem) <= 0:
            return []
        if int(quote.get("oi") or 0) < self.min_leg_oi:
            return []
        per_lot = int(chain.get("lot_size") or 0)
        if per_lot <= 0:
            try:
                per_lot = lot_size_for(u, expiry, overrides=self.lot_overrides)
            except KeyError:
                return []
        units = float(self.lots * per_lot)
        symbol = make(u, expiry, strike, right, lot_size=per_lot,
                      lot_overrides=self.lot_overrides).symbol
        self.sides[u][right]["leg"] = {
            "symbol": symbol, "strike": strike, "right": right,
            "entry": float(prem), "units": units, "dir": -1,
        }
        return [Signal(symbol, SignalAction.ENTER_SHORT, quantity=int(units),
                       reason="isc_entry", meta={"multiplier": 1})]

    def _current_otm_strike(self, ctx, u: str, right: str, today: date) -> float | None:
        """The OTM``otm_steps`` strike right now, or None when no chain is available. Used
        only by the same-strike modes, and only on a tick where an exit actually fired."""
        expiry = self._nearest_expiry(ctx, u, today)
        if expiry is None:
            return None
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(u, expiry.isoformat()) if chain_fn else None
        if not chain or chain.get("spot") is None:
            return None
        return self._otm_strike(u, float(chain["spot"]), right)

    def _otm_strike(self, u: str, spot: float, right: str) -> float:
        """OTM``otm_steps`` on the LISTING grid. Deck's example: NIFTY spot 25000 (step 50,
        3 steps) → PE 24850 / CE 25150. SENSEX (step 100) → ±300."""
        step = _LISTING_STEP.get(u.upper(), 100)
        atm = round(float(spot) / step) * step
        off = self.otm_steps * step
        return float(atm + off if right == "CE" else atm - off)

    # ---------------------------------------------------------------- risk
    def _mtm_breached(self, ctx, u: str, open_sides: list[str], now: datetime) -> bool:
        per_lot = float(self.mtm_stop_per_lot.get(u.upper(), 0.0) or 0.0)
        if per_lot <= 0:  # SENSEX: "not configured" — only the per-leg 40% stops apply
            return False
        if not open_sides:
            return False
        pnl = self.realized.get(u, 0.0)
        for r in open_sides:
            leg = self.sides[u][r]["leg"]
            cur = self._mark(ctx, leg["symbol"])
            if cur is None:
                return False  # a stale leg makes the whole MTM untrustworthy
            pnl += (leg["entry"] - cur) * leg["units"]
        # Cadence sampled AFTER every readiness guard (mixin rule #1: _due CONSUMES its
        # window) and keyed per underlying, so a SENSEX slice can't eat NIFTY's slot.
        if not self._due(f"stop:{u}", now):
            return False
        return pnl <= -(per_lot * self.lots)

    def _exit_sides(self, ctx, u: str, rights: list[str], reason: str) -> list[Signal]:
        """Close the named sides and BANK what they made. Booking here (not just in the
        per-leg path) keeps ``strategy_pnl`` — the number the UI shows and the MTM stop
        compares — honest after an EOD or MTM exit."""
        out: list[Signal] = []
        for r in rights:
            leg = self.sides[u][r]["leg"]
            if leg is None:
                continue
            cur = self._mark(ctx, leg["symbol"])
            if cur is not None:
                self.realized[u] += (leg["entry"] - cur) * leg["units"]
            out.append(Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason))
            self.sides[u][r]["leg"] = None
        return out

    # -------------------------------------------------------------- helpers
    def _mark(self, ctx, symbol: str) -> float | None:
        has_print = getattr(ctx.market, "has_print", None)
        if has_print is not None and not has_print(symbol):
            return None
        try:
            cur = ctx.close(symbol)
        except KeyError:
            return None
        return None if cur is None or bad_close(cur) else float(cur)

    def _scheduled(self, u: str, today: date) -> bool:
        return u.upper() in self.schedule.get(today.weekday(), [])

    def _nearest_expiry(self, ctx, u: str, today: date) -> date | None:
        """Current weekly: nearest listed expiry ≥ today, else the calendar weekday."""
        chain = ctx.option_chain()
        if chain is not None:
            try:
                listed = [date.fromisoformat(str(e)[:10]) for e in chain.expiries(u, today)]
                nearest = min((e for e in listed if e >= today), default=None)
                if nearest is not None:
                    return nearest
            except Exception:  # pragma: no cover - fall through to the calendar
                pass
        wd = expiry_weekday_for(u, today, "weekly")
        if wd is None:
            return None
        return today + timedelta(days=(wd - today.weekday()) % 7)

    # ------------------------------------------------------- snapshot hooks
    def exit_rules(self) -> list[str]:
        rules = [
            f"Per leg: stop at +{self.leg_stop_pct:g}% of its entry premium, "
            f"target at −{self.leg_target_pct:g}% (checked every tick)",
            f"Re-enter that leg at fresh OTM{self.otm_steps}: "
            f"max {self.max_sl_reentries} after a stop, {self.max_target_reentries} after a target",
            f"Hard exit {self.exit_time.strftime('%H:%M')} — never carried",
        ]
        live = [f"{u} ₹{v:,.0f}/lot" for u, v in sorted(self.mtm_stop_per_lot.items()) if v > 0]
        if live:
            rules.insert(2, f"Overall MTM stop ({self._cadence_phrase('stop')}): "
                            + ", ".join(live) + " → close that index and stop for the day")
        return rules

    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        today = market.current_date
        names = []
        for u in self.underlyings:
            names.append({
                "name": u,
                "spot": getattr(market, "index_spot", lambda _u: None)(u),
                "scheduled_today": self._scheduled(u, today) if today else False,
                "stopped_today": self.stopped_day.get(u) == (today.isoformat() if today else None),
                "realized_today": self.realized.get(u, 0.0),
                "mtm_stop": (self.mtm_stop_per_lot.get(u, 0.0) or 0.0) * self.lots,
                "sides": {
                    r: {
                        "leg": dict(self.sides[u][r]["leg"]) if self.sides[u][r]["leg"] else None,
                        "sl_reentries": self.sides[u][r]["sl_reentries"],
                        "tgt_reentries": self.sides[u][r]["tgt_reentries"],
                        "closed_for_day": self.sides[u][r]["closed_for_day"],
                    }
                    for r in _RIGHTS
                },
            })
        return {"kind": "intraday_strangle_combo", "names": names}

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "sides": {u: {r: dict(s, leg=(dict(s["leg"]) if s["leg"] else None))
                          for r, s in rights.items()}
                      for u, rights in self.sides.items()},
            "day": dict(self.day),
            "realized": dict(self.realized),
            "stopped_day": dict(self.stopped_day),
        }

    def load_state(self, state: dict) -> None:
        saved = state.get("sides", {}) or {}
        for u in self.underlyings:
            rights = saved.get(u, {}) or {}
            for r in _RIGHTS:
                s = rights.get(r) or {}
                self.sides[u][r] = {
                    "leg": dict(s["leg"]) if s.get("leg") else None,
                    "sl_reentries": int(s.get("sl_reentries", 0)),
                    "tgt_reentries": int(s.get("tgt_reentries", 0)),
                    "closed_for_day": bool(s.get("closed_for_day", False)),
                    # An OWED re-entry must survive a restart. Dropping it would leave the
                    # counters spent but the side looking like it had never traded, so the
                    # flat path would enter again WITHOUT charging a re-entry — free budget.
                    "pending": s.get("pending"),
                    "blocked_strike": (float(s["blocked_strike"])
                                       if s.get("blocked_strike") is not None else None),
                }
            self.day[u] = state.get("day", {}).get(u)
            self.realized[u] = float(state.get("realized", {}).get(u, 0.0))
            self.stopped_day[u] = state.get("stopped_day", {}).get(u)
