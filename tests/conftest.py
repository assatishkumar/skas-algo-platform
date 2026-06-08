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
