"""double_diagonal_calendar — a NIFTY double-diagonal calendar (manual deploy, auto-managed).

The FIRST two-expiry position on the platform. Structure (4 legs):
  * SELL a near-expiry short strangle (~short_target_delta, the NEXT weekly ≥ near_min_dte DTE).
  * BUY a farther-expiry long strangle hedge (~hedge_target_delta, the SUBSEQUENT expiry
    ≥ far_min_dte DTE) — the "calendar/diagonal" hedge whose residual time value at the near
    expiry rounds the payoff tent.

Owner's spec (paraphrased):
  * VIX regime (High ≥20 / Med 13-19 / Low 9-12) sets the *expected* net-premium band
    (High 40-45 / Med 28-35 / Low 22-25 pts) — a DISPLAYED label; strike selection is
    DELTA-FIRST (shorts ~20-25Δ, hedges ~15-20Δ). Low VIX naturally nets to a debit.
  * A manual BIAS knob (up / neutral / down) skews the short/hedge deltas asymmetrically:
    an "up" lean gives the call side room (lower Δ, wider) and tightens the put side
    (higher Δ, richer) so up/sideways drifts pay; "down" mirrors it.
  * ENTER on the deploy (force) or the next entry weekday (~11:00), ONCE — a MANUAL deploy:
    the owner owns the next cycle (recurring=False), so after an exit it sits idle.
  * EXIT at ±exit_pct of the FROZEN broker basket margin (default ±1.5%, inherited machinery).
  * ADJUST: when the UNTESTED (winning) near short decays to ≤ adjust_close_delta OR
    ≤ adjust_close_prem_frac of its sold premium, roll it back toward delta-neutral (target =
    the tested short's current |Δ|) AND drag its far hedge along — never crossing the other
    short (a straddle at most). No adjustments inside min_adjust_dte (3) DTE.
  * NEAR-EXPIRY ROLL-OFF: when the near shorts expire, SQUARE the whole structure (the residual
    far hedges too) — one clean cycle, no re-entry (owner design; the far leg is not left naked).

Design: live-chain-driven (delta solve on TWO chains) → deploy-only, broker source required, NO
backtest (like delta_neutral / iron_fly). Subclasses DeltaNeutralMonthlyStrategy to inherit the
margin freeze / ±%-margin exits / cadence / cooldown / (de)serialize spine; overrides entry,
adjustment, the manage-loop near-expiry guard, and the entry schedule.
"""

from __future__ import annotations

from datetime import date, datetime, time

from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close
from .delta_neutral_monthly import DeltaNeutralMonthlyStrategy, _hhmm

_VIX_SYMBOL = "INDIA VIX"
# Regime → the owner's expected NET-premium band (points). DISPLAY only — selection is delta-first.
_REGIME_PREMIUM = {"high": (40, 45), "medium": (28, 35), "low": (22, 25)}


def _vix_regime(vix: float | None) -> str:
    if vix is None:
        return "unknown"
    if vix >= 20:
        return "high"
    if vix >= 13:
        return "medium"
    return "low"


