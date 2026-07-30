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
import re
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
            _copy_offbox_dir(dest)
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


# Snapshot filenames are ``<series>-YYYYMMDD-HHMMSS-ffffff.db``; the series prefix is
# per-box (the Mac's own ``skas_algo``, the VPS's scp'd ``vps-skas_algo``, …).
# Optional .gz: offbox snapshots gzip since 2026-07-30; raw pre-gzip ones prune alongside.
_STAMP_RE = re.compile(r"^(?P<series>.+)-\d{8}-\d{6}-\d{6}\.db(\.gz)?$")


def last_backup_at(database_url: str | None = None) -> datetime | None:
    """Timestamp (from the filename stamp) of the newest on-box snapshot, or None.

    Lets the manager's nightly latch survive restarts: the latch was in-memory only, so
    every dev-evening restart after 16:30 re-fired the "nightly" and shipped another
    ~265MB snapshot to the Drive folder (5 extra on 2026-07-28 alone). The caller checks
    the TIME too — a morning pre-recovery startup backup is stamped before 16:30 and
    must not satisfy the nightly."""
    settings = get_settings()
    try:
        src = _sqlite_path(database_url or settings.database_url)
        if src is None:
            return None
        newest: datetime | None = None
        for f in (src.parent / "backups").glob(f"{src.stem}-*.db"):
            m = _STAMP_RE.match(f.name)
            if not m:
                continue
            try:
                stamp = f.name[len(src.stem) + 1 : -3]  # YYYYMMDD-HHMMSS-ffffff
                at = datetime.strptime(stamp, "%Y%m%d-%H%M%S-%f")
            except ValueError:  # pragma: no cover - foreign name that matched the regex
                continue
            if newest is None or at > newest:
                newest = at
        return newest
    except Exception:  # pragma: no cover - best-effort like the rest of this module
        return None


def _copy_offbox_dir(snapshot: Path) -> None:
    """Native off-box copy + retention for a DIRECTORY destination (a Google Drive for
    Desktop folder — the Drive app ships it to the cloud). Copies STRAIGHT to the final
    name: the old tmp+rename backfired here — Drive starts uploading the ~265MB ``.tmp``
    within seconds, the rename yanked it mid-upload, and Drive orphaned every nightly
    into its lost_and_found (daily complaint, fixed 2026-07-30). A direct copy at worst
    re-uploads once when the file settles; a failed copy unlinks the partial so a
    truncated snapshot can't pose as a backup. Then prunes EVERY snapshot series in the
    folder to the last ``backup_offbox_keep`` — per-series, so the VPS snapshots the box
    scp's into the same folder (``vps-`` prefix) get their own retention instead of
    competing with ours. Best-effort like the rest of this module.

    Snapshots land GZIPPED (``<name>.db.gz``, ~4x smaller — owner choice 2026-07-30:
    30 days × ~270MB raw was heading for ~8GB of Drive); restore = ``gunzip`` first.
    Pre-gzip raw ``.db`` snapshots prune within the same series."""
    import gzip
    import shutil

    settings = get_settings()
    if not settings.backup_offbox_dir:
        return
    try:
        dest_dir = Path(settings.backup_offbox_dir).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (snapshot.name + ".gz")
        try:
            with open(snapshot, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout, length=1 << 20)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        logger.info("off-box dir backup ok: %s", dest.name)
        _prune_offbox_dir(dest_dir, int(settings.backup_offbox_keep))
    except Exception as exc:  # pragma: no cover - same surfacing as the cmd path
        _alert_offbox_failure(snapshot.name, str(exc))


def _prune_offbox_dir(dest_dir: Path, keep: int) -> None:
    """Prune each snapshot SERIES in ``dest_dir`` to its newest ``keep`` files.
    ``keep <= 0`` → keep everything (the pre-2026-07-27 append-only behaviour)."""
    if keep <= 0:
        return
    series: dict[str, list[Path]] = {}
    for f in list(dest_dir.glob("*.db")) + list(dest_dir.glob("*.db.gz")):
        m = _STAMP_RE.match(f.name)
        if m:  # non-snapshot .db files are never touched
            series.setdefault(m.group("series"), []).append(f)
    for snaps in series.values():
        for old in sorted(snaps)[:-keep]:  # timestamped names sort chronologically
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
