"""Restore missed 1-min option-bar days from a remote store (the VPS) over its API.

The VPS trading box captures every trading day (incl. expiry days the Mac can miss) and keeps a
rolling ~7-day window. When the Mac was off for a few days, it pulls the gap day-files from the
VPS over Tailscale HTTPS: list the remote days (``GET /data/options/intraday-store``), diff against
the local store, and download each missing day's raw Parquet (``GET .../intraday-store/day/{day}``),
writing it atomically. Gap-fill by default — never clobbers a local day (the Mac's own capture stays
authoritative) — unless ``overwrite`` re-pulls all remote days. Auth is the single-operator bearer
(``POST /auth/login``); a remote with auth disabled needs no token. See docs/DEPLOY.md.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import requests

from skas_algo.data.option_intraday_store import captured_days, day_path

logger = logging.getLogger(__name__)


def login(base_url: str, password: str, *, timeout: float = 30.0) -> str:
    """``POST /api/v1/auth/login`` → the operator bearer JWT."""
    r = requests.post(
        f"{base_url.rstrip('/')}/api/v1/auth/login", json={"password": password}, timeout=timeout
    )
    r.raise_for_status()
    return r.json()["access_token"]


def restore_from(
    base_url: str,
    *,
    token: str | None = None,
    days: int = 30,
    overwrite: bool = False,
    timeout: float = 60.0,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict:
    """Pull the day-files this box is MISSING (or all, if ``overwrite``) from the remote store.
    Returns {remote, already, restored:[…], skipped:[…], errors:[…]}. Fail-soft per day."""
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    r = requests.get(
        f"{base}/api/v1/data/options/intraday-store",
        params={"days": int(days)},
        headers=headers,
        timeout=timeout,
    )
    r.raise_for_status()
    remote = sorted(d["day"] for d in (r.json().get("days") or []))
    local = set(captured_days())
    want = remote if overwrite else [d for d in remote if d not in local]

    if want:
        day_path(want[0]).parent.mkdir(parents=True, exist_ok=True)  # the store dir
    restored: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for i, d in enumerate(want, 1):
        tmp = day_path(d).with_suffix(".parquet.tmp")
        try:
            resp = requests.get(
                f"{base}/api/v1/data/options/intraday-store/day/{d}",
                headers=headers,
                timeout=timeout,
            )
            if resp.status_code == 404:
                skipped.append(d)  # pruned off the remote's rolling window
                continue
            resp.raise_for_status()
            content = resp.content
            # Parquet files start (and end) with the "PAR1" magic — a cheap corruption guard
            # before the atomic rename (the source is our own write_day output, so this holds).
            if not content.startswith(b"PAR1"):
                raise ValueError("not a parquet file")
            tmp.write_bytes(content)
            tmp.rename(day_path(d))
            restored.append(d)
            if progress is not None:
                progress(d, i, len(want))
        except Exception as exc:  # one bad day never aborts the rest
            errors.append(d)
            logger.warning("restore: %s failed: %s", d, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
    return {
        "remote": len(remote),
        "already": len(local),
        "restored": restored,
        "skipped": skipped,
        "errors": errors,
    }
