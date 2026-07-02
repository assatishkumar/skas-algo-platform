"""Donchian Strangle BACKTEST — the live basket executor re-entered cycle by cycle.

``donchian_strangle_monthly`` is one-shot (screener resolves legs, strategy executes one
cycle, live only). This subclass gives it a backtest path WITHOUT touching the live
class: a precomputed schedule (services/donchian_bt.build_cycle_schedule — the "backtest
screener") is injected via ``set_cycles``; each cycle loads its legs into the inherited
``_enter``, the inherited ``_manage`` governs it (portfolio stop/target, leg targets,
breach flips, max_flips close-out — the LIVE code, unchanged), survivors settle to
intrinsic at expiry (runner settles BEFORE the slice, so the flat book is visible the
same bar), and the state resets for the next cycle.

Why this works with zero live-class edits: the live class guards every live-only market
call with ``getattr(ctx.market, ..., None)`` fallbacks to ``ctx.close`` — in a backtest
``prefetch_quotes``/``fill_price``/``live_chain`` are absent (falls back to the loader's
Black-Scholes close; 30Δ flips degrade to ATM) and ``OptionMarketView.index_spot`` exists
for breach detection. The ONE behavioral override here is touch-basis breaches: live
"touch" reacts to intraday ticks; on daily bars we test the day's HIGH/LOW via
``ctx.market.day_range`` (wired by data/basket_options) instead of just the close.

Daily-bar approximations (also see data/basket_options): flips fill at that day's CLOSE
(live fills intraday at the bid); same-bar ordering (stop vs flip vs both edges) is
invisible; a bar clearing both a name's levels resolves to the larger excursion.
"""

from __future__ import annotations

from datetime import date

from skas_algo.engine.types import Signal

from .donchian_strangle_monthly import DonchianStrangleMonthlyStrategy


class DonchianStrangleBtStrategy(DonchianStrangleMonthlyStrategy):
    strategy_id = "donchian_strangle_bt"

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 10_000_000,
        # Declared (not just **_ignored) so _effective_strategy_params persists them on
        # the run and the report echoes the effective pricing/screening config. The
        # schedule builder and the run's BS loader are their actual consumers.
        vol_multiplier: float = 1.0,
        vol_window: int = 20,
        r: float = 0.065,
        skip_leg_min_premium_pct: float = 0.5,
        round_out: bool = False,
        breakout_atm: bool = True,
        lots_per_name: int = 1,
        hedge_enabled: bool = True,
        hedge_otm_pct: float = 4.5,
        notional_per_name: float = 750_000.0,  # split-proof sizing (0 = fixed lots)
        min_hv_ratio: float = 0.0,             # entry filters (0 = off) — see donchian_bt
        min_channel_width_pct: float = 0.0,
        vix_half_threshold: float = 0.0,
        vix_skip_threshold: float = 0.0,
        **kw,
    ):
        super().__init__(universe=universe, initial_capital=initial_capital, **kw)
        self.vol_multiplier = vol_multiplier
        self.vol_window = vol_window
        self.r = r
        self.skip_leg_min_premium_pct = skip_leg_min_premium_pct
        self.round_out = round_out
        self.breakout_atm = breakout_atm
        self.lots_per_name = lots_per_name
        self.hedge_enabled = hedge_enabled
        self.hedge_otm_pct = hedge_otm_pct
        self.notional_per_name = notional_per_name
        self.min_hv_ratio = min_hv_ratio
        self.min_channel_width_pct = min_channel_width_pct
        self.vix_half_threshold = vix_half_threshold
        self.vix_skip_threshold = vix_skip_threshold
        self._cycles: list[dict] = []   # injected by services/backtest via set_cycles
        self._cycle_idx = 0
        self._day_range_fn = None       # stashed per-slice: ctx.market.day_range (backtest)

    def set_cycles(self, cycles: list[dict]) -> None:
        """Inject the precomputed per-cycle leg schedule (deterministic; not persisted)."""
        self._cycles = list(cycles)
        self._cycle_idx = 0

    # ------------------------------------------------------------------ slice
    def on_slice(self, ctx) -> list[Signal]:
        self._day_range_fn = getattr(ctx.market, "day_range", None)
        if self.entered and not self.done:
            sigs = self._manage(ctx)  # the LIVE manage path, unchanged
            if not self.done:
                return sigs
            # ``done`` here means THIS CYCLE is over (settled at expiry / stopped out) —
            # reset and fall through so the next cycle can enter on this very bar (the
            # runner settles expiries BEFORE the slice, so entry day sees a flat book).
            self._reset_cycle_state()
            if sigs:
                return sigs  # exits execute this bar; enter from the next one
        return self._maybe_enter_cycle(ctx)

    def _maybe_enter_cycle(self, ctx) -> list[Signal]:
        today = ctx.today()
        while self._cycle_idx < len(self._cycles):
            cyc = self._cycles[self._cycle_idx]
            entry = date.fromisoformat(cyc["entry_date"])
            expiry = date.fromisoformat(cyc["expiry"])
            if today < entry:
                return []
            if today > expiry:  # a data gap swallowed the whole cycle — skip it
                self._cycle_idx += 1
                continue
            self._cycle_idx += 1
            if not cyc["legs"]:
                continue  # nothing tradable that cycle (e.g. every leg under the floor)
            self._expiry_param = cyc["expiry"]
            self.leg_defs = list(cyc["legs"])
            return self._enter(ctx)  # inherited: prices legs at ctx.close (= BS loader)
        return []

    def _reset_cycle_state(self) -> None:
        """Clear everything _enter/_record_leg/_flips accumulate — the next cycle starts
        from a clean book (realized P&L lives in the portfolio, not the strategy)."""
        self.entered = False
        self.done = False
        self.legs = []
        self.entry_close = {}
        self.units = {}
        self.leg_side = {}
        self.leg_underlying = {}
        self.leg_right = {}
        self.leg_strike = {}
        self.agg_notional = 0.0
        self.premium_collected = 0.0
        self.realized_pnl = 0.0
        self.name_lot = {}
        self.name_lots = {}
        self.name_step = {}
        self.flip_count = {}
        self.closed_names = []
        self.realized_by_name = {}
        self.leg_origin = {}
        self.last_flip_day = {}

    # ------------------------------------------------------------------ breach
    def _breach_side(self, name: str, open_legs, spot: float) -> str | None:
        """Touch basis on daily bars: a short CE is tested against the day's HIGH and a
        short PE against the day's LOW (the live tick-level "touch" is invisible in EOD
        data; the close alone would miss intraday breaches). Close basis and runs without
        a day_range provider defer to the live logic on ``spot``."""
        rng = self._day_range_fn(name) if self._day_range_fn is not None else None
        if self.breach_basis != "touch" or rng is None:
            return super()._breach_side(name, open_legs, spot)
        hi, lo = rng
        buf = self.breach_buffer_pct / 100.0
        best: tuple[float, str] | None = None  # a bar clearing BOTH → larger excursion wins
        for s in open_legs:
            if self.leg_side[s] != "sell" or self.leg_underlying[s] != name:
                continue
            k = self.leg_strike[s]
            if self.leg_right[s] == "CE" and hi >= k * (1 + buf):
                exc = (hi - k) / k
                if best is None or exc > best[0]:
                    best = (exc, "CE")
            if self.leg_right[s] == "PE" and lo <= k * (1 - buf):
                exc = (k - lo) / k
                if best is None or exc > best[0]:
                    best = (exc, "PE")
        return best[1] if best else None
