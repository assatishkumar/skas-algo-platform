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
    decision_time: str = "15:20"
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
    # Two-cadence: deploy default = owner policy (1min); ctor default stays "tick" (recovered
    # runs unchanged — §1). eod_time squares the structure at the near expiry.
    profit_check: str = "1min"
    stop_check: str = "1min"
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
    exit_time: str = "15:25"
    stop_loss_pct: float = 2.0  # fixed SL, % of broker margin
    trail_trigger_pct: float = 1.0  # every this much peak profit moves the stop
    trail_step_pct: float = 0.5  # ...by this much (0 on either disables trailing)
    trail_mode: str = "ratchet"  # "ratchet" | "below_peak"
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
    entry_cutoff: str = "15:20"  # no fresh entries after this
    eod_exit: str = "15:25"  # hard intraday square-off
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
