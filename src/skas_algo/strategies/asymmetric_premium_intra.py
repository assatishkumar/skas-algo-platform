"""asymmetric_premium_intra — intraday short CALL on the current week, short PUT on the next.

Owner's spec sheet ("Intraday Asymmetric Premium Strategy", 2026-08-17). Two short legs from
DIFFERENT expiries, entered 09:30 and always flat by 15:15:

    CALL  — near-ATM on the CURRENT week's expiry
    PUT   — near-ATM on the NEXT week's expiry

The rationale in the sheet: sharp declines happen fast, so keeping the short put a week out
avoids putting it in the same very-near-expiry window as the short call. It is stated there
as a design hypothesis, not a market prediction.

Adjustment (sheet §4) is driven by RELATIVE premium, not by points moved or by delta: when
one leg's premium decays to ``adjust_trigger_ratio`` of the other's, the CHEAP leg is rolled
to the strike on ITS OWN expiry whose premium most nearly matches the RICH leg. Note what
that does — it re-arms the side that just decayed by moving it back toward the money, so it
adds risk rather than hedging. ``max_adjusts`` bounds it.

Stop (sheet §5) is **100 POINTS of combined loss**, day-cumulative (realized + open). The
sheet writes it as "lot size × 100 = ₹6,500", which is the OLD 65 lot; expressing it in
points instead keeps it correct as the lot size changes (75 today, era-true in replay) and
across underlyings. Hitting it exits everything.

Things the sheet leaves open, and what this does about them — all configurable, all
defaulting to the most literal reading:
  * "look for re-entering opportunity if time and situation permits" is not a rule, so
    ``max_reentries`` defaults to 0 (off).
  * no adjustment cap is given; ``max_adjusts`` defaults to 3 rather than unbounded, because
    each adjustment moves a leg back toward a market that is running.
  * "near-ATM" is read as the ATM strike; ``strike_offset_steps`` shifts it if wanted.
Strikes obey the platform's usual NIFTY 100-multiples rule (``selection_step``) — nothing in
this spec needs the 50s, so it takes no carve-out.
"""

from __future__ import annotations

from datetime import date, datetime, time

from skas_algo.engine.options.contract_specs import lot_size_for, selection_step
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import ExitCadenceMixin, bad_close

_LEGS = ("CE", "PE")


def _hhmm(s: str, fallback: time) -> time:
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return fallback


