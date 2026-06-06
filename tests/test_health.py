"""Smoke test for the health endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"]["ok"] is True
    assert "version" in body
