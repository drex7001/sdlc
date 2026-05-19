"""Baseline tests that exist before any pipeline run."""

from __future__ import annotations

import time
from sample_app import create_app


def test_index_returns_hello():
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.get_json() == {"message": "hello"}


def test_status_endpoint_returns_ok():
    """AC: AC-1"""
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert isinstance(data["uptime_seconds"], int) and data["uptime_seconds"] >= 0


def test_rate_limiting_on_status_endpoint():
    """AC: AC-2"""
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    for _ in range(5):
        resp = client.get("/status")
        assert resp.status_code == 200
    resp = client.get("/status")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) == 10


def test_rate_limiting_resets_after_time_window():
    """AC: AC-2"""
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    for _ in range(5):
        client.get("/status")
    time.sleep(10)
    resp = client.get("/status")
    assert resp.status_code == 200


def test_rate_limiting_is_ip_specific():
    """AC: AC-4"""
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    for _ in range(5):
        client.get("/status")
    # Simulate a request from a different IP
    with client.session_transaction() as sess:
        sess['REMOTE_ADDR'] = '192.168.1.1'
    resp = client.get("/status")
    assert resp.status_code == 200


def test_retry_after_header():
    """AC: AC-3"""
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    for _ in range(5):
        client.get("/status")
    resp = client.get("/status")
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0
