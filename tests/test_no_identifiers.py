"""This repository is PUBLIC. Nothing operational or personal may be committed: the VPS's
static IP (whitelisted for orders at the broker), Tailscale addresses and the tailnet name,
broker account labels carrying a person's name or a client id, home-directory paths, and
anything secret-shaped. The identifiers were scrubbed on 2026-09-22 (old commits still show
them — the owner accepted that); this test keeps them from coming back.

Every pattern is assembled from fragments at runtime so this file never contains a literal
that the test itself would flag."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".gz", ".parquet",
           ".db", ".pdf", ".zip"}
_SKIP = {"web/package-lock.json", "web-mobile/package-lock.json", "tests/test_no_identifiers.py"}

# fragments, joined at runtime
_VPS_NET = r"\b" + "13" + r"\." + "205" + r"\.\d{1,3}\.\d{1,3}\b"          # the VPS's /16
_CGNAT = r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"   # Tailscale 100.64/10
_TAILNET = r"\btail" + r"[0-9a-f]{5}\.ts\.net\b"
# a Zerodha client id is 2-3 letters + 3-4 digits, only flagged NEAR a broker word
_CLIENT_ID = (r"\b[A-Z]{2,3}\d{3,4}\b(?=.{0,30}(?:Kite|account))"
              r"|(?:Kite|account).{0,30}\b[A-Z]{2,3}\d{3,4}\b")
_NAMES = [re.compile(x, re.I) for x in (
    "Pri" + "ya",                                  # a private person
    "Sat" + r"ish\s*(?:Kite|Dhan|account)",        # the owner's broker labels
    r"/Users/" + "sat" + "ish",                    # a home-directory path
)]
_SECRET = re.compile(r"(?:api_key|api_secret|access_token|totp(?:_secret)?|password)\s*[:=]\s*"
                     r"[\"'][A-Za-z0-9_\-]{12,}[\"']")
_PATTERNS = [re.compile(p) for p in (_VPS_NET, _CGNAT, _TAILNET, _CLIENT_ID)] + _NAMES


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    files = []
    for rel in out.stdout.split("\n"):
        if not rel or rel in _SKIP or Path(rel).suffix.lower() in _BINARY:
            continue
        p = ROOT / rel
        if p.is_file():
            files.append(p)
    return files


def _scan(files):
    hits = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else p.name
        for i, line in enumerate(text.split("\n"), 1):
            for pat in _PATTERNS:
                if pat.search(line):
                    hits.append(f"{rel}:{i}: {line.strip()[:100]}")
                    break
            if not rel.startswith("tests/") and rel != ".env.example" and _SECRET.search(line):
                hits.append(f"{rel}:{i}: secret-shaped assignment")
    return hits


def test_no_operational_or_personal_identifiers_are_tracked():
    hits = _scan(_tracked_text_files())
    assert not hits, "public repository — remove these before committing:\n" + "\n".join(hits)


def test_the_guard_bites(tmp_path):
    """The patterns must catch the things they exist for (assembled here the same way)."""
    bad = tmp_path / "note.md"
    bad.write_text("\n".join([
        "the box is at " + "13" + "." + "205" + "." + "1" + "." + "2",
        "phone hits http://" + "100" + "." + "66" + "." + "1" + "." + "9" + ":5173",
        "https://mac." + "tail" + "abc12" + ".ts.net",
        "connect " + "Sat" + "ish Kite",
        "Ops Kite (" + "HA" + "K794" + ")",
        'api_key: "' + "q" * 20 + '"',
    ]))
    hits = _scan([bad])
    assert len(hits) == 6, hits
    ok = tmp_path / "fine.md"
    ok.write_text("Ops Kite · NIFTY 24500 CE · 2026-09-29 · https://<vps>.<tailnet>.ts.net\n"
                  'api_key: ""\nlabel = "Main Dhan"\n')
    assert _scan([ok]) == []
