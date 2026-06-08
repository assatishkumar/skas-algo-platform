"""Broker account management: encrypted credential storage, login, arming."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.brokers.zerodha import ZerodhaAdapter, ZerodhaCredentials
from skas_algo.config import get_settings
from skas_algo.db.models import BrokerAccount
from skas_algo.notify import Alert, AlertLevel, build_notifier
from skas_algo.security import decrypt, encrypt


def connect_account(
    session: Session,
    *,
    broker: str,
    label: str,
    api_key: str,
    api_secret: str,
    user_id: str,
    password: str,
    totp_secret: str,
) -> BrokerAccount:
    """Store a broker account with credentials encrypted at rest."""
    account = BrokerAccount(
        broker=broker,
        label=label,
        api_key=api_key,
        user_id=user_id,
        enc_api_secret=encrypt(api_secret),
        enc_password=encrypt(password),
        enc_totp_secret=encrypt(totp_secret),
    )
    session.add(account)
    session.flush()
    return account


def list_accounts(session: Session) -> list[BrokerAccount]:
    return list(session.execute(select(BrokerAccount)).scalars())


def _credentials(account: BrokerAccount) -> ZerodhaCredentials:
    return ZerodhaCredentials(
        api_key=account.api_key or "",
        api_secret=decrypt(account.enc_api_secret) or "",
        user_id=account.user_id or "",
        password=decrypt(account.enc_password) or "",
        totp_secret=decrypt(account.enc_totp_secret) or "",
    )


def make_adapter(account: BrokerAccount) -> ZerodhaAdapter:
    """Build a live adapter for an account (orders still gated by armed + master switch)."""
    return ZerodhaAdapter(
        _credentials(account),
        armed=account.armed,
        live_enabled=get_settings().live_trading_enabled,
    )


def login_account(session: Session, account: BrokerAccount) -> BrokerAccount:
    """Run the broker login flow and persist the session token."""
    notifier = build_notifier()
    adapter = make_adapter(account)
    try:
        sess = adapter.login()
    except Exception as exc:
        notifier.send(Alert(f"Broker login failed: {account.label}", str(exc), AlertLevel.ERROR))
        raise
    account.session_token = encrypt(sess.access_token)
    account.session_expires_at = sess.expires_at
    session.flush()
    notifier.send(Alert(f"Broker connected: {account.label}", level=AlertLevel.SUCCESS))
    return account


def set_armed(session: Session, account: BrokerAccount, armed: bool) -> BrokerAccount:
    account.armed = armed
    session.flush()
    level = AlertLevel.WARNING if armed else AlertLevel.INFO
    state = "ARMED for live orders" if armed else "disarmed"
    build_notifier().send(Alert(f"Account {account.label} {state}", level=level))
    return account


def has_valid_session(account: BrokerAccount) -> bool:
    exp = account.session_expires_at
    if not account.session_token or exp is None:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp > datetime.now(UTC)
