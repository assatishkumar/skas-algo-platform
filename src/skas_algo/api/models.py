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
    symbols: list[str]
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
