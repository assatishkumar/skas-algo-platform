"""Staggered Covered Call — ETF underlying accumulated in tranches against a sold CE.

Structure (per docs/PLAN-staggered-covered-call.md):
  * At cycle entry: SELL ``lots`` CE of the next monthly at ~``ce_otm_pct``% OTM, and
    BUY tranche 1 (≈⅓) of the notional-matched ETF position (GOLD→GOLDBEES etc.).
  * Tranches 2/3 are GTT-style buys that fire when the chain spot's CLOSE crosses
    S + i/3·(K−S) — accumulating toward the strike, so coverage rises as the call
    moves toward the money. EOD engine → GTT fills at the close of the crossing day.
  * Roll-down: when ~``rolldown_trigger_pct`` of the sold premium is captured (price
    decayed to the remainder) and ≥ ``rolldown_min_dte`` days remain, buy the CE back
    and sell a fresh ~OTM% strike on the SAME expiry; unfired triggers re-anchor to
    the new (spot, strike).
  * Expiry (engine cash-settles the CE to intrinsic): ITM → liquidate the ETF
    (called-away equivalence: keep ≈ strike value + premium) and restart fresh;
    OTM → keep the tranches, sell next month's CE, re-baseline sizing, and set
    triggers only for the still-missing tranche units.

The short CE is INITIALLY only partly covered (≈33%) — the current naked fraction is
recomputed every cycle event and stamped into signal meta + exported state; never
present this as a fully covered position. Margin reporting is coverage-unaware
(overstates the short CE's margin) — a documented caveat.
"""

from __future__ import annotations

from datetime import date

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close, next_monthly_expiry, snap

# Underlying index/commodity -> NSE ETF proxy bought as the covered leg.
ETF_FOR = {"GOLD": "GOLDBEES", "NIFTY": "NIFTYBEES", "BANKNIFTY": "BANKBEES"}


