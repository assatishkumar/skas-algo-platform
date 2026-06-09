"""Broker account management: encrypted secret storage, request-token session, arming.

Login is out-of-band: the user opens ``login_url`` on Kite, authenticates themselves,
and pastes the resulting request_token, which ``exchange_token`` swaps for the daily
access token. Only the api_secret is stored (encrypted) — no password or TOTP.
"""

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
    session: Session, *, broker: str, label: str, api_key: str, api_secret: str, user_id: str
) -> BrokerAccount:
    """Store a broker account; the API secret is encrypted at rest."""
    account = BrokerAccount(
        broker=broker,
        label=label,
        api_key=api_key,
        user_id=user_id,
        enc_api_secret=encrypt(api_secret),
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
    )


def make_adapter(account: BrokerAccount) -> ZerodhaAdapter:
    """Adapter for an account, resuming a stored access token if present.

    Orders are still gated by ``armed`` + SKAS_LIVE_TRADING_ENABLED.
    """
    adapter = ZerodhaAdapter(
        _credentials(account),
        armed=account.armed,
        live_enabled=get_settings().live_trading_enabled,
    )
    token = decrypt(account.session_token)
    if token:
        adapter.set_access_token(token)
    return adapter


def login_url(account: BrokerAccount) -> str:
    return make_adapter(account).login_url()


def exchange_token(session: Session, account: BrokerAccount, request_token: str) -> BrokerAccount:
    """Exchange a user-supplied request_token for the daily access token; persist it."""
    notifier = build_notifier()
    adapter = make_adapter(account)
    try:
        sess = adapter.exchange_request_token(request_token)
    except Exception as exc:
        notifier.send(Alert(f"Broker session failed: {account.label}", str(exc), AlertLevel.ERROR))
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
