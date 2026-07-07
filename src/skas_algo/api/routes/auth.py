"""Authentication — operator login.

Single-user: verify the posted password against the bcrypt hash in
``settings.auth_password_hash`` and return a signed JWT the client presents on every request.
Open route (no ``require_auth``). When auth isn't configured the endpoint reports that, so a
misconfigured host is obvious rather than silently issuing tokens no route checks.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from skas_algo.config import get_settings
from skas_algo.security import create_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    settings = get_settings()
    if not settings.auth_enabled:
        # No hash/secret configured → nothing to log into (the API is open anyway).
        raise HTTPException(status_code=400, detail="authentication is not configured")
    if not verify_password(body.password, settings.auth_password_hash or ""):
        raise HTTPException(status_code=401, detail="incorrect password")
    return LoginResponse(access_token=create_token())