class DoubleDiagonalCalendarStrategy(DeltaNeutralMonthlyStrategy):
    strategy_id = "double_diagonal_calendar"
    intraday = True

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,
        # --- structure (delta-first) ---
        short_target_delta: float = 0.225,  # near short strangle (20-25Δ)
        hedge_target_delta: float = 0.175,  # far long hedge (15-20Δ)
        near_min_dte: int = 5,
        far_min_dte: int = 10,
        bias: str = "neutral",  # up | neutral | down (manual skew knob)
        bias_skew: float = 0.05,  # Δ shift applied asymmetrically per side
        # --- entry schedule (manual deploy: force at deploy, else next entry weekday) ---
        entry_time: str = "11:00",
        entry_window_end: str = "15:00",
        entry_weekday: int = 0,  # 0 = Monday (owner: enter Monday ~11:00)
        recurring: bool = False,  # deploy-once; owner owns the next cycle
        force_entry: bool = False,
        # --- adjustment ---
        adjust_cooldown_min: int = 15,
        adjust_close_delta: float = 0.10,  # untested short "decayed" at ≤10Δ ...
        adjust_close_prem_frac: float = 0.25,  # ... OR ≤¼ of its sold premium
        min_adjust_dte: int = 3,  # no adjustments inside 3 DTE
        # --- exits (±% of frozen broker margin; inherited machinery) ---
        profit_target_pct: float = 1.5,
        stop_loss_pct: float = 1.5,
        risk_free_rate: float = 0.065,
        profit_check: str = "tick",
        stop_check: str = "tick",
        eod_time: str = "15:20",
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        entry_legs: list[dict] | None = None,  # manual override: explicit legs, skips delta pick
        **_ignored,
    ):
        super().__init__(
            universe=universe,
            initial_capital=initial_capital,
            underlying=(underlying or (universe[0] if universe else "NIFTY")),
            lots=lots,
            target_delta=short_target_delta,
            entry_time=entry_time,
            entry_window_end=entry_window_end,
            force_entry=force_entry,
            adjust_cooldown_min=adjust_cooldown_min,
            adjust_close_delta=adjust_close_delta,
            adjust_close_prem_frac=adjust_close_prem_frac,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            risk_free_rate=risk_free_rate,
            profit_check=profit_check,
            stop_check=stop_check,
            eod_time=eod_time,
            min_leg_oi=min_leg_oi,
            lot_overrides=lot_overrides,
        )
        self.short_target_delta = float(short_target_delta)
        self.hedge_target_delta = float(hedge_target_delta)
        self.near_min_dte = int(near_min_dte)
        self.far_min_dte = int(far_min_dte)
        self.bias = str(bias).lower()
        self.bias_skew = float(bias_skew)
        self.entry_weekday = int(entry_weekday)
        self.recurring = bool(recurring)
        self.min_adjust_dte = int(min_adjust_dte)
        self.entry_legs = entry_legs

        # ---- calendar state (persisted) ----
        self.near_expiry: str | None = None  # ISO expiry of the near shorts
        self.far_expiry: str | None = None  # ISO expiry of the far hedges
        self.entered_once: bool = False  # one-shot latch (recurring=False)
        self.vix_entry: float | None = None
        self.regime: str = "unknown"
        self.net_premium: float | None = None  # signed points (+credit / −debit) at entry
        self._vix_fn = None  # optional manager hook

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        # Quote India VIX alongside the underlying so the regime label is available at entry.
        return [self.underlying, _VIX_SYMBOL]

    def set_vix_fn(self, fn) -> None:
        """Optional manager hook: fn(today)->vix. Falls back to ctx.close(INDIA VIX)."""
        self._vix_fn = fn

    def request_force_entry(self) -> str:
        self.force_pending = True
        return "next tick builds the double-diagonal calendar (near strangle + far hedges)"

    def _read_vix(self, ctx, today: date) -> float | None:
        """Best-effort India VIX — hook first, then the quoted symbol. FAIL-OPEN (label only).
        Live: refresh() quotes ``INDIA VIX`` (it's in spot_symbols) and feeds it via
        set_index_spot → market.index_spot('INDIA VIX'); a <150 guard rejects a mis-routed
        underlying spot (VIX is single/double digits, the index is thousands)."""
        if self._vix_fn is not None:
            try:
                v = self._vix_fn(today)
                if v and not bad_close(v):
                    return float(v)
            except Exception:  # pragma: no cover - never block entry on VIX
                pass
        idx = getattr(ctx.market, "index_spot", None)
        if idx is not None:
            try:
                v = idx(_VIX_SYMBOL)
                if v and not bad_close(v) and 0 < float(v) < 150:
                    return float(v)
            except Exception:  # pragma: no cover
                pass
        try:
            v = ctx.close(_VIX_SYMBOL)
            if v and not bad_close(v):
                return float(v)
        except Exception:  # pragma: no cover
            pass
        return None

    # ------------------------------------------------------------ expiries
    def _pick_expiries(self, ctx, today: date) -> tuple[date | None, date | None]:
        """(near, far): near = the nearest listed expiry ≥ near_min_dte DTE; far = the nearest
        listed expiry AFTER near that is ≥ far_min_dte DTE. None if either is unavailable."""
        exps = self._listed_expiries(ctx, today)
        near = min((e for e in exps if (e - today).days >= self.near_min_dte), default=None)
        if near is None:
            return None, None
        far = min(
            (e for e in exps if e > near and (e - today).days >= self.far_min_dte), default=None
        )
        return near, far

    def _is_entry_day(self, ctx, today: date) -> bool:
        # force is handled in on_slice; here just the weekday schedule (Monday by default).
        return today.weekday() == self.entry_weekday

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()

        live = self._live_legs(ctx)
        if live:
            return self._manage(ctx, live, now)

        # Flat. A completed cycle parks us (and, for a one-shot deploy, forever).
        if self.phase != "idle":
            self.done_expiry = self.near_expiry
            self.phase = "idle"
            self.near_expiry = None
            self.far_expiry = None
            self.cycle_expiry = None
            self.adjust_count = 0
            self.last_adjust_at = None

        if not self.recurring and self.entered_once:
            self.force_pending = False
            return []  # deploy-once: the owner owns the next cycle
        # A force/manual deploy (the Live force button, or force_entry from the Build view) enters
        # on the NEXT tick — bypassing the weekday + window gates. force_entry only fires the FIRST
        # entry (entered_once then blocks re-entry for a one-shot deploy), so it can't loop.
        if self.force_pending or (self.force_entry and not self.entered_once):
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

    # ----------------------------------------------------------------- entry
    def _side_targets(self) -> tuple[float, float, float, float]:
        """(ce_short, pe_short, ce_hedge, pe_hedge) target deltas after the bias skew.
        up-lean → wider calls (lower Δ) + tighter puts (higher Δ); down mirrors; neutral flat."""
        s = self.bias_skew if self.bias == "up" else -self.bias_skew if self.bias == "down" else 0.0
        return (
            max(0.02, self.short_target_delta - s),  # ce short
            max(0.02, self.short_target_delta + s),  # pe short
            max(0.02, self.hedge_target_delta - s),  # ce hedge
            max(0.02, self.hedge_target_delta + s),  # pe hedge
        )

    def _try_enter(self, ctx, now: datetime, today: date) -> list[Signal]:
        # Manual (Build-view) deploy: the owner's explicit, tuned legs are used VERBATIM — the
        # near/far expiries are the legs' OWN (WYSIWYG), not the auto-picked ones.
        if self.entry_legs:
            return self._enter_manual(ctx, now, today)

        near, far = self._pick_expiries(ctx, today)
        if near is None or far is None:
            return []
        if near.isoformat() == self.done_expiry:
            return []
        rows_near = self._chain_rows(ctx, near.isoformat())
        rows_far = self._chain_rows(ctx, far.isoformat())
        if rows_near is None or rows_far is None:
            return []
        spot = self._index_spot(ctx)
        if spot is None:
            return []
        t_near = self._t_years(near, now)
        t_far = self._t_years(far, now)

        ce_st, pe_st, ce_ht, pe_ht = self._side_targets()
        ce_s = self._pick_delta_strike(rows_near, "ce", spot, t_near, ce_st)
        pe_s = self._pick_delta_strike(rows_near, "pe", spot, t_near, pe_st)
        ce_h = self._pick_delta_strike(rows_far, "ce", spot, t_far, ce_ht)
        pe_h = self._pick_delta_strike(rows_far, "pe", spot, t_far, pe_ht)
        if not all((ce_s, pe_s, ce_h, pe_h)):
            return []
        try:
            near_lot = lot_size_for(self.underlying, near, overrides=self.lot_overrides)
            far_lot = lot_size_for(self.underlying, far, overrides=self.lot_overrides)
        except KeyError:
            return []
        n_units = float(self.lots * near_lot)
        f_units = float(self.lots * far_lot)
        legs = [
            self._leg(near, ce_s[0], "CE", -1, n_units, ce_s[1], near_lot),
            self._leg(near, pe_s[0], "PE", -1, n_units, pe_s[1], near_lot),
            self._leg(far, ce_h[0], "CE", 1, f_units, ce_h[1], far_lot),
            self._leg(far, pe_h[0], "PE", 1, f_units, pe_h[1], far_lot),
        ]
        return self._commit_entry(ctx, legs, near, far, today, spot)

    def _index_spot(self, ctx) -> float | None:
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        return float(spot) if spot is not None and not bad_close(spot) else None

    def _spec_expiry(self, spec: dict, picked: tuple[date | None, date | None]) -> date | None:
        """Resolve a leg's expiry: an ISO date verbatim, or the 'near'/'far' keyword → the
        auto-picked expiry (a convenience so a template can seed keyword legs)."""
        which = str(spec.get("expiry", "near")).lower()
        if which == "near":
            return picked[0]
        if which == "far":
            return picked[1]
        try:
            return date.fromisoformat(which[:10])
        except (ValueError, TypeError):
            return None

    def _enter_manual(self, ctx, now: datetime, today: date) -> list[Signal]:
        picked = self._pick_expiries(ctx, today)  # only for 'near'/'far' keyword legs
        exps: list[date] = []
        for spec in self.entry_legs:
            e = self._spec_expiry(spec, picked)
            if e is None:
                return []
            if e not in exps:
                exps.append(e)
        if not exps:
            return []
        exps.sort()
        near, far = exps[0], exps[-1]
        if near.isoformat() == self.done_expiry:
            return []
        spot = self._index_spot(ctx)
        if spot is None:
            return []
        chains: dict[str, dict] = {}
        for e in exps:
            rows = self._chain_rows(ctx, e.isoformat())
            if rows is None:
                return []
            chains[e.isoformat()] = rows
        legs = []
        for spec in self.entry_legs:
            try:
                exp = self._spec_expiry(spec, picked)
                right = str(spec["right"]).upper()
                strike = float(spec["strike"])
                direction = -1 if str(spec.get("side", "sell")).lower() == "sell" else 1
                lots = int(spec.get("lots", self.lots) or self.lots)
                per_lot = lot_size_for(self.underlying, exp, overrides=self.lot_overrides)
                prem = self._ltp((chains[exp.isoformat()].get(strike) or {}).get(right.lower()))
                if prem is None:
                    return []
                legs.append(
                    self._leg(exp, strike, right, direction, float(lots * per_lot), prem, per_lot)
                )
            except (KeyError, ValueError, TypeError):
                return []
        return self._commit_entry(ctx, legs, near, far, today, spot)

    def _commit_entry(
        self, ctx, legs: list[dict], near: date, far: date, today: date, spot: float
    ) -> list[Signal]:
        self.legs = legs
        self.phase = "strangle"  # routes super()._manage → our _maybe_adjust
        self.near_expiry = near.isoformat()
        self.far_expiry = far.isoformat()
        self.cycle_expiry = near.isoformat()  # base helpers key off cycle_expiry (= near)
        self.entered_day = today.isoformat()
        self.entered_once = True
        self.adjust_count = 0
        self.last_adjust_at = None
        self.vix_entry = self._read_vix(ctx, today)
        self.regime = _vix_regime(self.vix_entry)
        self.net_premium = round(sum(-leg["dir"] * leg["entry"] for leg in legs), 2)
        self._freeze_margin(ctx, spot)
        return [
            Signal(
                leg["symbol"],
                SignalAction.ENTER_SHORT if leg["dir"] < 0 else SignalAction.ENTER_LONG,
                quantity=int(leg["units"]),
                reason="ddc_entry",
                meta={"multiplier": 1},
            )
            for leg in legs
        ]

    def _leg(
        self,
        expiry: date,
        k: float,
        right: str,
        direction: int,
        units: float,
        entry: float,
        per_lot: int,
    ) -> dict:
        sym = make(
            self.underlying,
            expiry,
            float(k),
            right,
            lot_size=per_lot,
            lot_overrides=self.lot_overrides,
        ).symbol
        return {"symbol": sym, "right": right, "dir": direction, "units": units, "entry": entry}

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, live: list[dict], now: datetime) -> list[Signal]:
        # Near-expiry roll-off: once the near shorts expire (or their day's EOD arrives), SQUARE
        # the whole structure — the far hedges are never left running naked (owner design).
        if self.near_expiry:
            ne = date.fromisoformat(self.near_expiry)
            today = ctx.today()
            near_open = any(
                ctx.lots(leg["symbol"])
                for leg in self.legs
                if leg["symbol"].split("|")[1] == self.near_expiry
            )
            if (
                today > ne
                or not near_open
                or (today == ne and now.time() >= _hhmm(self.eod_time, time(15, 20)))
            ):
                return self._exit_all(live, "near_expired")
        return super()._manage(ctx, live, now)

    def _is_near(self, leg: dict) -> bool:
        return leg["symbol"].split("|")[1] == self.near_expiry

    def _maybe_adjust(
        self, ctx, live: list[dict], marks: dict[str, float], now: datetime
    ) -> list[Signal]:
        """Roll the UNTESTED (decayed) near short back toward delta-neutral (target = the tested
        short's current |Δ|) AND drag its far hedge; capped at the other short (straddle max);
        blocked inside min_adjust_dte."""
        ne = date.fromisoformat(self.near_expiry)
        if (ne - ctx.today()).days < self.min_adjust_dte:
            return []
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        if spot is None or bad_close(spot):
            return []
        spot = float(spot)
        t_near = self._t_years(ne, now)

        ce_s = next(
            (leg for leg in live if leg["right"] == "CE" and leg["dir"] < 0 and self._is_near(leg)),
            None,
        )
        pe_s = next(
            (leg for leg in live if leg["right"] == "PE" and leg["dir"] < 0 and self._is_near(leg)),
            None,
        )
        if ce_s is None or pe_s is None:
            return []

        def leg_delta(leg):
            ltp = marks.get(leg["symbol"])
            if ltp is None:
                return None
            return self._leg_delta(
                spot, float(leg["symbol"].split("|")[2]), t_near, leg["right"], ltp
            )

        def decayed(leg):
            ltp = marks.get(leg["symbol"])
            if ltp is None:
                return False
            d = leg_delta(leg)
            by_delta = d is not None and d <= self.adjust_close_delta
            by_prem = leg["entry"] > 0 and ltp <= self.adjust_close_prem_frac * leg["entry"]
            return by_delta or by_prem

        cand = [leg for leg in (ce_s, pe_s) if decayed(leg)]
        if not cand:
            return []
        # Roll the MORE decayed short (lowest ltp/entry ratio); the other is the "tested" short.
        roll = min(cand, key=lambda leg: (marks.get(leg["symbol"], 0.0) / (leg["entry"] or 1e9)))
        tested = ce_s if roll is pe_s else pe_s
        target_d = leg_delta(tested) or self.short_target_delta  # restore net-delta ≈ 0

        rows_near = self._chain_rows(ctx, self.near_expiry)
        if rows_near is None:
            return []
        side = "ce" if roll["right"] == "CE" else "pe"
        cap = float(tested["symbol"].split("|")[2])
        pick = self._pick_delta_toward(rows_near, side, spot, t_near, target_d, cap)
        if pick is None:
            return []
        new_k, new_ltp = pick
        old_k = float(roll["symbol"].split("|")[2])
        if new_k == old_k:
            return []
        per_lot = int(roll["units"] // self.lots) or 1
        signals = [
            Signal(roll["symbol"], SignalAction.EXIT_ALL, reason="ddc_roll"),
            Signal(
                self._leg(ne, new_k, roll["right"], -1, roll["units"], new_ltp, per_lot)["symbol"],
                SignalAction.ENTER_SHORT,
                quantity=int(roll["units"]),
                reason="ddc_roll",
                meta={"multiplier": 1},
            ),
        ]
        self.legs = [leg for leg in self.legs if leg["symbol"] != roll["symbol"]]
        self.legs.append(self._leg(ne, new_k, roll["right"], -1, roll["units"], new_ltp, per_lot))

        # Drag the far hedge on the SAME side to its target delta on the far expiry.
        signals += self._drag_hedge(ctx, roll["right"], spot, now)

        self.adjust_count += 1
        self.last_adjust_at = now.isoformat()
        self._freeze_margin(ctx, spot)
        return signals

    def _drag_hedge(self, ctx, right: str, spot: float, now: datetime) -> list[Signal]:
        """Re-pick the far hedge on ``right`` at hedge_target_delta (bias-skewed) and roll it."""
        if not self.far_expiry:
            return []
        far = date.fromisoformat(self.far_expiry)
        old = next(
            (
                leg
                for leg in self.legs
                if leg["dir"] > 0
                and leg["right"] == right
                and leg["symbol"].split("|")[1] == self.far_expiry
            ),
            None,
        )
        if old is None:
            return []
        rows_far = self._chain_rows(ctx, self.far_expiry)
        if rows_far is None:
            return []
        ce_ht, pe_ht = self._side_targets()[2:]
        target = ce_ht if right == "CE" else pe_ht
        side = "ce" if right == "CE" else "pe"
        pick = self._pick_delta_strike(rows_far, side, spot, self._t_years(far, now), target)
        if pick is None:
            return []
        new_k, new_ltp = pick
        if new_k == float(old["symbol"].split("|")[2]):
            return []
        per_lot = int(old["units"] // self.lots) or 1
        new_leg = self._leg(far, new_k, right, 1, old["units"], new_ltp, per_lot)
        self.legs = [leg for leg in self.legs if leg["symbol"] != old["symbol"]]
        self.legs.append(new_leg)
        return [
            Signal(old["symbol"], SignalAction.EXIT_ALL, reason="ddc_hedge_drag"),
            Signal(
                new_leg["symbol"],
                SignalAction.ENTER_LONG,
                quantity=int(old["units"]),
                reason="ddc_hedge_drag",
                meta={"multiplier": 1},
            ),
        ]

    def _pick_delta_toward(
        self,
        rows: dict[float, dict],
        side: str,
        spot: float,
        t: float,
        target: float,
        cap_strike: float,
    ) -> tuple[float, float] | None:
        """(strike, ltp) on ``side`` whose |Δ| is nearest ``target``, searching OTM→ITM up to the
        cap (the other short's strike — never crossing it: a straddle at most). Unlike the base
        OTM-only picker, this allows the roll to reach ATM/ITM so it can restore delta-neutrality.
        """
        best = None
        for k, r in rows.items():
            if side == "pe" and k > cap_strike:  # a put roll can go up to the CE strike, no further
                continue
            if (
                side == "ce" and k < cap_strike
            ):  # a call roll can go down to the PE strike, no further
                continue
            leg = r.get(side)
            prem = self._ltp(leg)
            if prem is None or not self._oi_ok(leg):
                continue
            right = "CE" if side == "ce" else "PE"
            d = self._leg_delta(spot, k, t, right, prem)
            if d is None:
                continue
            err = abs(d - target)
            if best is None or err < best[0]:
                best = (err, k, prem)
        return (best[1], best[2]) if best else None

    def _exit_all(self, live: list[dict], reason: str) -> list[Signal]:
        sigs = super()._exit_all(live, reason)
        self.near_expiry = None
        self.far_expiry = None
        return sigs

    # ------------------------------------------------------------ snapshot hooks
    def exit_rules(self) -> list[str]:
        cad = self._cadence_phrase("profit")
        rules = [f"Book profit at +{self.target_pct:g}% of broker margin ({cad})"]
        if self.stop_pct > 0:
            rules.append(
                f"Stop out at −{self.stop_pct:g}% of broker margin ({self._cadence_phrase('stop')})"
            )
        rules.append(
            "Adjust: untested short decays (≤10Δ / ≤¼ premium) → roll it + drag the far "
            "hedge toward delta-neutral (straddle max)"
        )
        rules.append(f"No adjustments inside {self.min_adjust_dte} DTE")
        rules.append(
            "Square the whole structure when the near expires (far hedge never left naked)"
        )
        return rules

    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        band = _REGIME_PREMIUM.get(self.regime)
        out = super().basket_status(market, portfolio, margin)
        out.update(
            {
                "kind": "double_diagonal_calendar",
                "phase": "diagonal" if self.legs else self.phase,
                "near_expiry": self.near_expiry,
                "far_expiry": self.far_expiry,
                "bias": self.bias,
                "vix_entry": self.vix_entry,
                "regime": self.regime,
                "net_premium": self.net_premium,
                "regime_premium_band": list(band) if band else None,
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
                "entered_once": self.entered_once,
                "vix_entry": self.vix_entry,
                "regime": self.regime,
                "net_premium": self.net_premium,
            }
        )
        return state

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        self.near_expiry = state.get("near_expiry")
        self.far_expiry = state.get("far_expiry")
        self.entered_once = bool(state.get("entered_once", False))
        self.vix_entry = state.get("vix_entry")
        self.regime = state.get("regime", "unknown")
        self.net_premium = state.get("net_premium")
