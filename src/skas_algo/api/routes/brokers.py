"""Broker account endpoints: connect (encrypted), list, login, arm/disarm."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import BrokerAccountOut, BrokerConnectRequest, RequestTokenInput
from skas_algo.config import get_settings
from skas_algo.db.models import BrokerAccount
from skas_algo.security.crypto import EncryptionKeyMissing
from skas_algo.services import broker as broker_svc

router = APIRouter(tags=["brokers"], prefix="/brokers")


def _to_out(account: BrokerAccount) -> BrokerAccountOut:
    return BrokerAccountOut(
        id=account.id,
        broker=account.broker,
        label=account.label,
        user_id=account.user_id,
        armed=account.armed,
        has_session=broker_svc.has_valid_session(account),
        session_expires_at=(
            account.session_expires_at.isoformat() if account.session_expires_at else None
        ),
        live_trading_enabled=get_settings().live_trading_enabled,
    )


def _get(db: Session, account_id: int) -> BrokerAccount:
    account = db.get(BrokerAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="broker account not found")
    return account


@router.post("", response_model=BrokerAccountOut)
def connect(req: BrokerConnectRequest, db: Session = Depends(get_db)) -> BrokerAccountOut:
    try:
        account = broker_svc.connect_account(
            db,
            broker=req.broker,
            label=req.label,
            api_key=req.api_key,
            api_secret=req.api_secret,
            user_id=req.user_id,
        )
    except EncryptionKeyMissing as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_out(account)


@router.get("", response_model=list[BrokerAccountOut])
def list_brokers(db: Session = Depends(get_db)) -> list[BrokerAccountOut]:
    return [_to_out(a) for a in broker_svc.list_accounts(db)]


@router.delete("/{account_id}")
def delete(account_id: int, db: Session = Depends(get_db)) -> dict:
    db.delete(_get(db, account_id))
    return {"deleted": account_id}


@router.get("/{account_id}/login-url")
def login_url(account_id: int, db: Session = Depends(get_db)) -> dict:
    """The Kite URL to open and authenticate; the redirect yields a request_token."""
    account = _get(db, account_id)
    try:
        return {"login_url": broker_svc.login_url(account)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/login", response_model=BrokerAccountOut)
def login(
    account_id: int, body: RequestTokenInput, db: Session = Depends(get_db)
) -> BrokerAccountOut:
    """Exchange the user-supplied request_token for the daily access token."""
    account = _get(db, account_id)
    try:
        broker_svc.exchange_token(db, account, body.request_token.strip())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"token exchange failed: {exc}") from exc
    return _to_out(account)


@router.post("/{account_id}/arm", response_model=BrokerAccountOut)
def arm(account_id: int, db: Session = Depends(get_db)) -> BrokerAccountOut:
    return _to_out(broker_svc.set_armed(db, _get(db, account_id), True))


@router.post("/{account_id}/disarm", response_model=BrokerAccountOut)
def disarm(account_id: int, db: Session = Depends(get_db)) -> BrokerAccountOut:
    return _to_out(broker_svc.set_armed(db, _get(db, account_id), False))
