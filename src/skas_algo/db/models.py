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
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
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
    # Shared id for runs produced by one multi-run "sweep" backtest (else null).
    batch_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
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


class GreeksSnapshot(Base):
    """A sampled point of an options deployment's live greeks, for history + analytics.

    Written ~once a minute by the live loop (not every tick) so a day of forward-testing
    is a few hundred rows. Holds the aggregate (net delta / IV-weighted) plus a per-leg
    JSON breakdown; greeks are derived from live Zerodha quotes (LTP + index spot + DTE)."""

    __tablename__ = "greeks_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    algo_run_id: Mapped[int] = mapped_column(ForeignKey("algo_run.id"), index=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    spot: Mapped[float | None] = mapped_column(Float, nullable=True)  # live underlying spot
    net_delta: Mapped[float | None] = mapped_column(Float, nullable=True)  # Σ dir·δ·units
    net_iv: Mapped[float | None] = mapped_column(Float, nullable=True)  # units-weighted IV
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # net unrealized P&L (₹)
    legs: Mapped[list] = mapped_column(JSON, default=list)  # [{symbol, iv, delta, pos_delta, ...}]


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


class StrategyTemplate(Base, TimestampMixin):
    """Per-strategy default backtest params, captured from a chosen run ("use this run's
    config as the starting point for new backtests"). One template per strategy_id;
    params are COPIED so the template survives the source run's deletion."""

    __tablename__ = "strategy_template"

    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # source run (informational)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)  # source run's name
    capital: Mapped[float] = mapped_column(Float, default=0.0)
    params: Mapped[dict] = mapped_column(JSON, default=dict)


