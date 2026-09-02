"""Add columns the /portfolio tables have gained since a database was first created.

``Base.metadata.create_all`` creates missing TABLES but never missing COLUMNS, so a box whose
portfolio tables were made a week ago starts up fine and then fails on the first query with
"no such column". That is a bad failure: the backend is healthy, the trading engine is
running, and one screen is broken in a way nothing surfaces until someone opens it. It has
already happened twice on the VPS.

Scope is deliberately narrow — the ``portfolio_*`` tables only. These are young and still
gaining columns; nothing in the trading schema is touched, and once the shape settles this
should give way to a proper Alembic migration. SQLite's ``ADD COLUMN`` is cheap, non-locking
and cannot lose data, which is what makes it safe to run on every boot.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# table -> column -> SQL type + default. Append-only: every entry must be safe to apply to a
# table that already holds rows, which means nullable or defaulted, never NOT NULL.
_COLUMNS: dict[str, dict[str, str]] = {
    "portfolio_holding": {
        "native_currency": "VARCHAR(8)",
        "native_price": "FLOAT",
        "native_invested": "FLOAT",
        "units_locked": "BOOLEAN DEFAULT 0",
        "broker_units": "JSON DEFAULT '{}'",
        "dividend_yield_pct": "FLOAT",
        "interest_rate_pct": "FLOAT",
        "maturity_date": "VARCHAR(10)",
        "monthly_contribution": "FLOAT",
        "monthly_income": "FLOAT",
    },
    "portfolio_goal": {
        "schedule": "JSON DEFAULT '[]'",
        "inflation_pct": "FLOAT DEFAULT 6.0",
        "allocations": "JSON DEFAULT '[]'",
        "expected_return_pct": "FLOAT",
    },
}


# Asset classes that were split or renamed, and where their holdings should land. A row on
# a class the code no longer knows is INVISIBLE to allocation and rebalancing while still
# counting toward net worth, and any UI that indexes the class table by it crashes. "bank"
# became cash + fd; a bare "Bank" is more likely a balance than a locked deposit, so it goes
# to cash and can be moved in one edit.
_CLASS_RENAMES = {"bank": "cash"}


def migrate_classes(engine: Engine) -> list[str]:
    """Move holdings off classes that no longer exist. Returns what moved."""
    moved: list[str] = []
    if "portfolio_holding" not in set(inspect(engine).get_table_names()):
        return moved
    for old, new in _CLASS_RENAMES.items():
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT id, name FROM portfolio_holding WHERE asset_class = :old"),
                {"old": old},
            ).fetchall()
            if not rows:
                continue
            conn.execute(
                text("UPDATE portfolio_holding SET asset_class = :new WHERE asset_class = :old"),
                {"old": old, "new": new},
            )
            moved.extend(f"{name} ({old} -> {new})" for _id, name in rows)
    if moved:
        logger.warning("portfolio classes migrated: %s", ", ".join(moved))
    return moved


def ensure_columns(engine: Engine) -> list[str]:
    """Add any missing portfolio columns. Returns what was added, for the log."""
    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, columns in _COLUMNS.items():
        if table not in existing_tables:
            continue  # create_all will build it complete
        have = {c["name"] for c in inspector.get_columns(table)}
        for name, spec in columns.items():
            if name in have:
                continue
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {spec}"))
            added.append(f"{table}.{name}")

    if added:
        logger.warning("portfolio schema: added %s", ", ".join(added))
    return added
