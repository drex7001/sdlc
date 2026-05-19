"""Integration tests for /status endpoint with rate limiting.

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


def test_status_endpoint_rate_limiting(client):
    """AC: AC-2, AC-3, AC-4"""
    env_a = {"REMOTE_ADDR": "192.168.1.1"}
    env_b = {"REMOTE_ADDR": "192.168.1.2"}

    # Test rate limiting for IP A
    for _ in range(5):
        response = client.get("/status", environ_overrides=env_a)
        assert response.status_code == 200

    response = client.get("/status", environ_overrides=env_a)
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) > 0

    # Test independent rate limiting for IP B
    for _ in range(5):
        response = client.get("/status", environ_overrides=env_b)
        assert response.status_code == 200

    response = client.get("/status", environ_overrides=env_b)
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) > 0
