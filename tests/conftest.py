"""Test fixtures. Uses an isolated in-memory SQLite DB so tests never touch dev data."""

from __future__ import annotations

import os

# Configure an in-memory DB before any app module imports settings.
os.environ["SKAS_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["SKAS_ENVIRONMENT"] = "test"

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


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())
