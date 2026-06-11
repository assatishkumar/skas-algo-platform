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


class BacktestResponse(BaseModel):
    run_id: int
    strategy_id: str
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