class Alert(Base, TimestampMixin):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(primary_key=True)
    algo_id: Mapped[int | None] = mapped_column(ForeignKey("algo.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    channel: Mapped[AlertChannel] = mapped_column(Enum(AlertChannel), default=AlertChannel.IN_APP)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- portfolio
# The personal net-worth tracker (/portfolio) — deliberately SEPARATE from the trading
# tables above. Nothing here is traded by the platform: these rows are what the owner
# owns across brokers, funds, banks and physical assets, tracked by hand or refreshed
# from a broker's holdings() where a feed exists.


class PortfolioHolding(Base, TimestampMixin):
    """One tracked asset. Cost basis and units come from the transaction ledger when the
    holding has one, and from the typed ``invested``/``units`` when it does not — PPF, EPF
    and real estate will never have a trade history, so both paths are first-class."""

    __tablename__ = "portfolio_holding"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    asset_class: Mapped[str] = mapped_column(String(16))  # stk|etf|mf|us|btc|bank|ppf|epf|gold|re
    # Overrides the class's default equity/debt/alt bucket — an MF can be a debt fund.
    kind_override: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Summary-entry fields. IGNORED once the holding has transactions (see services/portfolio).
    invested: Mapped[float] = mapped_column(Float, default=0.0)
    units: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    # Last known unit price (a quote's LTP, or an AMFI NAV). With a ledger, value is
    # units x last_price — units come from the transactions, so a stale typed value can
    # never contradict them. Without one, the typed ``value`` stands.
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_asof: Mapped[str | None] = mapped_column(String(10), nullable=True)  # NAV/quote date
    # The sleeve's OWN currency, for anything not quoted in rupees. Everything else on the
    # screen is INR, but a US position is thought about in dollars — and the dollar cost basis
    # cannot be recovered from the rupee one, because those rupees were spent at historical
    # rates, not today's. So it is stored rather than derived.
    native_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    native_price: Mapped[float | None] = mapped_column(Float, nullable=True)     # set by sync
    native_invested: Mapped[float | None] = mapped_column(Float, nullable=True)  # cost basis
    day_change: Mapped[float] = mapped_column(Float, default=0.0)
    # Annualised return typed by the owner (a broker statement's figure). NULL = derive it:
    # a real money-weighted XIRR when a ledger exists, else cost→value over the holding period.
    xirr_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_month: Mapped[str] = mapped_column(String(7), default="")  # "2022-04"; first buy wins
    sync: Mapped[str] = mapped_column(String(8), default="manual")  # auto|manual
    # What a sync reads this holding's price from. "broker" -> sync_ref is the exchange
    # tradingsymbol on ``broker_account_id``; "amfi" -> sync_ref is the fund's ISIN, matched
    # against AMFI's daily NAVAll.txt (the owner's own sheet is keyed by ISIN, so a paste of
    # it lands without translation). NULL source = manual, whatever ``sync`` says.
    sync_source: Mapped[str | None] = mapped_column(String(12), nullable=True)
    sync_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    broker_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_account.id"), nullable=True
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The same stock can sit in several broker accounts, and this row is the AGGREGATE. A sync
    # must then refresh the PRICE only: no single broker's book equals these units, so adopting
    # one would silently replace the total with a fraction of it, and comparing against one
    # would report a mismatch on every pass forever.
    units_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    # …unless the aggregate is broken down per source, which is what makes a LIVE account
    # safe to track inside one. ``{"static": 7757, "account:3": 24}`` — a sync rewrites only
    # the key for the account it just read and re-totals; every other source holds. That is
    # what lets value_investing's daily Dhan buys flow in without the Zerodha and IIFL
    # positions, which no sync can see, being wiped to zero. Empty = the whole row is static.
    broker_units: Mapped[dict] = mapped_column(JSON, default=dict)
    excluded_from_buckets: Mapped[bool] = mapped_column(Boolean, default=False)
    # Expected annual distribution as a % of current value. TYPED, never guessed: no free feed
    # publishes Indian dividend yields, and an invented one would put fictitious income on a
    # planning screen. NULL means unknown, which the UI shows as unknown.
    dividend_yield_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Fixed deposits: the rate and the maturity make the value computable, so it is
    # accrued (quarterly, as Indian banks do) instead of being re-typed and drifting.
    interest_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    maturity_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    tags: Mapped[list[PortfolioTag]] = relationship(
        secondary="portfolio_holding_tag", back_populates="holdings", lazy="selectin",
    )


class PortfolioTransaction(Base, TimestampMixin):
    """A buy or sell against a holding — the source of truth for cost basis, realized gains
    and per-lot holding periods. FIFO: a SELL consumes the oldest open lots first, which is
    what Indian capital-gains rules assume."""

    __tablename__ = "portfolio_transaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    holding_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_holding.id", ondelete="CASCADE"), index=True
    )
    on_date: Mapped[str] = mapped_column(String(10))  # ISO date — the trade date
    kind: Mapped[str] = mapped_column(String(8))  # buy|sell
    units: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PortfolioBucket(Base, TimestampMixin):
    """A user-defined grouping with a target share of bucketed money."""

    __tablename__ = "portfolio_bucket"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    target_pct: Mapped[float] = mapped_column(Float, default=0.0)
    holding_ids: Mapped[list] = mapped_column(JSON, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PortfolioGoal(Base, TimestampMixin):
    """A funding goal — target rupees by a target year, backed by linked holdings and a
    monthly SIP. Progress, ETA and the benchmark delta are all COMPUTED (see
    services/portfolio.goal_view); nothing about a goal's status is stored."""

    __tablename__ = "portfolio_goal"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    # A goal is a STREAM of outflows, not one number at one date: school fees run for four
    # years, travel for twenty, a wedding lands twice. ``schedule`` is [{"year", "amount"}]
    # with amounts in TODAY'S rupees — that is how a person actually knows what a thing costs
    # — and ``inflation_pct`` carries each one forward to the year it is needed.
    schedule: Mapped[list] = mapped_column(JSON, default=list)
    inflation_pct: Mapped[float] = mapped_column(Float, default=6.0)
    # Legacy single-point form, kept so an older goal still reads as a one-row schedule.
    target_amount: Mapped[float] = mapped_column(Float, default=0.0)
    target_year: Mapped[int] = mapped_column(Integer, default=0)
    monthly_sip: Mapped[float] = mapped_column(Float, default=0.0)
    holding_ids: Mapped[list] = mapped_column(JSON, default=list)
    benchmark: Mapped[str] = mapped_column(String(32), default="NIFTY 50 TRI")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PortfolioSnapshot(Base):
    """One row per day, written by the manager's maintenance task — the ONLY source of the
    Growth tab's history. Nothing is back-filled: the chart shows what was actually recorded,
    starting the day tracking began, and says so when that is only a few points.

    ``by_holding`` keys are holding ids as strings (JSON). Class and bucket series are summed
    from it against CURRENT membership, so reclassifying a holding moves its whole history."""

    __tablename__ = "portfolio_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    on_date: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    invested: Mapped[float] = mapped_column(Float, default=0.0)
    by_holding: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PortfolioSetting(Base, TimestampMixin):
    """Single-row-per-key settings: asset-class targets, equity/debt/alt targets. View
    preferences (density, include-real-estate) stay in the browser — they are per-device."""

    __tablename__ = "portfolio_setting"

    key: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class PortfolioDividend(Base, TimestampMixin):
    """One distribution actually received — a dividend, an interest credit, a REIT payout.

    Only RECEIVED money is recorded. What is *expected* is derived from the holding's yield
    and its current value, so it moves with the position instead of ageing into a stale
    forecast that has to be re-entered every time something is bought or sold.
    """

    __tablename__ = "portfolio_dividend"

    id: Mapped[int] = mapped_column(primary_key=True)
    holding_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_holding.id", ondelete="CASCADE"), index=True
    )
    on_date: Mapped[str] = mapped_column(String(10))  # the day it was credited
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    per_unit: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# A holding can carry many tags and a tag many holdings. Deliberately separate from the
# single-valued ``kind_override``: rebalancing needs each rupee counted ONCE, so the asset
# class stays one-per-holding, while tags are free labels for grouping and filtering
# ("child's fund", "long term", "review 2027") that can overlap as much as they like.
portfolio_holding_tag = Table(
    "portfolio_holding_tag",
    Base.metadata,
    Column(
        "holding_id",
        ForeignKey("portfolio_holding.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tag_id", ForeignKey("portfolio_tag.id", ondelete="CASCADE"), primary_key=True),
)


class PortfolioTag(Base, TimestampMixin):
    """A user-defined label. Created freely, applied to any number of holdings."""

    __tablename__ = "portfolio_tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    color: Mapped[str] = mapped_column(String(9), default="#12b3a4")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    holdings: Mapped[list[PortfolioHolding]] = relationship(
        secondary=portfolio_holding_tag, back_populates="tags", lazy="selectin",
    )