class StaggeredCoveredCallStrategy:
    strategy_id = "staggered_covered_call"
    entry_reason = "covered_call"

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 2_000_000,
        underlying: str | None = None,
        etf_symbol: str | None = None,     # default: ETF_FOR[underlying]
        lots: int = 1,                     # CE lots; ETF units derived (notional match)
        ce_otm_pct: float = 6.0,           # CE strike ≈ spot × (1 + this/100); spec range 3–12
        min_premium_pct: float = 0.001,    # floor on the sold CE's premium as a frac of spot;
                                           # below it, walk the strike NEARER (more premium)
        min_ce_otm_pct: float = 2.0,       # but never sell a call nearer than this to spot
        keep_strike_above_cost: bool = True,  # never sell/roll a CE below the held ETF's
                                              # average cost → called-away always books a
                                              # profit (don't roll a covered call into a loss)
        min_return_pct: float = 0.0,       # cost-anchored: CE strike ≥ avg_cost ×(1+this/100)
                                           # so an assignment locks in ≥ this % on the equity
        covered_call_delta: float = 0.0,   # once FULLY covered & above cost, target this |Δ|
                                           # strike (e.g. 0.30) for richer premium; 0 = off
        sell_puts: bool = False,           # "wheel": accumulate the unfilled tranches by
                                           # SELLING cash-secured puts (assigned on dips,
                                           # premium kept otherwise) instead of GTT up-buys
        put_otm_pct: float = 5.0,          # put strike ≈ spot × (1 − this/100)
        tranches: int = 3,
        rolldown_trigger_pct: float = 0.80,  # roll when ≥80% of the premium is captured
        rolldown_min_dte: int = 5,           # near expiry just let it expire instead
        min_dte: int = 18,                   # monthly expiry selection (ratio-family convention)
        risk_free_rate: float = 0.065,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "GOLD")).upper()
        self.etf_symbol = (etf_symbol or ETF_FOR.get(self.underlying,
                                                     f"{self.underlying}BEES")).upper()
        self.initial_capital = float(initial_capital)
        self.lots = int(lots)
        self.ce_otm_pct = float(ce_otm_pct)
        self.min_premium_pct = float(min_premium_pct)
        self.min_ce_otm_pct = float(min_ce_otm_pct)
        self.keep_strike_above_cost = bool(keep_strike_above_cost)
        self.min_return_pct = float(min_return_pct)
        self.covered_call_delta = float(covered_call_delta)
        self.sell_puts = bool(sell_puts)
        self.put_otm_pct = float(put_otm_pct)
        self.tranches = int(tranches)
        self.rolldown_trigger_pct = float(rolldown_trigger_pct)
        self.rolldown_min_dte = int(rolldown_min_dte)
        self.min_dte = int(min_dte)
        self.r = float(risk_free_rate)
        self.lot_overrides = lot_overrides

        # State (persisted for live recovery).
        self.ce: dict | None = None      # {symbol, units, entry, strike, expiry}
        self.pe: dict | None = None      # wheel: the open short put (accumulation leg)
        self.held_units = 0              # ETF units currently held
        self.held_cost = 0.0             # total ₹ paid for the held units (→ avg cost)
        self.full_units = 0              # notional-matched target for this cycle
        self.tranche_units = 0
        self.triggers: list[dict] = []   # pending GTT-style buys: {level, ordinal}

    # ------------------------------------------------------------------ decide
    def on_slice(self, ctx) -> list[Signal]:
        chain = ctx.option_chain()
        if chain is None:
            return []  # not an options run
        today = ctx.today()
        signals: list[Signal] = []
        if self.pe is not None and not ctx.lots(self.pe["symbol"]):
            # The engine settled our short put at expiry — assign (accumulate) if ITM.
            signals += self._on_pe_settled(ctx, chain)
        if self.ce is not None and not ctx.lots(self.ce["symbol"]):
            # The engine settled our CE at expiry — resolve the cycle outcome.
            signals += self._on_expiry_settled(chain)
        if self.ce is None:
            return signals + self._enter(ctx, chain, today)
        return signals + self._manage(ctx, chain, today)

    # ------------------------------------------------------------------ entry
    def _enter(self, ctx, chain, today: date) -> list[Signal]:
        expiry = next_monthly_expiry(chain, self.underlying, today, self.min_dte, "CE")
        spot = chain.spot(self.underlying, today)
        etf_px = self._etf_close(ctx)
        if expiry is None or spot is None or etf_px is None:
            return []
        rows = {r.strike: r for r in chain.chain(self.underlying, today, expiry)
                if r.right == "CE" and r.oi > 0}
        if not rows:
            return []
        # Floor the strike at the cost basis of any tranches already held (an OTM-expiry
        # restart) so a future assignment can't sell them below cost.
        t = max((expiry - today).days, 0) / 365.0
        target = self._ce_target_strike(rows, spot, t)
        strike, row, _met = self._select_ce_strike(
            rows, spot, self._cost_floor_strike(spot, etf_px), target=target)
        if strike is None:
            return []  # no usable OTM strike listed → retry next slice
        # Entry establishes the covered position even if premium is thin (the structure
        # needs the short call); the floor only steers WHICH strike, walking it nearer.

        opt_units = self.lots * lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        # Notional-matched coverage: ETF units worth ≈ the option's underlying notional.
        full = max(1, round(opt_units * spot / etf_px))
        tranche = full // self.tranches
        # Tranches already in hand (an OTM-expiry restart) count as fired stages.
        fired = min(self.tranches, self.held_units // tranche) if tranche else self.tranches
        buy_now = 0
        if fired == 0:
            buy_now = full - tranche * (self.tranches - 1)  # T1 carries the remainder
            fired = 1

        self.ce = {"symbol": row.symbol, "units": opt_units, "entry": row.close,
                   "strike": float(strike), "expiry": expiry}
        self.full_units, self.tranche_units = full, tranche
        # Wheel mode accumulates by selling puts (in _manage), not GTT up-buys → no triggers.
        self.triggers = [] if self.sell_puts else [
            {"level": spot + (strike - spot) * i / self.tranches, "ordinal": i}
            for i in range(fired, self.tranches)]
        naked = 1.0 - (self.held_units + buy_now) / full
        signals = [Signal(row.symbol, SignalAction.ENTER_SHORT, quantity=opt_units,
                          reason=self.entry_reason,
                          meta={"multiplier": 1, "naked_fraction": round(naked, 4)})]
        if buy_now:
            signals.append(Signal(self.etf_symbol, SignalAction.ENTER_LONG, quantity=buy_now,
                                  reason="cc_t1", meta={"tag": "cc_t1"}))
            self.held_units += buy_now
            self.held_cost += buy_now * etf_px  # fills at this slice's ETF close
        return signals

    # ------------------------------------------------------------------ manage
    def _manage(self, ctx, chain, today: date) -> list[Signal]:
        signals: list[Signal] = []
        spot = chain.spot(self.underlying, today)
        etf_px = self._etf_close(ctx)

        # 1) GTT tranches: fire when the chain spot CLOSES at/over a trigger (EOD fill).
        if spot is not None and self.triggers:
            remaining = []
            for t in self.triggers:
                if spot >= t["level"]:
                    signals.append(Signal(
                        self.etf_symbol, SignalAction.ENTER_LONG, quantity=self.tranche_units,
                        reason=f"cc_t{t['ordinal'] + 1}", meta={"tag": f"cc_t{t['ordinal'] + 1}"}))
                    self.held_units += self.tranche_units
                    if etf_px is not None:
                        self.held_cost += self.tranche_units * etf_px
                else:
                    remaining.append(t)
            self.triggers = remaining

        # 2) Roll-down on premium capture. Stale-mark guard: only act on a fresh print
        #    (a forward-filled CE mark could fake the capture threshold).
        market = getattr(ctx, "market", None)
        if market is not None and hasattr(market, "has_print"):
            if not market.has_print(self.ce["symbol"]):
                return signals
        try:
            ce_px = ctx.close(self.ce["symbol"])
        except KeyError:
            return signals
        dte = (self.ce["expiry"] - today).days
        if (spot is not None and not bad_close(ce_px)
                and ce_px <= (1.0 - self.rolldown_trigger_pct) * self.ce["entry"]
                and dte >= self.rolldown_min_dte):
            signals += self._rolldown(chain, today, spot, etf_px)

        # 3) Wheel: while still under-accumulated, sell a cash-secured put (one open at a
        #    time) — premium income on the way down, and a tranche of ETF on assignment.
        if (self.sell_puts and self.pe is None and spot is not None
                and self.held_units < self.full_units):
            signals += self._sell_put(chain, today, spot)
        return signals

    def _sell_put(self, chain, today: date, spot: float) -> list[Signal]:
        expiry = self.ce["expiry"]
        rows = {r.strike: r for r in chain.chain(self.underlying, today, expiry)
                if r.right == "PE" and r.oi > 0}
        if not rows:
            return []
        strike = snap(sorted(rows), spot * (1.0 - self.put_otm_pct / 100.0))
        row = rows.get(strike)
        if row is None or bad_close(row.close) or strike >= spot:
            return []
        units = self.lots * lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        self.pe = {"symbol": row.symbol, "units": units, "entry": row.close,
                   "strike": float(strike), "expiry": expiry}
        return [Signal(row.symbol, SignalAction.ENTER_SHORT, quantity=units,
                       reason="cc_put_sell", meta={"multiplier": 1})]

    def _on_pe_settled(self, ctx, chain) -> list[Signal]:
        """A sold put reached expiry (engine cash-settled it to intrinsic). If it finished
        ITM we were 'assigned' → buy a tranche of ETF (accumulate the dip); the put premium
        was already booked at sale. OTM → premium simply kept, sell another next cycle."""
        pe = self.pe
        self.pe = None
        settle_spot = chain.spot(self.underlying, pe["expiry"])
        if settle_spot is None or settle_spot >= pe["strike"]:
            return []  # put expired worthless — premium kept, nothing to accumulate
        etf_px = self._etf_close(ctx)
        # The put covers the remaining un-accumulated notional → assignment completes it.
        units = max(0, self.full_units - self.held_units) if self.full_units else self.tranche_units
        if not etf_px or units <= 0:
            return []
        self.held_units += units
        self.held_cost += units * etf_px
        return [Signal(self.etf_symbol, SignalAction.ENTER_LONG, quantity=units,
                       reason="cc_put_assigned", meta={"tag": "cc_put_assigned"})]

    def _rolldown(self, chain, today: date, spot: float, etf_px: float | None) -> list[Signal]:
        rows = {r.strike: r for r in chain.chain(self.underlying, today, self.ce["expiry"])
                if r.right == "CE" and r.oi > 0}
        if not rows:
            return []
        # Only roll DOWN to a strike with real premium that is STILL at/above the held
        # ETF's cost basis — rolling below cost means a recovery calls us away at a loss.
        t = max((self.ce["expiry"] - today).days, 0) / 365.0
        target = self._ce_target_strike(rows, spot, t)
        strike, row, met = self._select_ce_strike(
            rows, spot, self._cost_floor_strike(spot, etf_px), target=target)
        if strike is None or not met or strike >= self.ce["strike"]:
            return []  # no lower strike (above cost) with real premium → keep riding
        naked = 1.0 - (self.held_units / self.full_units if self.full_units else 0.0)
        signals = [
            Signal(self.ce["symbol"], SignalAction.EXIT_ALL, reason="cc_rolldown_close"),
            Signal(row.symbol, SignalAction.ENTER_SHORT, quantity=self.ce["units"],
                   reason="cc_rolldown_open",
                   meta={"multiplier": 1, "naked_fraction": round(naked, 4)}),
        ]
        self.ce = {"symbol": row.symbol, "units": self.ce["units"], "entry": row.close,
                   "strike": float(strike), "expiry": self.ce["expiry"]}
        # Unfired triggers re-anchor to the new (spot, strike) — Rules 3.2–3.4.
        self.triggers = [{"level": spot + (strike - spot) * t["ordinal"] / self.tranches,
                          "ordinal": t["ordinal"]} for t in self.triggers]
        return signals

    # ------------------------------------------------------------------ expiry
    def _on_expiry_settled(self, chain) -> list[Signal]:
        """The engine cash-settled our CE. ITM → called-away equivalence (sell the ETF,
        restart fresh next slice); OTM → keep the tranches for the next cycle."""
        strike, expiry = self.ce["strike"], self.ce["expiry"]
        self.ce = None
        self.triggers = []
        settle_spot = chain.spot(self.underlying, expiry)
        if settle_spot is not None and settle_spot > strike and self.held_units > 0:
            self.held_units = 0
            self.held_cost = 0.0
            self.full_units = 0
            return [Signal(self.etf_symbol, SignalAction.EXIT_ALL, reason="cc_called_away")]
        return []

    # ------------------------------------------------------------------ helpers
    def _ce_target_strike(self, rows: dict, spot: float, t: float) -> float | None:
        """The IDEAL strike before flooring: once FULLY covered (all tranches in) and a
        delta target is set, pick the ~``covered_call_delta``-delta strike (closer to ATM
        → richer premium on a clean covered position); otherwise the fixed ``ce_otm_pct``
        OTM strike used while still accumulating."""
        fully_covered = self.full_units > 0 and self.held_units >= self.full_units
        if fully_covered and self.covered_call_delta > 0 and t > 0:
            k = self._delta_strike(rows, spot, t, self.covered_call_delta)
            if k is not None:
                return k
        return snap(sorted(rows), spot * (1.0 + self.ce_otm_pct / 100.0))

    def _delta_strike(self, rows: dict, spot: float, t: float, target_delta: float) -> float | None:
        """The OTM call strike whose |BS delta| is nearest ``target_delta`` (IV backed out
        of each row's close). None if no OTM strike yields a usable IV."""
        best, err = None, 1e9
        for k, row in rows.items():
            if k <= spot or bad_close(row.close):
                continue  # OTM calls only
            iv = bs.implied_vol(row.close, spot, k, t, self.r, "CE")
            if iv is None:
                continue
            d = abs(bs.delta(spot, k, t, self.r, iv, "CE"))
            if abs(d - target_delta) < err:
                best, err = k, abs(d - target_delta)
        return best

    def _select_ce_strike(self, rows: dict, spot: float, floor_strike: float = 0.0,
                          target: float | None = None):
        """The CE strike to sell, its ChainRow, and whether the premium floor was met.
        Start at ``target`` (the ideal strike — ``ce_otm_pct`` OTM, or a delta strike when
        fully covered; defaults to ``ce_otm_pct`` OTM), then walk the strike DOWN toward
        spot (calls get richer) until the per-unit premium clears ``min_premium_pct × spot``
        — but never nearer than ``min_ce_otm_pct`` OTM, and never BELOW ``floor_strike``
        (the held ETF's cost basis ×(1+min_return_pct), so an assignment books ≥ that gain).

        Returns ``(strike, row, met)`` where ``met`` is True iff a strike cleared the
        premium floor. When nothing clears it (e.g. only days to expiry, or a dead-vol
        month), the most-OTM tradable strike is returned with ``met=False`` so the caller
        can decide: ENTRY still establishes coverage; a ROLL-DOWN declines (no point
        churning into another worthless call). ``(None, None, False)`` if no strike fits.
        """
        if target is None:
            target = snap(sorted(rows), spot * (1.0 + self.ce_otm_pct / 100.0))
        if target is None:
            return None, None, False
        floor_px = self.min_premium_pct * spot
        min_strike = max(spot * (1.0 + self.min_ce_otm_pct / 100.0), floor_strike)
        if target < min_strike:
            # The OTM target sits below the cost/OTM floor (we're underwater). Take the
            # nearest listed strike at/above the floor — richest premium that still keeps
            # any assignment at/above cost. ``met`` flags whether even that is worthwhile.
            above = [k for k in sorted(rows) if k >= min_strike and not bad_close(rows[k].close)]
            if not above:
                return None, None, False
            k = above[0]
            return k, rows[k], rows[k].close >= floor_px
        best = None  # most-OTM tradable strike (fallback if none clears the floor)
        for k in sorted(rows, reverse=True):  # most OTM → nearest
            if k > target or k < min_strike:
                continue
            row = rows[k]
            if bad_close(row.close):
                continue
            if best is None:
                best = k
            if row.close >= floor_px:
                return k, row, True
        return (best, rows[best], False) if best is not None else (None, None, False)

    def _cost_floor_strike(self, spot: float, etf_px: float | None) -> float:
        """The held ETF's average cost expressed in index/strike points (0 when nothing
        held or the guard is off). ``spot/etf_px`` is the current index-per-ETF ratio, so
        ``avg_cost_etf × ratio`` is the index level our cost basis corresponds to — the
        strike at/above which a called-away sale books a profit."""
        if (not self.keep_strike_above_cost or self.held_units <= 0
                or etf_px is None or etf_px <= 0):
            return 0.0
        avg_cost_etf = self.held_cost / self.held_units
        return avg_cost_etf * (spot / etf_px) * (1.0 + self.min_return_pct / 100.0)

    def _etf_close(self, ctx) -> float | None:
        try:
            px = ctx.close(self.etf_symbol)
        except KeyError:
            return None  # ETF series not cached / no print yet
        return px if px and px > 0 else None

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "ce": dict(self.ce, expiry=self.ce["expiry"].isoformat()) if self.ce else None,
            "pe": dict(self.pe, expiry=self.pe["expiry"].isoformat()) if self.pe else None,
            "etf_symbol": self.etf_symbol,
            "held_units": self.held_units,
            "held_cost": self.held_cost,
            "full_units": self.full_units,
            "tranche_units": self.tranche_units,
            "triggers": [dict(t) for t in self.triggers],
            "naked_fraction": (1.0 - self.held_units / self.full_units
                               if self.full_units else None),
        }

    def load_state(self, state: dict) -> None:
        ce = state.get("ce")
        self.ce = dict(ce, expiry=date.fromisoformat(ce["expiry"])) if ce else None
        pe = state.get("pe")
        self.pe = dict(pe, expiry=date.fromisoformat(pe["expiry"])) if pe else None
        self.etf_symbol = state.get("etf_symbol", self.etf_symbol)
        self.held_units = int(state.get("held_units", 0))
        self.held_cost = float(state.get("held_cost", 0.0))
        self.full_units = int(state.get("full_units", 0))
        self.tranche_units = int(state.get("tranche_units", 0))
        self.triggers = [dict(t) for t in state.get("triggers", [])]
