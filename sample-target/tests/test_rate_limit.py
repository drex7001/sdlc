import time
from flask.testing import FlaskClient


def test_rate_limiting(client: FlaskClient) -> None:
    """AC: AC-2"""
    # Make 5 requests within the rate limit
    for _ in range(5):
        response = client.get("/status")
        assert response.status_code == 200

    # 6th request should be rate limited
    response = client.get("/status")
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) == 10

    # Wait for the rate limit to reset
    time.sleep(10)

    # Request should succeed again
    response = client.get("/status")
    assert response.status_code == 200


def test_rate_limiting_different_ips(client: FlaskClient) -> None:
    """AC: AC-4"""
    # Simulate requests from different IPs
    response = client.get("/status", environ_overrides={"REMOTE_ADDR": "192.168.1.1"})
    assert response.status_code == 200
    response = client.get("/status", environ_overrides={"REMOTE_ADDR": "192.168.1.2"})
    assert response.status_code == 200

    # Make 5 requests from the first IP
    for _ in range(5):
        response = client.get("/status", environ_overrides={"REMOTE_ADDR": "192.168.1.1"})
        assert response.status_code == 200

    # 6th request from the first IP should be rate limited
    response = client.get("/status", environ_overrides={"REMOTE_ADDR": "192.168.1.1"})
    assert response.status_code == 429

    # The second IP should still succeed
    response = client.get("/status", environ_overrides={"REMOTE_ADDR": "192.168.1.2"})
    assert response.status_code == 200


def test_status_endpoint(client: FlaskClient) -> None:
    """AC: AC-1"""
    response = client.get("/status")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert isinstance(data["uptime_seconds"], int) and data["uptime_seconds"] >= 0


def test_retry_after_header(client: FlaskClient) -> None:
    """AC: AC-3"""
    # Make 5 requests within the rate limit
    for _ in range(5):
        response = client.get("/status")
        assert response.status_code == 200

    # 6th request should be rate limited
    response = client.get("/status")
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) == 10
