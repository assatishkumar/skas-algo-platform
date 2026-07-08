"""On-box rolling snapshots of the platform SQLite DB.

``skas_algo.db`` is the ONLY copy of live position state, recovery state, and the full
Order/Fill audit trail — a single gitignored file on one disk. This makes cheap, crash-
consistent local snapshots so an accidental corruption/`rm`/bad-migration is recoverable.

``VACUUM INTO`` produces a fully self-contained copy of all COMMITTED data (WAL included),
without blocking writers for more than the copy — safe to run against the live DB while it
trades. On-box copies defend against logical loss (bad migration / rm / corruption); the
optional OFF-box push (``backup_remote_cmd``, wired to the nightly backup) defends against
disk failure by shipping the fresh snapshot to another host / object store.

Postgres URLs are skipped (managed backups belong to the DB server).
"""

from __future__ import annotations

import logging
import shlex
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from skas_algo.config import get_settings

logger = logging.getLogger("skas_algo")


def _sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if not url.get_backend_name().startswith("sqlite") or not url.database:
        return None
    if url.database == ":memory:":
        return None
    return Path(url.database).resolve()


def backup_db(database_url: str | None = None, keep: int | None = None,
              offbox: bool = False) -> Path | None:
    """Snapshot the SQLite DB into ``<db-dir>/backups/`` and prune to the last ``keep``.

    Returns the snapshot path, or None if the DB isn't SQLite / doesn't exist yet.
    Never raises — a failed backup logs and returns None (must not break startup/loop).
    ``offbox=True`` (the nightly backup) also ships the fresh snapshot off the box via
    ``settings.backup_remote_cmd`` if configured; startup backups stay on-box only.
    """
    settings = get_settings()
    keep = int(settings.db_backup_keep if keep is None else keep)
    try:
        src = _sqlite_path(database_url or settings.database_url)
        if src is None or not src.exists():
            return None
        backups = src.parent / "backups"
        backups.mkdir(exist_ok=True)
        # Microsecond stamp: VACUUM INTO refuses an existing target, so back-to-back calls
        # (a restart immediately after the daily backup, or tests) must not collide.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = backups / f"{src.stem}-{stamp}.db"

        conn = sqlite3.connect(str(src))
        try:
            # Parameter binding isn't allowed for VACUUM INTO's target; the path is
            # server-derived (never user input), and we quote-escape defensively.
            safe = str(dest).replace("'", "''")
            conn.execute(f"VACUUM INTO '{safe}'")
        finally:
            conn.close()

        _prune(backups, src.stem, keep)
        logger.info("db backup written: %s", dest.name)
        if offbox:
            _push_offbox(dest)
        return dest
    except Exception:  # pragma: no cover - backups are best-effort
        logger.exception("db backup failed")
        return None


def _prune(backups: Path, stem: str, keep: int) -> None:
    snaps = sorted(backups.glob(f"{stem}-*.db"))  # timestamped names sort chronologically
    for old in snaps[:-keep] if keep > 0 else snaps:
        try:
            old.unlink()
        except OSError:  # pragma: no cover
            pass


def _push_offbox(snapshot: Path) -> None:
    """Run the configured off-box command with the fresh snapshot's path. Best-effort: a
    failure logs + alerts (so silent off-box gaps surface) but never breaks the backup.
    The command is operator-supplied (trusted); ``{path}``/``{name}`` are the only inputs
    and are server-derived."""
    cmd = get_settings().backup_remote_cmd
    if not cmd:
        return
    filled = cmd.replace("{path}", shlex.quote(str(snapshot))).replace(
        "{name}", shlex.quote(snapshot.name))
    try:
        r = subprocess.run(filled, shell=True, capture_output=True, text=True, timeout=1800)
        if r.returncode == 0:
            logger.info("off-box backup ok: %s", snapshot.name)
        else:
            _alert_offbox_failure(snapshot.name, (r.stderr or r.stdout or "").strip()[:300])
    except Exception as exc:  # pragma: no cover - subprocess/timeout issues
        _alert_offbox_failure(snapshot.name, str(exc))


def _alert_offbox_failure(name: str, detail: str) -> None:
    logger.error("off-box backup FAILED for %s: %s", name, detail)
    try:  # pragma: no cover - alert is best-effort
        from skas_algo.notify import Alert, AlertLevel, build_notifier

        build_notifier().send(Alert(
            "Off-box backup failed", f"{name}: {detail}", AlertLevel.ERROR))
    except Exception:
        logger.exception("off-box failure alert could not be sent")
