"""Baseline tests that exist before any pipeline run."""

from __future__ import annotations

from fastapi.testclient import TestClient

from crud_app import create_app


def test_health_returns_ok():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
