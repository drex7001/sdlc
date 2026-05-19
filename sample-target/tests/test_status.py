"""Integration tests for /status endpoint.

Each test docstring carries an AC tag (e.g. AC: AC-1) so the pytest gate can
compute acceptance-criteria coverage.
"""

from __future__ import annotations

import pytest

from sample_app import create_app
from sample_app.status import _LIMITER


@pytest.fixture
def client():
    _LIMITER._state.clear()
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_status_returns_ok_and_uptime(client):
    """AC: AC-1"""
    resp = client.get("/status", environ_overrides={"REMOTE_ADDR": "10.0.0.1"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0


def test_sixth_request_same_ip_is_rate_limited(client):
    """AC: AC-2"""
    env = {"REMOTE_ADDR": "10.0.0.2"}
    for _ in range(5):
        assert client.get("/status", environ_overrides=env).status_code == 200
    resp = client.get("/status", environ_overrides=env)
    assert resp.status_code == 429


def test_rate_limited_response_has_retry_after(client):
    """AC: AC-3"""
    env = {"REMOTE_ADDR": "10.0.0.3"}
    for _ in range(5):
        client.get("/status", environ_overrides=env)
    resp = client.get("/status", environ_overrides=env)
    assert resp.status_code == 429
    retry_after = resp.headers.get("Retry-After")
    assert retry_after is not None
    assert int(retry_after) > 0


def test_different_ips_have_independent_quota(client):
    """AC: AC-4"""
    env_a = {"REMOTE_ADDR": "10.0.0.4"}
    env_b = {"REMOTE_ADDR": "10.0.0.5"}
    for _ in range(5):
        assert client.get("/status", environ_overrides=env_a).status_code == 200
    # IP A is now at the limit, IP B should still succeed.
    assert client.get("/status", environ_overrides=env_b).status_code == 200
