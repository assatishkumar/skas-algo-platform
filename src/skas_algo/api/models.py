"""Pydantic request/response models for the API."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class OverrideInput(BaseModel):
    scope: str = Field(description="ALGO | SYMBOL | POSITION")
    target: str | None = None
    rule: dict


class BacktestRequest(BaseModel):
    strategy_id: str
    # Either an explicit symbol list (Custom) or a named universe (expanded server-side).
    symbols: list[str] = Field(default_factory=list)
    universe: str | None = None
    start_date: date
    end_date: date
    capital: float = 2_500_000
    # Strategy-specific knobs (e.g. profit_target, capital_parts, max_lots).
    params: dict = Field(default_factory=dict)
    tax_rate: float = 0.20
    withdrawal_rate: float = 0.0
    lookback: int = 20
    name: str | None = None
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
    strategy_id: str
    mode: str
    started_at: str | None
    metrics: dict


class UniverseOut(BaseModel):
    name: str
    label: str
    count: int  # symbols available in the cache


class BrokerConnectRequest(BaseModel):
    broker: str = "zerodha"
    label: str
    api_key: str
    api_secret: str
    user_id: str
    password: str
    totp_secret: str


class BrokerAccountOut(BaseModel):
    id: int
    broker: str
    label: str
    user_id: str | None
    armed: bool
    has_session: bool
    session_expires_at: str | None
    live_trading_enabled: bool
