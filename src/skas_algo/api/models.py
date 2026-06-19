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
    instrument_class: str = "STOCK"   # "STOCK" | "DERIV" (options)
    underlying: str | None = None     # DERIV: NIFTY/BANKNIFTY
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

    right: str       # "CE" | "PE"
    strike: float
    side: str        # "buy" | "sell"
    lots: int = 1    # lot-sets (× the contract lot size)


class OptionsTradeDeploy(BaseModel):
    """Deploy a user-built multi-leg option position (strategy_id=custom_options). Percentages
    are entered as whole numbers (e.g. 50 = 50%) and converted to fractions for the strategy."""

    name: str
    underlying: str
    expiry: str                                  # ISO date from the chain
    legs: list[OptionTradeLeg] = Field(default_factory=list)
    lot_size: int = 0                            # explicit contract lot size (required for stock F&O)
    capital: float = 1_000_000
    spot_upper: float | None = None              # exit-all band on the underlying spot
    spot_lower: float | None = None
    target_pct: float | None = None              # combined P&L target, % of net entry premium
    stop_pct: float | None = None
    leg_targets: dict[int, float] | None = None  # {leg_index: %} per-leg premium target
    leg_stops: dict[int, float] | None = None
    mode: str = "PAPER"
    quote_source: str = "cache"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True
    notes: str | None = None


class EquityTradeDeploy(BaseModel):
    """Deploy a single managed equity position (strategy_id=custom_equity)."""

    name: str
    symbol: str
    qty: int = 0                  # explicit share count; 0 → size from capital
    capital: float = 1_000_000
    entry_mode: str = "immediate"  # "immediate" | "trigger" (engine-managed GTT)
    trigger_price: float | None = None
    target_pct: float | None = None   # % from entry
    stop_pct: float | None = None     # % from entry
    trailing: bool = False
    trail_pct: float | None = None    # % below the high-water mark
    mode: str = "PAPER"
    quote_source: str = "cache"
    broker_account_id: int | None = None
    ignore_market_hours: bool = False
    auto: bool = True
    notes: str | None = None


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
    lots: int | None = None  # options: lot-sets for the NEXT entry (doesn't resize open legs)


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
