"""Pull the VPS's /portfolio into this box for testing (owner ask 2026-09-25).

The VPS is the authoritative portfolio (CLAUDE.md §8a) and the Mac's tables are dev-only, so
anything portfolio-shaped was tested on an empty book and only met real data on deploy. This
copies the PORTFOLIO tables — and nothing else — from the peer, one way:

- The peer is read over the existing ssh access with a read-only SQLite URI; nothing on the
  VPS is ever written.
- Locally only ``portfolio*`` tables are replaced, inside one transaction, after a DB backup.
  Deployments, runs, accounts and every other table are untouched.
- Broker account ids do NOT mean the same thing on both boxes (VPS 2 = the Dhan account, Mac
  2 = a paper Zerodha account), so each holding's ``broker_account_id`` is remapped to the
  local account of the SAME broker whose label matches ignoring spaces/case, and cleared when
  there is none — a holding must never be read through the wrong account.
- Columns are intersected: a table or column the peer's older schema lacks is simply left
  empty, never an error.

Pair it with ``SKAS_PORTFOLIO_MAINTENANCE=0`` on the Mac so the copy is never repriced,
snapshotted, BIDS-evaluated or notified from here — the VPS stays the only writer of history
and the only box that alerts the owner. Nothing flows back.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from pathlib import Path

logger = logging.getLogger("skas_algo.portfolio_pull")

# Runs ON THE PEER via `venv/bin/python -`: read-only dump of the portfolio tables + accounts.
_DUMP = r'''
import json, sqlite3
c = sqlite3.connect("file:skas_algo.db?mode=ro", uri=True)
c.row_factory = None
out = {"tables": {}, "accounts": []}
names = [r[0] for r in c.execute(
    "select name from sqlite_master where type='table' and name like 'portfolio%'")]
for n in names:
    cur = c.execute(f'select * from "{n}"')
    out["tables"][n] = {"columns": [d[0] for d in cur.description], "rows": cur.fetchall()}
out["accounts"] = c.execute("select id, broker, label from broker_account").fetchall()
print(json.dumps(out, default=str))
'''


def fetch(host: str, repo: str = "~/git/skas-algo-platform", timeout: int = 120) -> dict:
    """Read the peer's portfolio tables over ssh. Read-only on the peer by construction."""
    cmd = ["ssh", "-o", "ConnectTimeout=20", host, f"cd {repo} && venv/bin/python -"]
    res = subprocess.run(cmd, input=_DUMP, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"ssh {host} failed: {res.stderr.strip()[:300]}")
    return json.loads(res.stdout)


def _norm(label: str | None) -> str:
    return "".join(str(label or "").split()).lower()


def account_map(peer_accounts: list, local_accounts: list) -> dict[int, int | None]:
    """peer account id → local account id of the same broker and label (spaces/case
    ignored), else None."""
    local = {(str(b).lower(), _norm(lbl)): lid for lid, b, lbl in local_accounts}
    return {int(pid): local.get((str(b).lower(), _norm(lbl))) for pid, b, lbl in peer_accounts}


def apply(dump: dict, db_path: str | Path, *, dry_run: bool = False) -> dict:
    """Replace the local portfolio tables with the dump. Returns what it did."""
    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        local_tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' and name like 'portfolio%'")]
        local_accounts = con.execute("select id, broker, label from broker_account").fetchall()
        amap = account_map(dump.get("accounts", []), local_accounts)
        report = {"tables": {}, "skipped": [], "accounts": {str(k): v for k, v in amap.items()},
                  "cleared_accounts": 0}
        peer = dump.get("tables", {})
        if dry_run:
            for n in local_tables:
                report["tables"][n] = len(peer.get(n, {}).get("rows", []))
            report["skipped"] = sorted(set(peer) - set(local_tables))
            return report
        con.execute("BEGIN IMMEDIATE")
        con.execute("PRAGMA defer_foreign_keys = ON")
        # children before parents doesn't matter once FKs are deferred, but keep it tidy
        for n in sorted(local_tables, key=lambda t: t == "portfolio_holding"):
            con.execute(f'DELETE FROM "{n}"')
        for n in local_tables:
            t = peer.get(n)
            if not t:
                report["tables"][n] = 0
                continue
            local_cols = [r[1] for r in con.execute(f'PRAGMA table_info("{n}")')]
            cols = [c for c in t["columns"] if c in local_cols]
            idx = [t["columns"].index(c) for c in cols]
            acct_i = cols.index("broker_account_id") if "broker_account_id" in cols else None
            rows = []
            for row in t["rows"]:
                vals = [row[i] for i in idx]
                if acct_i is not None and vals[acct_i] is not None:
                    mapped = amap.get(int(vals[acct_i]))
                    if mapped is None:
                        report["cleared_accounts"] += 1
                    vals[acct_i] = mapped
                rows.append(vals)
            ph = ",".join("?" for _ in cols)
            names = ",".join(f'"{c}"' for c in cols)
            con.executemany(f'INSERT INTO "{n}" ({names}) VALUES ({ph})', rows)
            report["tables"][n] = len(rows)
        report["skipped"] = sorted(set(peer) - set(local_tables))
        con.commit()
        return report
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def pull(host: str, *, dry_run: bool = False) -> dict:
    from skas_algo.config import get_settings
    from skas_algo.services.backup import _sqlite_path, backup_db

    settings = get_settings()
    db_path = _sqlite_path(settings.database_url)
    if db_path is None:
        raise RuntimeError("portfolio-pull needs a SQLite database")
    if settings.portfolio_maintenance and not dry_run:
        raise RuntimeError(
            "refusing to load the real portfolio while this box's portfolio maintenance is ON "
            "— set SKAS_PORTFOLIO_MAINTENANCE=0 first, or it would reprice, snapshot and notify "
            "from here")
    dump = fetch(host)
    if not dry_run:
        snap = backup_db()
        logger.info("portfolio-pull: backup %s", snap)
    return apply(dump, db_path, dry_run=dry_run)
