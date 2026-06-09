"""ORM domain models for the platform (see docs/PLAN.md → Core domain model).

Note: market OHLC/options data is NOT stored here — that lives in skas-data's DuckDB
cache. This database holds *platform state*: accounts, algos, runs, positions, the
order/fill audit trail, overrides, and alerts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import (
    AlertChannel,
    AlgoStatus,
    InstrumentClass,
    OrderSide,
    OrderStatus,
    OrderType,
    OverrideScope,
    OverrideSource,
    PositionStatus,
    TradingMode,
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BrokerAccount(Base, TimestampMixin):
    """A broker login. Secrets are stored encrypted (Fernet) — never plaintext."""

    __tablename__ = "broker_account"

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(32))  # zerodha | angelone | ...
    label: Mapped[str] = mapped_column(String(64))
    api_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Only the API secret is stored (encrypted). Login is done by the user out-of-band;
    # they paste the request_token, which we exchange for the daily access token.
    enc_api_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Daily session (the exchanged access token, encrypted).
    session_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Must be explicitly armed (plus SKAS_LIVE_TRADING_ENABLED) before a real order fires.
    armed: Mapped[bool] = mapped_column(Boolean, default=False)

    algos: Mapped[list[Algo]] = relationship(back_populates="broker_account")


class Algo(Base, TimestampMixin):
    """A configured algorithm instance (a strategy + params + mode + capital)."""

    __tablename__ = "algo"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy_id: Mapped[str] = mapped_column(String(64))  # e.g. "sst_lifo"
    instrument_class: Mapped[InstrumentClass] = mapped_column(
        Enum(InstrumentClass), default=InstrumentClass.STOCK
    )
    mode: Mapped[TradingMode] = mapped_column(Enum(TradingMode), default=TradingMode.PAPER)
    status: Mapped[AlgoStatus] = mapped_column(Enum(AlgoStatus), default=AlgoStatus.IDLE)
    capital: Mapped[float] = mapped_column(Float, default=0.0)
    params: Mapped[dict] = mapped_column(JSON, default=dict)

    broker_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_account.id"), nullable=True
    )
    broker_account: Mapped[BrokerAccount | None] = relationship(back_populates="algos")

    runs: Mapped[list[AlgoRun]] = relationship(back_populates="algo")
    positions: Mapped[list[Position]] = relationship(back_populates="algo")
    orders: Mapped[list[Order]] = relationship(back_populates="algo")
    overrides: Mapped[list[Override]] = relationship(back_populates="algo")


class AlgoRun(Base, TimestampMixin):
    """One execution session of an algo (a backtest run, or a live/paper trading day)."""

    __tablename__ = "algo_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algo.id"))
    mode: Mapped[TradingMode] = mapped_column(Enum(TradingMode))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    params_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)  # scalars + breakdowns
    trade_log: Mapped[list] = mapped_column(JSON, default=list)  # serialized transactions
    # Live-session snapshot (portfolio/lots, stops, tracking, overrides) so a running
    # paper/live run can be rebuilt after a restart. Null for finished/backtest runs.
    state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    algo: Mapped[Algo] = relationship(back_populates="runs")


class Position(Base, TimestampMixin):
    __tablename__ = "position"

    id: Mapped[int] = mapped_column(primary_key=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algo.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    lots: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[PositionStatus] = mapped_column(
        Enum(PositionStatus), default=PositionStatus.OPEN
    )
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    algo: Mapped[Algo] = relationship(back_populates="positions")


class Order(Base, TimestampMixin):
    __tablename__ = "order"

    id: Mapped[int] = mapped_column(primary_key=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algo.id"))
    # Idempotency: stable client id so restarts never double-fire (see PLAN recommendation #4).
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType), default=OrderType.MARKET)
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    tag: Mapped[str | None] = mapped_column(String(64), nullable=True)

    algo: Mapped[Algo] = relationship(back_populates="orders")
    fills: Mapped[list[Fill]] = relationship(back_populates="order")


class Fill(Base, TimestampMixin):
    __tablename__ = "fill"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("order.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    order: Mapped[Order] = relationship(back_populates="fills")


class Override(Base, TimestampMixin):
    """A rule that modifies the strategy's default exit/sizing decision.

    Satisfies both pre-trade config rules (source=CONFIG) and mid-session live
    intervention (source=LIVE). The engine's resolver reads active rows on each decision.
    """

    __tablename__ = "override"

    id: Mapped[int] = mapped_column(primary_key=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algo.id"))
    scope: Mapped[OverrideScope] = mapped_column(Enum(OverrideScope))
    # Target identifier within the scope (symbol name or position id); null = whole algo.
    target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[OverrideSource] = mapped_column(
        Enum(OverrideSource), default=OverrideSource.CONFIG
    )
    # e.g. {"exit": [{"at_pct": 6, "action": "book", "qty_pct": 50},
    #                {"action": "trail_sl", "trail_pct": 2}]}
    rule: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    algo: Mapped[Algo] = relationship(back_populates="overrides")


class Alert(Base, TimestampMixin):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(primary_key=True)
    algo_id: Mapped[int | None] = mapped_column(ForeignKey("algo.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    channel: Mapped[AlertChannel] = mapped_column(Enum(AlertChannel), default=AlertChannel.IN_APP)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
