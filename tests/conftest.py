"""Test fixtures. Uses an isolated in-memory SQLite DB so tests never touch dev data."""

from __future__ import annotations

import os
import tempfile

# Use a temp-file SQLite DB (shared across threads/connections, unlike :memory:)
# so TestClient's worker thread sees the schema. Set before any app import.
_db_fd, _DB_PATH = tempfile.mkstemp(suffix=".db", prefix="skas_test_")
os.close(_db_fd)
os.environ["SKAS_DATABASE_URL"] = f"sqlite:///{_DB_PATH}"
os.environ["SKAS_ENVIRONMENT"] = "test"
# Tests must NEVER page the owner: the dev .env carries real Telegram credentials, and
# every arm/disarm/login in the API tests fired a REAL message (the 2026-07-07 flood of
# "Account arm ARMED" alerts on the owner's phone). Blank them before any settings load.
os.environ["SKAS_TELEGRAM_BOT_TOKEN"] = ""
os.environ["SKAS_TELEGRAM_CHAT_ID"] = ""
# Integration paths (manager/recovery quote-source construction) must NOT open a real
# KiteTicker WebSocket in tests. The feed's own logic is covered by tests/test_pricefeed.py
# with a fake ticker; here we force the legacy REST path (same reasoning as the notifiers).
os.environ["SKAS_WS_FEED_ENABLED"] = "false"
# Tests must NEVER touch the owner's real off-box backup destinations: the dev .env
# points SKAS_BACKUP_OFFBOX_DIR at the real Google Drive folder, and the backup tests'
# offbox=True calls copied ~30 junk `s-*.db` test snapshots into it (found 2026-07-30).
# Env vars beat .env in pydantic-settings; tests that exercise offbox set their own
# tmp_path destination via monkeypatch.
os.environ["SKAS_BACKUP_OFFBOX_DIR"] = ""
os.environ["SKAS_BACKUP_REMOTE_CMD"] = ""

# A throwaway Fernet key so credential-encryption tests work in isolation.
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("SKAS_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())

import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.db.base import Base, get_engine


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """Create all tables once for the test session."""
    Base.metadata.create_all(get_engine())
    yield
    Base.metadata.drop_all(get_engine())
    if os.path.exists(_DB_PATH):
        os.remove(_DB_PATH)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())
