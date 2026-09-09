"""Saved console sessions: one JSON per save under ``~/.skas_data/console/``
(``SKAS_CONSOLE_DIR``; tests get a tmp dir). The `data/nse_universe` precedent — dated,
human-readable, no DB migration. A save is the session's IDENTITY (underlying, day, clock,
expiry, capital, anchor) plus the JOURNAL and the ALERTS: everything `restore` needs, and
nothing derived — the book, the marks and the greeks are rebuilt from the tape on load, so
a saved file can never disagree with the store it replays."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def console_dir() -> Path:
    return Path(os.environ.get("SKAS_CONSOLE_DIR", "~/.skas_data/console")).expanduser()


def _slug(name: str) -> str:
    s = _SAFE.sub("", name).strip().replace(" ", "_")[:60]
    return s or "session"


def save(name: str, payload: dict) -> dict:
    d = console_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"{stamp}_{_slug(name)}.json"
    body = {**payload, "name": name, "saved_at": datetime.now().isoformat(timespec="seconds"),
            "file": fname}
    (d / fname).write_text(json.dumps(body, indent=1))
    return {"file": fname, "name": name, "saved_at": body["saved_at"]}


def load(fname: str) -> dict:
    p = console_dir() / Path(fname).name        # Path(...).name: no directory escapes
    return json.loads(p.read_text())


def delete(fname: str) -> bool:
    p = console_dir() / Path(fname).name
    if p.exists():
        p.unlink()
        return True
    return False


def saved() -> list[dict]:
    d = console_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            j = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        out.append({"file": p.name, "name": j.get("name") or p.stem,
                    "saved_at": j.get("saved_at"), "underlying": j.get("underlying"),
                    "day": j.get("day"), "clock": j.get("clock"),
                    "legs": len({f["symbol"] for f in j.get("journal", [])
                                 if f.get("action") in ("BUY", "SHORT")}),
                    "fills": len(j.get("journal", []))})
    return out
