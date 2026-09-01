"""Pydantic request/response models for the API."""

from __future__ import annotations

from datetime import UTC, date, datetime

from pydantic import BaseModel, Field


def iso_utc(dt: datetime | None) -> str | None:
    """Serialize a datetime as a UTC ISO string the browser parses unambiguously.

    Stored timestamps are UTC, but SQLite drops tzinfo on read, yielding a naive
    datetime whose ``.isoformat()`` has no offset — which the browser then treats as
    *local* time (off by the local UTC offset). Attach UTC for naive values so the
    emitted string always carries an offset.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


class OverrideInput(BaseModel):
    scope: str = Field(description="ALGO | SYMBOL | POSITION")
    target: str | None = None
    rule: dict


class BacktestRequest(BaseModel):
    strategy_id: str
    # Either an explicit symbol list (Custom) or a named universe (expanded server-side).
    symbols: list[str] = Field(default_factory=list)
    universe: str | None = None
    # "STOCK" (equity, default) or "DERIV" (index options). DERIV uses the options
    # data + chain + expiry-settlement engine instead of the equity loader.
    instrument_class: str = "STOCK"
    underlying: str | None = None  # DERIV: NIFTY / BANKNIFTY (else taken from params/symbols)
    start_date: date
    end_date: date
    capital: float = 2_500_000
    # Strategy-specific knobs (e.g. profit_target, capital_parts, max_lots).
    params: dict = Field(default_factory=dict)
    tax_rate: float = 0.20
    withdrawal_rate: float = 0.0
    lookback: int = 20
    name: str | None = None
    notes: str | None = None
    batch_id: str | None = None  # set by a sweep to group its variant runs
    overrides: list[OverrideInput] = Field(default_factory=list)
    # False → compute + return the report/trades WITHOUT persisting an Algo/AlgoRun (preview).
    # The client then calls /backtest/save to persist the already-computed result.
    persist: bool = True


class BacktestResponse(BaseModel):
    run_id: int | None = None  # None for a non-persisted preview
    strategy_id: str
    report: dict
    trades: list[dict]


class SaveBacktestRequest(BaseModel):
    """Persist a previously-previewed backtest WITHOUT recomputing it."""

    request: BacktestRequest
    report: dict
    trades: list[dict]


class RunSummary(BaseModel):
    run_id: int
    algo_id: int
    name: str
    notes: str | None = None
    strategy_id: str
    mode: str
    archived: bool = False
    batch_id: str | None = None
    started_at: str | None
    metrics: dict


class UniverseOut(BaseModel):
    name: str
    label: str
    count: int  # symbols available in the cache


class LiveStartRequest(BaseModel):
    strategy_id: str
    name: str | None = None
    notes: str | None = None
    symbols: list[str] = Field(default_factory=list)
    universe: str | None = None
    instrument_class: str = "STOCK"  # "STOCK" | "DERIV" (options)
    underlying: str | None = None  # DERIV: NIFTY/BANKNIFTY
    capital: float = 2_500_000
    params: dict = Field(default_factory=dict)
    tax_rate: float = 0.20
    withdrawal_rate: float = 0.0
    lookback: int = 20
    overrides: list[OverrideInput] = Field(default_factory=list)
    mode: str = "PAPER"
    quote_source: str = "cache"  # "cache" (offline) | "zerodha" (live LTP)
    broker_account_id: int | None = None
    refresh_seconds: int = 30
    # None = let the STRATEGY choose (its `default_decision_time`, else 15:20). Not a plain
    # "15:20" default, because then an omitted field and a deliberate 15:20 are the same
    # value and a strategy could never state its own. Recovery's fallback is untouched — an
    # existing deploy keeps whatever it stored (§1).
    decision_time: str | None = None
    ignore_market_hours: bool = False
    auto: bool = False  # start the background refresh/decision loop
    # Options PAPER only: seed from a past date — replay the strategy as a backtest from
    # warm_from_date → today, then carry the resulting open book forward live.
    warm_from_date: date | None = None


class OptionTradeLeg(BaseModel):
    """One leg of a custom option trade picked off the chain."""

    right: str  # "CE" | "PE"
    strike: float
    side: str  # "buy" | "sell"
    lots: int = 1  # lot-sets (× the contract lot size)
    expiry: str | None = None  # per-leg ISO expiry (calendars); None → the trade's default expiry


class OptionsTradeDeploy(BaseModel):
    """Deploy a user-built multi-leg option position (strategy_id=custom_options). Percentages
    are entered as whole numbers (e.g. 50 = 50%) and converted to fractions for the strategy."""

    name: str
    underlying: str
    expiry: str  # ISO date from the chain
    legs: list[OptionTradeLeg] = Field(default_factory=list)
    lot_size: int = 0  # explicit contract lot size (required for stock F&O)
    capital: float = 1_000_000
    spot_upper: float | None = None  # exit-all band on the underlying spot
    spot_lower: float | None = None
    target_pct: float | None = None  # combined P&L target, % of net entry premium
    stop_pct: float | None = None
    leg_targets: dict[int, float] | None = None  # {leg_index: %} per-leg premium target
    leg_stops: dict[int, float] | None = None
    mode: str = "PAPER"
    quote_source: str = "cache"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True
    notes: str | None = None


class DonchianNameInput(BaseModel):
    """One universe name + its Sensibull screener fields (ATMIV / IVP / Event date)."""

    symbol: str
    atm_iv: float | None = None
    ivp: float | None = None
    event: str | None = None  # ISO date, or "-"/empty when none


class DonchianAnalyzeRequest(BaseModel):
    """Screen the basket for the Donchian-strangle setup using LIVE chains (needs a session).
    Cycle anchors are resolved from the listed monthly expiries unless the user overrides them."""

    broker_account_id: int
    names: list[DonchianNameInput] = Field(default_factory=list)
    range_start: str | None = None  # ISO overrides (else resolved from the monthly calendar)
    range_end: str | None = None
    entry_date: str | None = None
    sell_expiry: str | None = None
    ivp_min: float = 50.0
    require_iv_gt_hv: bool = True
    hv_window: int = 20
    skip_leg_min_premium_pct: float = 0.5  # % of spot
    round_out: bool = False
    breakout_atm: bool = True  # spot beyond range → sell the ATM opposite leg (skip ITM)
    lots_per_name: int = 1
    min_dte: int = 7
    # Entry gates ported from the backtest loss study (0 = off): vol compression + tight
    # channel marked the worst entries. Excluded rows keep their legs (deployable override).
    min_hv_ratio: float = 0.0  # exclude when HV(hv_window)/HV60 < this (~0.85)
    min_channel_width_pct: float = 0.0  # exclude when (high−low)/spot·100 < this (~8)


class DonchianPortfolioRequest(BaseModel):
    """Recompute the portfolio panel (notional, notional-matched NIFTY hedge, SL/target, combined
    basket margin) for the selected screener rows."""

    broker_account_id: int
    sell_expiry: str
    selected: list[dict] = Field(default_factory=list)  # selected analyze() rows
    hedge_otm_pct: float = 4.5
    hedge_beta_weight: bool = False
    hedge_cost_cap_pct: float = 25.0
    portfolio_sl_pct: float = 2.0
    portfolio_target_enabled: bool = False
    portfolio_target_pct: float = 50.0  # % of the basis (see portfolio_basis)
    portfolio_basis: str = "notional"  # "notional" (legacy) | "margin" (stop+target as % of margin)


class DonchianDeploy(BaseModel):
    """Deploy the resolved basket + NIFTY hedge in one action (strategy_id=donchian_strangle_monthly).
    ``legs`` is the fully-resolved leg list (stock shorts + hedge longs) built by the screener."""

    name: str
    notes: str | None = None
    sell_expiry: str  # ISO monthly expiry for all legs
    legs: list[dict] = Field(
        default_factory=list
    )  # [{underlying, right, strike, side, lots, spot, lot_size}]
    capital: float = 5_000_000
    portfolio_sl_pct: float = 2.0
    portfolio_target_enabled: bool = False
    portfolio_target_pct: float = 50.0
    portfolio_basis: str = "notional"  # "notional" (legacy) | "margin"
    leg_target_enabled: bool = False
    leg_target_pct: float = 80.0  # % of each leg's own premium → close that leg
    # New deploys flip INTRADAY the moment spot clears a strike (touch), capped at one flip per
    # name per day (the strategy's last_flip_day guard). The strategy constructor still defaults to
    # "close"/2 as the conservative backstop for any param-less recovery (see CLAUDE.md §1); the
    # deploy layer explicitly opts into the intraday behavior here.
    breach_basis: str = "touch"  # "touch" (intraday) | "close" (EOD)
    breach_buffer_pct: float = 0.5  # spot must clear a short strike by this % to flip
    flip_delta: str = "atm"  # "atm" | "30delta"
    max_flips: int = 3  # up to two rolls (once/day), then close the name on the next breach
    mode: str = "PAPER"
    quote_source: str = "cache"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class MtgBacktestRequest(BaseModel):
    """Dedicated momentum-theta intraday backtest (NIFTY only — SENSEX has no history).
    Premiums are Black-Scholes with prior-day HV20 × vol_multiplier (a model, not the tape)."""

    start_date: date
    end_date: date | None = None
    lots: int = 1
    st_period: int = 7
    st_multiplier: float = 3.0
    max_trades_per_day: int = 3
    entry_cutoff: str = "15:00"
    eod_exit: str = "15:20"
    min_dte: int = 0
    vol_multiplier: float = 1.1
    r: float = 0.065
    slippage_bps: float = 5.0
    capital: float = 500_000
    # With a logged-in Zerodha account the 15-min bar store is topped up first;
    # without one the backtest runs on whatever is already cached.
    broker_account_id: int | None = None


class DeltaNeutralDeploy(BaseModel):
    """Deploy delta_neutral_monthly: 18Δ monthly strangle (BANKNIFTY default) with
    premium-rebalance rolls → straddle cap → iron fly. Live-chain-driven (delta solve +
    premium-matched rolls) → broker quote source required; no backtest (BANKNIFTY has
    ~no cached chain history)."""

    name: str
    notes: str | None = None
    underlying: str = "BANKNIFTY"
    lots: int = 1
    target_delta: float = 0.18
    entry_time: str = "11:00"
    force_entry: bool = False  # enter next window tick instead of waiting for entry day
    adjust_threshold_pct: float = 40.0
    adjust_cooldown_min: int = 15
    profit_target_pct: float = 2.5  # % of margin deployed
    stop_loss_pct: float = 0.0  # 0 = off (spec-faithful)
    # Two-cadence model (2026-07-18): live decision sampling. Deploy default = the
    # owner policy (1min); the strategy ctor default stays "tick" so RECOVERED runs
    # keep their recorded behavior (recovery binds params_snapshot, not these).
    profit_check: str = "1min"
    stop_check: str = "1min"
    # Adjustment (roll/hedge) cadence decoupled from profit + no decision in the first N min
    # after the open (2026-07-22). Deploy defaults carry the policy; ctor defaults preserve old.
    adjust_check: str = "5min"
    adjust_after_open_min: int = 5
    # Profit-protecting trailing stop (additive to the fixed stop). 0/0 = off.
    trail_trigger_pct: float = 0.0  # % of margin (whole percent)
    trail_step_pct: float = 0.0
    trail_mode: str = "ratchet"  # "ratchet" | "below_peak"
    # Which P&L the target/stop/trail measure: "total" (realized+unrealized) | "open_legs".
    pnl_basis: str = "total"
    # Which MARGIN anchors the ₹ thresholds: "entry" = frozen once at cycle entry (absolute ₹,
    # owner's two-margin scheme 2026-07-27) | "current" = re-frozen after every roll/hedge/add.
    exit_margin_basis: str = "entry"
    eod_time: str = "15:20"
    # Build-view manual deploy: explicit entry legs — enter these verbatim, then run the roll.
    entry_legs: list[dict] | None = None
    capital: float = 1_000_000
    refresh_seconds: int = 20
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class IronFlyDeploy(BaseModel):
    """Deploy iron_fly_monthly: BANKNIFTY monthly IRON FLY (ATM straddle + breakeven wings)
    with the post-iron-fly adjustment (default ON — sell ~15-20Δ on the untested side on a
    breakeven breach, roll it, exit-all if the payoff turns fully negative). Live-chain-driven
    → broker quote source required; no backtest."""

    name: str
    notes: str | None = None
    underlying: str = "BANKNIFTY"
    lots: int = 1
    entry_time: str = "11:00"
    force_entry: bool = False  # enter next window tick instead of waiting for entry day
    ironfly_adjust: bool = True  # the active adjustment (the whole point)
    adjust_target_delta: float = 0.175  # 15-20Δ untested-side sell
    adjust_cooldown_min: int = 15
    profit_target_pct: float = 2.5  # % of margin deployed
    stop_loss_pct: float = 0.0  # 0 = off; optional hard MTM floor for the naked tail
    # Two-cadence model (2026-07-18): live decision sampling. Deploy default = the
    # owner policy (1min); the strategy ctor default stays "tick" so RECOVERED runs
    # keep their recorded behavior (recovery binds params_snapshot, not these).
    profit_check: str = "1min"
    stop_check: str = "1min"
    adjust_check: str = "5min"  # roll/hedge cadence (own, decoupled from profit) — 2026-07-22
    adjust_after_open_min: int = 5  # no roll/hedge in the first N min after the open
    trail_trigger_pct: float = 0.0  # trailing stop (% of margin, whole percent); 0/0 = off
    trail_step_pct: float = 0.0
    trail_mode: str = "ratchet"  # "ratchet" | "below_peak"
    pnl_basis: str = "total"  # "total" (realized+unrealized) | "open_legs"
    exit_margin_basis: str = "entry"  # ₹-threshold anchor: "entry" (frozen once) | "current"
    eod_time: str = "15:20"
    # Build-view manual deploy: explicit entry legs — enter these verbatim, then run the adjustment.
    entry_legs: list[dict] | None = None
    capital: float = 1_000_000
    refresh_seconds: int = 20
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class RatioManualDeploy(BaseModel):
    """Build-view manual deploy of a ratio-family strategy (batman / hni / call- / put-ratio):
    enter the owner's explicit legs VERBATIM, then run the strategy's OWN management — the
    %-of-broker-margin profit/stop AND the native time exit (batman: max_holding_days; HNI:
    exit_weekday). Percentages are whole numbers here → fractions at the strategy. Live-chain
    (broker source required)."""

    name: str
    notes: str | None = None
    strategy_id: str  # batman_ratio_monthly | hni_weekly | call_ratio_monthly | put_ratio_monthly
    underlying: str = "NIFTY"
    entry_legs: list[dict] = Field(default_factory=list)
    profit_target_pct: float = 2.5  # % of margin (whole percent)
    stop_loss_pct: float = 3.0  # % of margin (whole percent; 0 = off)
    max_holding_days: int = 20  # batman time exit (HNI uses its Friday exit_weekday)
    # Trailing stop, whole-percent here → fractions at the strategy (like profit/stop). 0/0 = off.
    trail_trigger_pct: float = 0.0
    trail_step_pct: float = 0.0
    trail_mode: str = "ratchet"  # "ratchet" | "below_peak"
    # The %-of-margin target/stop/trail measure the WHOLE cycle (banked rolls + open
    # MTM). Ctor default stays "open_legs" (§1 — a recovered deploy is unchanged);
    # every NEW deploy gets "total". Run #203 sat on open_legs, banked Rs1.53L of
    # rolls its exit check could not see, and peaked 0.2% short of a target the UI
    # already showed as cleared (2026-08-21).
    pnl_basis: str = "total"
    profit_check: str = "1min"
    stop_check: str = "eod"
    time_check: str = "eod"
    eod_time: str = "15:15"
    capital: float = 1_000_000
    refresh_seconds: int = 20
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class FairValueCalendarDeploy(BaseModel):
    """Deploy fair_value_calendar: the monthly premium-matched ratio calendar (sell ~150 +
    ~450 near-weekly, buy 3× ~200 monthly, same side; 900-pt gap rule; weekly rolls at the
    SAME strikes; the buy leg never rolls — sells about to land on it end the cycle and a
    fresh one opens next session). Premium hunt reads the LIVE chain → broker source
    required. Deploy default side_mode="ce" — the only configuration the 5-year store
    replay rewarded (+₹147k/set, maxDD ₹52k); "fair_value" (the video's auto-pick),
    "both" and "pe" all backtested negative and are offered for experiments only."""

    name: str
    notes: str | None = None
    underlying: str = "NIFTY"
    sets: int = 1
    # Rupee margin for ONE lot-set — the anchor for the % target/stop. The DEPLOY default
    # is the owner's measured Kite basket figure (2026-08-25); the ctor default stays 0
    # (= derive from the broker push) so a recovered deploy is unchanged (§1). Manual is
    # preferred here because the broker number is not stable: run 15 froze ~₹70k/set at
    # entry and the same legs priced ₹4.5L/set on expiry day with a deep-ITM short, so a
    # "5% of margin" target meant wildly different rupees at different times.
    margin_per_set: float = 134612.0
    side_mode: str = "ce"  # ce | fair_value | both | pe
    sell_premium_1: float = 150.0
    sell_premium_2: float = 450.0
    buy_premium: float = 200.0
    buy_lots_per_set: int = 3
    premium_tolerance_pct: float = 30.0
    max_gap_points: float = 900.0
    min_sold_dte: int = 4
    fv_growth_pct: float = 11.7
    fv_anchor_value: float = 12430.0
    fv_anchor_date: str = "2020-01-20"
    fv_band_pct: float = 4.0
    entry_time: str = "09:30"
    entry_window_end: str = "15:00"
    roll_time: str = "15:00"
    # Roll the day BEFORE the sold expiry: expiry-day margin on a short weekly spikes into
    # settlement (owner rule 2026-08-20). Ctor default is 0 (§1) — the policy lives here.
    roll_days_before: int = 1
    max_rolls: int = 0  # 0 = roll until target or the buy-expiry cycle end
    profit_target_pct: float = 5.0  # % of frozen broker margin, whole percent
    stop_loss_pct: float = 0.0  # 0 = off (the cycle end is the structural bound)
    force_entry: bool = False  # enter next window tick, skipping the month gate
    # Deploy default = the owner cadence policy (1min); ctor defaults stay "tick" (§1).
    profit_check: str = "1min"
    stop_check: str = "1min"
    eod_time: str = "15:20"
    pnl_basis: str = "total"  # the rolls ARE the income — whole-cycle basis
    exit_margin_basis: str = "entry"
    min_leg_oi: int = 1
    capital: float = 500_000
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    refresh_seconds: int = 15
    ignore_market_hours: bool = False
    auto: bool = True


class IntradayStrangleComboDeploy(BaseModel):
    """Deploy intraday_strangle_combo: sell OTM3 CE+PE on the current weekly at 09:16,
    flat by exit_time; each leg managed INDEPENDENTLY (40% stop / 70% target on its OWN
    premium, re-entering at a freshly recomputed OTM3, separate SL/target budgets).

    One INDEX per deploy — the strategy reads it from the run's universe, and the
    day_schedule then gates which weekdays that index trades (deck: Mon NIFTY · Tue BOTH ·
    Wed/Thu SENSEX · Fri NIFTY). NIFTY uses the LISTING grid (50-pt steps) via the
    needs_listing_grid carve-out — the ONE documented exemption from the round-strikes
    rule, which is why this had no backtest-form checkbox and deploys were API-only until
    2026-08-27. Deploy exit default 15:20 (the intraday-family rule: Zerodha auto-squares
    at 15:26, a 15:25 exit leaves no room to retry); ctor stays 15:25 (§1)."""

    name: str
    notes: str | None = None
    underlying: str = "NIFTY"  # NIFTY | SENSEX (SENSEX is unvalidated in replay — 23 days)
    lots: int = 1
    otm_steps: int = 3  # listing-grid steps; 0 = straddle
    entry_time: str = "09:16"
    exit_time: str = "15:20"
    reentry_cutoff: str = "15:00"
    leg_stop_pct: float = 40.0
    leg_target_pct: float = 70.0
    max_sl_reentries: int = 2
    max_target_reentries: int = 2
    same_strike_action: str = "reenter"  # reenter (deck) | skip | hold
    wing_steps: int = 0  # >0 = iron fly/condor wings that many grid steps beyond each short
    # Rupee MTM stop per lot for THIS index (scalar — a deploy is one index). Deck: NIFTY
    # 1500, SENSEX 0 (off). Day-cumulative, outranks a leg stop in the same tick.
    mtm_stop_per_lot: float = 1500.0
    # Deploy default = the owner cadence policy (1min); ctor default stays "tick" (§1).
    stop_check: str = "1min"
    min_leg_oi: int = 1
    capital: float = 500_000
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    refresh_seconds: int = 15
    ignore_market_hours: bool = False
    auto: bool = True


class VolcanoCalendarDeploy(BaseModel):
    """Deploy volcano_calendar: the monthly PE butterfly + CE calendar (NIFTY). Entry on
    the LAST FRIDAY of the month at 15:16 (holiday → the prior session); near = next
    month's monthly, far = the month after; ±2% of margin target/stop; close all on the
    near expiry day. The 4% center-credit rule walks the CE calendar out one strike at a
    time — it needs ``margin_per_set`` (no broker margin exists before the order does),
    so the deploy default is the owner's measured figure, NOT the ctor's 0. NOTE Kite's
    basket API reports margin NET of premium receivable while the web calculator's
    headline is span+exposure BEFORE that credit — enter whichever you mean to size on."""

    name: str
    notes: str | None = None
    underlying: str = "NIFTY"
    lots: int = 1  # lot-SETS (1 set = 5 legs / 6 lots)
    margin_per_set: float = 190_000.0  # ₹ per set — measure YOUR basket on Kite
    wing_1: float = 400.0
    wing_2: float = 800.0
    ce_offset: float = 200.0
    max_credit_pct: float = 4.0
    max_ce_shifts: int = 6
    entry_time: str = "15:16"
    entry_window_end: str = "15:29"
    cycle_exit_time: str = "15:15"
    profit_target_pct: float = 2.0  # % of margin, whole percent
    stop_loss_pct: float = 2.0
    force_entry: bool = False  # enter next window tick, skipping the last-Friday gate
    # Deploy default = the owner cadence policy (1min); ctor defaults stay "tick" (§1).
    profit_check: str = "1min"
    stop_check: str = "1min"
    pnl_basis: str = "open_legs"
    exit_margin_basis: str = "entry"
    min_leg_oi: int = 1
    capital: float = 500_000
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    refresh_seconds: int = 15
    ignore_market_hours: bool = False
    auto: bool = True


class DoubleDiagonalDeploy(BaseModel):
    """Deploy double_diagonal_calendar: a NIFTY double-diagonal calendar — a near short strangle +
    farther long hedges (the first TWO-expiry position). Delta-first (shorts ~20-25Δ, hedges
    ~15-20Δ) with a manual bias skew, ±%-of-broker-margin exits, and the untested-short roll +
    far-hedge drag adjustment. Manual deploy (deploy-once), auto-managed. Live-chain-driven (two
    chains) → broker quote source required; no backtest. ``entry_legs`` (optional) overrides the
    delta pick with explicit legs from the Build view."""

    name: str
    notes: str | None = None
    underlying: str = "NIFTY"
    lots: int = 1
    short_target_delta: float = 0.225  # near shorts (20-25Δ)
    hedge_target_delta: float = 0.175  # far hedges (15-20Δ)
    near_min_dte: int = 5
    far_min_dte: int = 10
    bias: str = "neutral"  # up | neutral | down (manual skew knob)
    bias_skew: float = 0.05
    entry_time: str = "11:00"
    entry_weekday: int = 0  # 0 = Monday
    recurring: bool = False  # deploy-once; the owner owns the next cycle
    force_entry: bool = False
    adjust_cooldown_min: int = 15
    adjust_close_delta: float = 0.10
    adjust_close_prem_frac: float = 0.25
    min_adjust_dte: int = 3
    profit_target_pct: float = 1.5  # % of broker margin
    stop_loss_pct: float = 1.5
    # Whole-cycle measure for the %-thresholds (see RatioManualDeploy) — new deploys only.
    pnl_basis: str = "total"
    # Two-cadence: deploy default = owner policy (1min); ctor default stays "tick" (recovered
    # runs unchanged — §1). eod_time squares the structure at the near expiry.
    profit_check: str = "1min"
    stop_check: str = "1min"
    exit_margin_basis: str = "entry"  # ₹-threshold anchor: "entry" (frozen once) | "current"
    eod_time: str = "15:20"
    entry_legs: list[dict] | None = None  # manual Build-view override (explicit legs)
    capital: float = 1_000_000
    refresh_seconds: int = 20
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class SmokeTestDeploy(BaseModel):
    """Deploy broker_smoke_test: buy 1 lot of a cheap OTM weekly option OR 1 share of a
    stock, hold ~60s, sell, then the run stops itself — a deliberate end-to-end probe of
    the REAL order path (place → poll → escalate → fill → reconcile → exit). Sizes are
    hard-coded to 1 lot / 1 share in the strategy; mode=LIVE is the whole point but the
    §1 gates (armed ∧ flag ∧ adapter) still decide whether orders are real."""

    leg: str = "option"  # "option" | "stock"
    name: str | None = None
    underlying: str = "NIFTY"  # option leg
    right: str = "CE"  # option leg: CE | PE
    symbol: str = "ITC"  # stock leg
    hold_seconds: int = Field(60, ge=15, le=600)
    target_premium: float = 10.0  # option leg: strike trading nearest this…
    premium_min: float = 5.0  # …within this band
    premium_max: float = 20.0
    capital: float = 50_000
    refresh_seconds: int = Field(10, ge=5)  # fast ticks so the 60s hold is honored ±10s
    mode: str = "PAPER"  # UI requires a typed confirmation for LIVE
    quote_source: str = "zerodha"
    broker_account_id: int | None = None


class CpRatioExpiryDeploy(BaseModel):
    """Deploy call_put_ratio_expiry: expiry-day-only 1:3 premium-ratio seller (buy ATM
    straddle, sell 3× at the ⅓-premium strikes). Needs a live chain for strike selection,
    so a broker quote source is required — cache has no live premiums at 09:20."""

    name: str
    notes: str | None = None
    underlyings: list[str] = Field(default_factory=lambda: ["NIFTY"])
    sets: dict[str, int] = Field(default_factory=dict)  # 1 set = buy1 + sell3 per side
    entry_start: str = "09:20"
    entry_end: str = "09:27"
    eod_exit: str = "15:20"
    profit_target_pct: float = 1.1  # % of margin deployed
    stop_loss_pct: float = 1.0
    ratio_tolerance_pct: float = 30.0
    # Two-cadence model (2026-07-18): live decision sampling. Deploy default = the
    # owner policy (1min); the strategy ctor default stays "tick" so RECOVERED runs
    # keep their recorded behavior (recovery binds params_snapshot, not these).
    profit_check: str = "1min"
    stop_check: str = "1min"
    eod_time: str = "15:20"
    capital: float = 500_000
    refresh_seconds: int = 15
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class IntradayStraddleDeploy(BaseModel):
    """Deploy intraday_straddle: a daily intraday short straddle on the nearest weekly. Sell ATM
    CE+PE at entry_time, exit at exit_time, with a fixed %-of-margin stop and a trailing stop
    (ratchet / below_peak). Live-chain-driven → broker quote source required; no backtest."""

    name: str
    notes: str | None = None
    underlying: str = "NIFTY"  # NIFTY or BANKNIFTY, one per deployment
    lots: int = 1
    strike_delta: float = 0.0  # 0 = ATM straddle; e.g. 0.6 = slight-ITM (by BS delta)
    entry_time: str = "09:18"
    entry_window_end: str = "15:00"
    # 15:20, not 15:25: Zerodha auto-squares intraday F&O at 15:26, and an exit that runs
    # right up against that leaves no room to retry — on 2026-08-11 our 15:25:34 square-off
    # failed and the leg had to be closed by hand. Six minutes of buffer is the point.
    # The CONSTRUCTOR default stays 15:25 (§1: a param-less recovery is unchanged); only new
    # deploys get this. Move a RUNNING deploy with the tile's "Edit params".
    exit_time: str = "15:20"
    stop_loss_pct: float = 2.0  # fixed SL, % of broker margin
    trail_trigger_pct: float = 1.0  # every this much peak profit moves the stop
    trail_step_pct: float = 0.5  # ...by this much (0 on either disables trailing)
    trail_mode: str = "ratchet"  # "ratchet" | "below_peak"
    leg_book_pct: float = 0.0  # buy back a leg alone at this % premium captured (0 = off)
    # Two-cadence model (2026-07-18): live decision sampling. Deploy default = the
    # owner policy (1min); the strategy ctor default stays "tick" so RECOVERED runs
    # keep their recorded behavior (recovery binds params_snapshot, not these).
    profit_check: str = "1min"
    stop_check: str = "1min"
    eod_time: str = "15:20"
    capital: float = 1_000_000
    refresh_seconds: int = 20
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class WeeklyIntradayStraddleDeploy(BaseModel):
    """Deploy weekly_intraday_straddle: a weekly-cycle intraday SHORT straddle (NIFTY). The ATM
    strike is locked once per weekly expiry cycle (09:20 on expiry+1, nearest 100) and traded
    every day: SELL when the combined premium closes below both its VWAP and the prior day's
    intraday low; exit on a VWAP cross-up or 15:25; up to max_entries_per_day. Optional MTM stop
    (% of broker margin), default off. Live-chain + Kite option bars → broker source; no backtest.
    """

    name: str
    notes: str | None = None
    underlying: str = "NIFTY"  # NIFTY only for v1
    lots: int = 1
    entry_start: str = "09:20"  # cycle lock time + daily entry-window open
    entry_cutoff: str = "15:15"  # no fresh entries after this
    eod_exit: str = "15:20"  # hard square-off, clear of Zerodha's 15:26 intraday auto-close
    candle_minutes: int = 5
    max_entries_per_day: int = 3
    stop_loss_pct: float = 0.0  # optional MTM stop, % of broker margin; 0 = OFF
    # Stop-cadence only (no profit-booking decision exists — VWAP exits, bar-driven).
    stop_check: str = "1min"
    eod_time: str = "15:20"
    capital: float = 1_000_000
    # 15s ticks so a 5-min candle close is evaluated promptly (loop clamps to ≥5s).
    refresh_seconds: int = 15
    mode: str = "PAPER"
    quote_source: str = "zerodha"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class MomentumThetaDeploy(BaseModel):
    """Deploy momentum_theta_gainer_intra: intraday 15-min SuperTrend(7,3) + daily-pivot
    ATM weekly option seller on index underlyings (NIFTY, SENSEX). SENSEX is live-only and
    needs a broker quote source — there is no cached BSE data for the cache source to serve."""

    name: str
    notes: str | None = None
    underlyings: list[str] = Field(default_factory=lambda: ["NIFTY"])
    lots: dict[str, int] = Field(default_factory=dict)  # per-underlying lots (default 1)
    st_period: int = 7
    st_multiplier: float = 3.0
    candle_minutes: int = 15
    max_trades_per_day: int = 3
    eod_exit: str = "15:20"
    entry_cutoff: str = "15:00"
    min_dte: int = 0  # 0 → sell the 0DTE weekly on expiry day
    capital: float = 500_000
    # 15s ticks so a candle close is evaluated promptly (loop clamps to ≥5s).
    refresh_seconds: int = 15
    mode: str = "PAPER"
    quote_source: str = "cache"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True


class EquityTradeDeploy(BaseModel):
    """Deploy a single managed equity position (strategy_id=custom_equity)."""

    name: str
    symbol: str
    qty: int = 0  # explicit share count; 0 → size from capital
    capital: float = 1_000_000
    entry_mode: str = "immediate"  # "immediate" | "trigger" (engine-managed GTT)
    trigger_price: float | None = None
    target_pct: float | None = None  # % from entry
    stop_pct: float | None = None  # % from entry
    trailing: bool = False
    trail_pct: float | None = None  # % below the high-water mark
    mode: str = "PAPER"
    quote_source: str = "cache"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True
    notes: str | None = None


class GoLiveRequest(BaseModel):
    """Promote a PAPER deployment to a fresh LIVE one (re-enters per the strategy)."""

    broker_account_id: int
    keep_paper_running: bool = True
    capital: float | None = None  # optional resize (UI deferred)
    lots: int | None = None  # optional resize (UI deferred)


class BrokerConnectRequest(BaseModel):
    broker: str = "zerodha"
    label: str
    api_key: str
    api_secret: str
    user_id: str


class RequestTokenInput(BaseModel):
    request_token: str


class QuoteSourceInput(BaseModel):
    quote_source: str  # "cache" | "zerodha"
    broker_account_id: int | None = None


class LiveControlsInput(BaseModel):
    """Edit a running deployment's loop controls + exclusion list. Null = unchanged."""

    auto: bool | None = None
    ignore_market_hours: bool | None = None
    refresh_seconds: int | None = None
    excluded_symbols: list[str] | None = None  # replaces the no-new-entry blocklist
    lots: int | None = None  # options: scalar lot-sets for the NEXT entry (no resize of open legs)
    lot_sets: dict[str, int] | None = None  # per-underlying lot-sets (momentum_theta / cp_ratio)


class ManualLegClose(BaseModel):
    """Close some/all lot-records of one held option leg (manual option intervention)."""

    symbol: str
    lots: int | None = None  # None = close every lot-record of this symbol


class ManualLegOpen(BaseModel):
    """Open a new option leg on a running deployment (uses the strategy's current expiry)."""

    right: str  # "CE" | "PE"
    strike: float
    lots: int  # lot-sets (× the contract lot size)
    side: str  # "buy" | "sell"


class ManualOrderInput(BaseModel):
    """Option-aware live intervention: close selected legs/lots and/or open new legs now.

    Executes immediately at live prices; afterwards the strategy adopts the resulting book.
    """

    closes: list[ManualLegClose] = Field(default_factory=list)
    opens: list[ManualLegOpen] = Field(default_factory=list)


class AdoptedLeg(BaseModel):
    """One leg the broker has already closed, and the price it settled at."""

    symbol: str
    price: float = Field(gt=0, description="the price this leg actually closed at, ₹")


class AdoptBrokerCloseInput(BaseModel):
    """Book legs the BROKER already closed (manual square-off, MIS auto-square-off) at the
    prices they settled at. Places NO orders — it only makes the platform's book match."""

    legs: list[AdoptedLeg] = Field(min_length=1)


class SetHoldingInput(BaseModel):
    """Force the platform's unit count for one symbol to match the broker. NO orders.

    The escape hatch for an over-count adoption cannot undo (it only ever adds) — see
    LiveRun.set_holding_units. ``units`` is the TARGET, not a delta, so the caller states
    the number it wants to end up with and a repeated call is idempotent."""

    symbol: str = Field(min_length=1)
    units: float = Field(ge=0)


class RefreshCacheInput(BaseModel):
    """Symbols to refresh on the shared session: an explicit list or a named universe.

    ``start_date`` (ISO) backfills from that date — used by "add symbol" to pull full
    history for a name not yet cached; omitted, the service fills only recent gaps.
    """

    symbols: list[str] = Field(default_factory=list)
    universe: str | None = None
    start_date: date | None = None


class DonchianStudyRequest(BaseModel):
    """Pure-price Donchian breakout study over expiry-anchored monthly cycles (Research page).

    Cache-only — no broker session. Range = the previous FULL expiry→expiry window; trade
    window = first trading day after the last monthly expiry → the next monthly expiry.
    """

    universe: str = "nifty50"
    symbols: list[str] = Field(default_factory=list)  # explicit list overrides the universe
    start_date: date = date(2010, 1, 1)
    end_date: date | None = None  # None → today
    buffer_pct: float = 0.5  # breach must clear the edge by this % (live default)
    basis: str = "touch"  # "touch" (day high/low) | "close" (day close)
    max_flips: int = 3  # live deploy default: two rolls, then close the name
    include_index: bool = True  # add the NIFTY 50 row alongside the stocks
    detail: bool = True  # include per-name-per-cycle rows (~10k small rows)


class BsCalibrationRequest(BaseModel):
    """Compare TODAY's Black-Scholes prices (sigma = realized HV) against the LIVE option
    chain for the basket — quantifies the HV-vs-IV gap and suggests the ``vol_multiplier``
    for the synthetic donchian backtest. Read-only (quote fetch only, never orders)."""

    broker_account_id: int
    names: list[str] = Field(default_factory=list)  # empty → nifty50 resolved server-side
    hv_window: int = 20
    r: float = 0.065
    sell_expiry: str | None = None  # ISO; None → resolved like the screener (min_dte=7)
    round_out: bool = False


class LossStudyRequest(BaseModel):
    """Loss-reduction study for batman_ratio_monthly (Research page). Replays batman ONCE
    over the 1-min store, then evaluates candidate loss-cutting rules (trailing / VIX / trend
    / entry-filter) post-hoc over each cycle's reconstructed MTM path, with an in-sample /
    out-of-sample split so a single window can't overfit the thresholds. Cache + store only —
    no broker session, read-only, never an order path."""

    start_date: date = date(2021, 7, 29)
    end_date: date | None = None  # None → the store's last captured day
    oos_start: date = date(2024, 7, 1)  # cycles entered on/after this validate the fit
    capital: float = 1_000_000
    margin_per_lot: float = 300_000
    lots: int = 3


class DeploymentUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None


class BrokerAccountOut(BaseModel):
    id: int
    broker: str
    label: str
    user_id: str | None
    armed: bool
    has_session: bool
    session_expires_at: str | None
    live_trading_enabled: bool
    # Does this broker have a real order surface in Skais? Zerodha and (since 2026-08-21)
    # Dhan do. The Brokers page keys the smoke-test picker off this rather than hard-coding
    # broker names, so the UI and the server can never disagree about what can trade.
    can_place_orders: bool = True


# ------------------------------------------------------------------ portfolio (/portfolio)


class PortfolioHoldingInput(BaseModel):
    """Create/update payload for a tracked asset.

    ``invested``/``units``/``value`` are the SUMMARY-entry fields and are ignored once the
    holding has transactions — the ledger wins, because it is the only record that knows when
    the money went in. ``xirr_pct`` is the owner's own figure off a broker statement; leave it
    null to have the return derived."""

    name: str = Field(min_length=1, max_length=120)
    asset_class: str = Field(pattern="^(stk|etf|mf|us|btc|bank|ppf|epf|gold|re)$")
    kind_override: str | None = Field(
        default=None, pattern="^(equity|debt|gold|realestate|crypto)$"
    )
    invested: float = Field(default=0.0, ge=0)
    units: float | None = Field(default=None, ge=0)
    value: float = Field(default=0.0, ge=0)
    last_price: float | None = Field(default=None, ge=0)
    native_currency: str | None = Field(default=None, max_length=8)
    native_price: float | None = Field(default=None, ge=0)
    native_invested: float | None = Field(default=None, ge=0)
    day_change: float = 0.0
    xirr_pct: float | None = None
    buy_month: str = Field(default="", pattern=r"^(\d{4}-\d{2})?$")
    sync: str = Field(default="manual", pattern="^(auto|manual)$")
    sync_source: str | None = Field(default=None, pattern="^(broker|amfi|global)$")
    sync_ref: str | None = Field(default=None, max_length=64)
    broker_account_id: int | None = None
    # True when units are an aggregate across brokers — a sync then refreshes price only.
    units_locked: bool = False
    # Per-source unit breakdown; a sync rewrites only the slice it just read.
    broker_units: dict[str, float] = Field(default_factory=dict)
    excluded_from_buckets: bool = False
    # Expected annual distribution as a % of value. None = unknown, shown as unknown.
    dividend_yield_pct: float | None = Field(default=None, ge=0, le=100)
    note: str | None = None


class PortfolioTransactionInput(BaseModel):
    """One buy or sell. ``units`` is always positive — ``kind`` carries the direction, so a
    sign error can't quietly turn a sale into a purchase."""

    on_date: date
    # "bonus" is a corporate action, not a trade: units appear, no money moves, and the
    # ledger RE-BASES the open lots rather than adding one (see services/portfolio).
    kind: str = Field(pattern="^(buy|sell|bonus)$")
    units: float = Field(gt=0)
    price: float = Field(ge=0)
    fees: float = Field(default=0.0, ge=0)
    note: str | None = None


class PortfolioTransactionImport(BaseModel):
    """Bulk import for one holding — the paste-a-tradebook path. REPLACES the holding's
    ledger when ``replace`` is set, so re-importing a corrected export doesn't double every
    buy; appends otherwise."""

    holding_id: int
    replace: bool = False
    rows: list[PortfolioTransactionInput]


class PortfolioBucketInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    target_pct: float = Field(default=0.0, ge=0, le=100)
    holding_ids: list[int] = Field(default_factory=list)


class PortfolioScheduleRow(BaseModel):
    """One year's cost, in TODAY's rupees — the only form a person reliably knows."""

    year: int = Field(ge=2000, le=2200)
    amount: float = Field(gt=0)


class PortfolioGoalInput(BaseModel):
    """A goal is a STREAM of outflows: school fees for four years, travel for twenty, a
    wedding twice. ``target_amount``/``target_year`` remain only so an older single-point
    goal still reads."""

    name: str = Field(min_length=1, max_length=80)
    schedule: list[PortfolioScheduleRow] = Field(default_factory=list)
    inflation_pct: float = Field(default=6.0, ge=0, le=25)
    target_amount: float = Field(default=0.0, ge=0)
    target_year: int = Field(default=0, ge=0, le=2200)
    monthly_sip: float = Field(default=0.0, ge=0)
    holding_ids: list[int] = Field(default_factory=list)
    benchmark: str = "NIFTY 50 TRI"


class PortfolioSettingsInput(BaseModel):
    """Target allocations. Percentages are NOT forced to sum to 100 — the page shows the
    shortfall instead, because forcing it would silently rewrite a number the owner typed."""

    class_targets: dict[str, float] | None = None
    kind_targets: dict[str, float] | None = None


class PortfolioSyncInput(BaseModel):
    holding_ids: list[int] | None = None


class PortfolioPasteInput(BaseModel):
    """Raw pasted text for the ledger importer. Parsed server-side so the preview the owner
    approves is produced by the same code that performs the import."""

    text: str = Field(max_length=500_000)


class PortfolioSeedSymbol(BaseModel):
    """One symbol's landing spot in a bulk seed."""

    symbol: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    asset_class: str = Field(pattern="^(stk|etf|mf|us|btc|bank|ppf|epf|gold|re)$")


class PortfolioSeedInput(BaseModel):
    """Create holdings from a pasted multi-symbol sheet and load each one's ledger.

    ``replace`` wipes a matched holding's existing transactions before loading — the safe
    re-import. Without it a second paste doubles every buy."""

    text: str = Field(max_length=2_000_000)
    symbols: list[PortfolioSeedSymbol]
    broker_account_id: int | None = None
    sync: str = Field(default="manual", pattern="^(auto|manual)$")
    replace: bool = True


class PortfolioDividendInput(BaseModel):
    """A distribution that was actually RECEIVED. Expected income is derived from the
    holding's yield, never stored — a typed forecast ages into fiction the moment the
    position changes size."""

    holding_id: int
    on_date: date
    amount: float = Field(gt=0)
    per_unit: float | None = Field(default=None, ge=0)
    note: str | None = None
