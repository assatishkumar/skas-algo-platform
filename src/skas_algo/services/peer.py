"""A READ-ONLY client for a peer backend (the VPS, over Tailscale) — 2026-09-18.

Why: the 1-min option store lives on the Mac data box while the real deployments live on
the VPS, so forking a VPS cycle into the Mac's console needs the VPS's trade log here.
This module is GET-only by construction and pinned by `tests/test_peer.py`: ONE transport
function, a whitelist of paths, and a source-text test that no other HTTP verb appears.
It can list deployments, read a run's cycles and its trades — nothing that changes state,
nothing near the order path (CLAUDE.md §1).

Auth: the peer's operator JWT, minted on the peer with `skas-algo mint-token --days 365`
and kept here as `SKAS_PEER_API_TOKEN`. A peer with auth disabled needs no token.
"""

from __future__ import annotations

import logging
import re

import httpx

from skas_algo.config import get_settings

logger = logging.getLogger("skas_algo.peer")

# the ONLY paths this module may fetch — all of them read-only surfaces
_ALLOWED = (
    re.compile(r"^/live/deployments$"),
    re.compile(r"^/live/\d+/trades$"),
    re.compile(r"^/runs/\d+/cycles$"),
)
_transport: httpx.BaseTransport | None = None     # tests inject httpx.MockTransport


class PeerError(Exception):
    """The peer is unconfigured, unreachable, refused the token, or answered badly."""


def configured() -> bool:
    return bool(get_settings().peer_api_url)


def base_url() -> str | None:
    url = get_settings().peer_api_url
    return url.rstrip("/") if url else None


def _get(path: str):
    """The one transport. A path outside the whitelist raises BEFORE any I/O."""
    if not any(p.match(path) for p in _ALLOWED):
        raise PeerError(f"peer path not allowed: {path}")
    s = get_settings()
    url = base_url()
    if not url:
        raise PeerError("no peer configured (SKAS_PEER_API_URL)")
    headers = {"Authorization": f"Bearer {s.peer_api_token}"} if s.peer_api_token else {}
    try:
        with httpx.Client(base_url=url + "/api/v1", headers=headers,
                          timeout=float(s.peer_api_timeout_s), transport=_transport) as c:
            r = c.get(path)
    except httpx.HTTPError as exc:
        raise PeerError(f"peer {url} unreachable: {exc.__class__.__name__}") from exc
    if r.status_code == 401:
        raise PeerError("peer token rejected — mint a new one (skas-algo mint-token)")
    if r.status_code == 404:
        raise PeerError(f"peer has no {path} — update the peer backend")
    if r.status_code >= 400:
        raise PeerError(f"peer answered {r.status_code} for {path}")
    try:
        return r.json()
    except ValueError as exc:
        raise PeerError(f"peer answered non-JSON for {path}") from exc


def deployments() -> list[dict]:
    out = _get("/live/deployments")
    return list(out) if isinstance(out, list) else []


def trades(run_id: int) -> list[dict]:
    out = _get(f"/live/{int(run_id)}/trades")
    return list(out.get("trades") or []) if isinstance(out, dict) else []


def run_cycles(run_id: int) -> dict:
    out = _get(f"/runs/{int(run_id)}/cycles")
    if not isinstance(out, dict):
        raise PeerError("peer answered an unexpected shape for the cycle list")
    return out
