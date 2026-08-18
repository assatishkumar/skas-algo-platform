"""Point-in-time Nifty500 Momentum 50 membership (see mom50_membership.json).

Official NSE constituent lists where a capture exists (Wayback 2024-09 + 2025-02, live
2026-08); methodology replication elsewhere (validated 38-39/50 at the official
checkpoints — sources labelled per rebalance in the JSON). Consumed by the backtest
route's ``pit_universe`` flag and nifty_shop's ``membership`` filter.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).with_name("mom50_membership.json")


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(_PATH.read_text())


def membership_table() -> dict[str, list[str]]:
    """{effective_iso: [symbols]} — semi-annual, sorted keys."""
    return load()["membership"]


def union_members() -> list[str]:
    """Every symbol that was EVER a member — the run's trading universe under
    point-in-time mode (the per-date filter then gates entries)."""
    out: set[str] = set()
    for syms in membership_table().values():
        out.update(syms)
    return sorted(out)