class AsymmetricPremiumIntraStrategy(ExitCadenceMixin):
    strategy_id = "asymmetric_premium_intra"
    intraday = True  # ticks every refresh; the session window and exits self-gate

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,
        entry_time: str = "09:30",
        exit_time: str = "15:15",          # hard square-off, never carried
        strike_offset_steps: int = 0,      # 0 = ATM ("near-ATM" in the sheet)
        # Which expiry the PUT sits on, as an index into the sorted future expiries: 1 =
        # next week (the sheet), 0 = the SAME week as the call — i.e. a plain same-expiry
        # straddle. That makes the sheet's §7 hypothesis ("why not sell the current-week
        # put?") a controlled experiment instead of an assertion: everything else is held
        # constant and only the put's expiry moves.
        put_expiry_offset: int = 1,
        # --- adjustment (sheet §4) ---
        adjust_trigger_ratio: float = 0.5,  # cheap leg ≤ this × rich leg → roll the cheap one
        max_adjusts: int = 3,
        adjust_tolerance_pct: float = 40.0,  # no strike within this of the target → don't roll
        # --- stop (sheet §5) ---
        stop_loss_points: float = 100.0,   # COMBINED, day-cumulative; 0 = off
        max_reentries: int = 0,            # after a stop; the sheet leaves this undefined
        reentry_cutoff: str = "14:30",
        stop_check: str = "tick",
        eod_time: str = "15:10",
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        u = (underlying or (universe or ["NIFTY"])[0] or "NIFTY").upper()
        self.underlying = u
        self.lots = max(1, int(lots))
        self.entry_time = _hhmm(entry_time, time(9, 30))
        self.exit_time = _hhmm(exit_time, time(15, 15))
        self.reentry_cutoff = _hhmm(reentry_cutoff, time(14, 30))
        self.strike_offset_steps = int(strike_offset_steps)
        self.put_expiry_offset = max(0, int(put_expiry_offset))
        self.adjust_trigger_ratio = float(adjust_trigger_ratio)
        self.max_adjusts = max(0, int(max_adjusts))
        self.adjust_tolerance_pct = float(adjust_tolerance_pct)
        self.stop_loss_points = float(stop_loss_points)
        self.max_reentries = max(0, int(max_reentries))
        self.stop_check = str(stop_check)
        self.eod_time = str(eod_time)
        self.min_leg_oi = int(min_leg_oi)
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # ---- per-day state, all persisted ----
        self.legs: dict[str, dict] = {}     # right -> leg dict (at most one per side)
        self.day: str | None = None
        self.realized: float = 0.0          # banked TODAY (rolls + a stopped-out cycle)
        self.adjusts: int = 0
        self.reentries: int = 0
        self.done_for_day: bool = False

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return [self.underlying]

    def strategy_pnl(self, closes: dict) -> float | None:
        total, seen = self.realized, False
        for leg in self.legs.values():
            cur = closes.get(leg["symbol"])
            if cur is None or bad_close(cur):
                continue
            total += (leg["entry"] - float(cur)) * leg["units"]
            seen = True
        return total if (seen or total) else None

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now, today = ctx.now(), ctx.today()
        self._roll_day(today)

        held = {r: lg for r, lg in self.legs.items() if ctx.lots(lg["symbol"])}
        if len(held) != len(self.legs):
            self.legs = held      # engine settled/closed something behind us

        # 1. Hard time exit FIRST — gated on nothing.
        if now.time() >= self.exit_time:
            self.done_for_day = True
            return self._exit_all(ctx, "apx_eod")

        if self.legs:
            return self._manage(ctx, now)
        if self.done_for_day or now.time() < self.entry_time:
            return []
        if self.reentries and now.time() >= self.reentry_cutoff:
            return []
        return self._try_enter(ctx, today)

    def _roll_day(self, today: date) -> None:
        iso = today.isoformat()
        if self.day == iso:
            return
        self.day, self.realized = iso, 0.0
        self.adjusts = self.reentries = 0
        self.done_for_day = False
        self.legs = {}

    # ----------------------------------------------------------------- entry
    def _expiries(self, ctx, today: date) -> tuple[date, date] | None:
        """(call expiry, put expiry) from the sorted future expiries — the call always takes
        the soonest, the put takes ``put_expiry_offset`` steps out (1 = next week)."""
        chain = ctx.option_chain()
        if chain is None:
            return None
        try:
            future = sorted({date.fromisoformat(str(e)[:10])
                             for e in chain.expiries(self.underlying, today)})
        except Exception:  # pragma: no cover - no chain this tick
            return None
        future = [e for e in future if e >= today]
        want = self.put_expiry_offset
        return (future[0], future[want]) if len(future) > want else None

    def _try_enter(self, ctx, today: date) -> list[Signal]:
        """All-or-nothing: BOTH legs or neither. A one-legged version of this strategy is a
        naked directional short, which is not what the sheet describes."""
        exps = self._expiries(ctx, today)
        if exps is None:
            return []
        want = {"CE": exps[0], "PE": exps[1]}     # call current week, put next week
        built: dict[str, dict] = {}
        for right, expiry in want.items():
            chain = self._chain(ctx, expiry)
            if chain is None:
                return []
            atm = self._atm(chain)
            step = selection_step(self.underlying, 100) or 100
            strike = atm + self.strike_offset_steps * step * (1 if right == "CE" else -1)
            leg = self._build(ctx, chain, expiry, right, float(strike))
            if leg is None:
                return []
            built[right] = leg
        self.legs = built
        return [Signal(lg["symbol"], SignalAction.ENTER_SHORT, quantity=int(lg["units"]),
                       reason="apx_entry", meta={"multiplier": 1}) for lg in built.values()]

    def _chain(self, ctx, expiry: date) -> dict | None:
        fn = getattr(ctx.market, "live_chain", None)
        chain = fn(self.underlying, expiry.isoformat()) if fn else None
        if not chain or not chain.get("rows") or chain.get("spot") is None:
            return None
        return chain

    def _atm(self, chain: dict) -> float:
        spot = float(chain["spot"])
        ks = [float(r["strike"]) for r in chain["rows"]]
        return min(ks, key=lambda k: abs(k - spot))

    def _build(self, ctx, chain: dict, expiry: date, right: str, strike: float) -> dict | None:
        row = next((r for r in chain["rows"] if float(r["strike"]) == strike), None)
        q = (row or {}).get(right.lower()) or {}
        prem = q.get("ltp")
        if prem is None or bad_close(prem) or float(prem) <= 0:
            return None
        if int(q.get("oi") or 0) < self.min_leg_oi:
            return None
        per_lot = int(chain.get("lot_size") or 0)
        if per_lot <= 0:
            try:
                per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
            except KeyError:
                return None
        units = float(self.lots * per_lot)
        sym = make(self.underlying, expiry, strike, right, lot_size=per_lot,
                   lot_overrides=self.lot_overrides).symbol
        return {"symbol": sym, "right": right, "strike": strike, "expiry": expiry.isoformat(),
                "entry": float(prem), "units": units, "dir": -1}

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, now: datetime) -> list[Signal]:
        marks = {}
        for right, leg in self.legs.items():
            cur = self._mark(ctx, leg["symbol"])
            if cur is None:
                return []      # a stale leg makes both the stop and the ratio untrustworthy
            marks[right] = cur

        # 2. Combined stop, in POINTS of the day's cumulative loss (sheet §5).
        if self.stop_loss_points > 0:
            pnl = self.realized + sum(
                (self.legs[r]["entry"] - marks[r]) * self.legs[r]["units"] for r in marks)
            per_lot = self.legs[next(iter(self.legs))]["units"] / self.lots
            budget = self.stop_loss_points * per_lot * self.lots
            # Cadence sampled AFTER the readiness guards (mixin rule #1: _due consumes).
            if self._due("stop", now) and pnl <= -budget:
                out = self._exit_all(ctx, "apx_stop")
                if self.reentries < self.max_reentries and now.time() < self.reentry_cutoff:
                    self.reentries += 1
                    return out + self._try_enter(ctx, ctx.today())
                self.done_for_day = True
                return out

        # 3. Relative-premium adjustment (sheet §4).
        return self._maybe_adjust(ctx, marks, now)

    def _maybe_adjust(self, ctx, marks: dict[str, float], now: datetime) -> list[Signal]:
        if self.adjusts >= self.max_adjusts or len(self.legs) < 2:
            return []
        cheap = min(marks, key=lambda r: marks[r])
        rich = "PE" if cheap == "CE" else "CE"
        if marks[rich] <= 0 or marks[cheap] > self.adjust_trigger_ratio * marks[rich]:
            return []

        leg = self.legs[cheap]
        expiry = date.fromisoformat(leg["expiry"])
        chain = self._chain(ctx, expiry)
        if chain is None:
            return []
        target = marks[rich]
        # The strike on the CHEAP leg's own expiry whose premium best matches the RICH leg.
        best = None
        for r in chain["rows"]:
            q = (r.get(cheap.lower()) or {})
            p = q.get("ltp")
            if p is None or bad_close(p) or float(p) <= 0:
                continue
            if int(q.get("oi") or 0) < self.min_leg_oi:
                continue
            k = float(r["strike"])
            # Rank by premium error, then by NEARNESS to where the leg already is. Without
            # the tie-break a flat stretch of the chain makes every strike an equal match
            # and the scan order decides — which sent the roll to the far end of the chain.
            key = (abs(float(p) - target), abs(k - leg["strike"]))
            if best is None or key < best[0]:
                best = (key, k, float(p))
        if best is None or best[0][0] / target * 100.0 > self.adjust_tolerance_pct:
            return []               # nothing close enough — leave the book alone
        if best[1] == leg["strike"]:
            return []               # already there; rolling to itself is pure cost

        new = self._build(ctx, chain, expiry, cheap, best[1])
        if new is None:
            return []
        self.realized += (leg["entry"] - marks[cheap]) * leg["units"]
        self.legs[cheap] = new
        self.adjusts += 1
        # EXIT before ENTER: EXIT_ALL resolves against the PRE-action book, so an entry-first
        # ordering would let the close swallow the freshly opened lot.
        return [
            Signal(leg["symbol"], SignalAction.EXIT_ALL, reason="apx_adjust"),
            Signal(new["symbol"], SignalAction.ENTER_SHORT, quantity=int(new["units"]),
                   reason="apx_adjust_open", meta={"multiplier": 1}),
        ]

    def _exit_all(self, ctx, reason: str) -> list[Signal]:
        out = []
        for leg in self.legs.values():
            cur = self._mark(ctx, leg["symbol"])
            if cur is not None:
                self.realized += (leg["entry"] - cur) * leg["units"]
            out.append(Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason))
        self.legs = {}
        return out

    def _mark(self, ctx, symbol: str) -> float | None:
        has_print = getattr(ctx.market, "has_print", None)
        if has_print is not None and not has_print(symbol):
            return None
        try:
            cur = ctx.close(symbol)
        except KeyError:
            return None
        return None if cur is None or bad_close(cur) else float(cur)

    # ------------------------------------------------------- snapshot hooks
    def exit_rules(self) -> list[str]:
        rules = [
            f"Roll the cheap leg when its premium falls to "
            f"{self.adjust_trigger_ratio:.0%} of the other's — to the strike on its OWN "
            f"expiry that matches the richer premium (max {self.max_adjusts}/day)",
            f"Hard exit {self.exit_time.strftime('%H:%M')} — never carried",
        ]
        if self.stop_loss_points > 0:
            rules.insert(0, f"Stop at {self.stop_loss_points:g} points of COMBINED loss for "
                            f"the day ({self._cadence_phrase('stop')}) — exits everything")
        return rules

    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        return {"kind": "asymmetric_premium_intra", "names": [{
            "name": self.underlying,
            "spot": getattr(market, "index_spot", lambda _u: None)(self.underlying),
            "realized_today": self.realized,
            "adjusts": self.adjusts,
            "reentries": self.reentries,
            "legs": [dict(lg) for lg in self.legs.values()],
        }]}

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {"legs": {r: dict(lg) for r, lg in self.legs.items()},
                "day": self.day, "realized": self.realized, "adjusts": self.adjusts,
                "reentries": self.reentries, "done_for_day": self.done_for_day}

    def load_state(self, state: dict) -> None:
        self.legs = {r: dict(lg) for r, lg in (state.get("legs") or {}).items()}
        self.day = state.get("day")
        self.realized = float(state.get("realized", 0.0))
        self.adjusts = int(state.get("adjusts", 0))
        self.reentries = int(state.get("reentries", 0))
        self.done_for_day = bool(state.get("done_for_day", False))
