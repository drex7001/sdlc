import pytest
from flask import json
from src.sample_app import create_app

@pytest.fixture
def client():
    app = create_app()
    with app.test_client() as client:
        yield client


def test_status_endpoint(client):
    """AC: AC-1"""
    response = client.get('/status')
    assert response.status_code == 200
    data = json.loads(response.data)
    assert data['status'] == 'ok'
    assert 'uptime_seconds' in data
    assert data['uptime_seconds'] >= 0


def test_rate_limiting(client):
    """AC: AC-2"""
    ip = '192.168.1.1'
    for _ in range(5):
        client.get('/status', environ_overrides={'REMOTE_ADDR': ip})
    response = client.get('/status', environ_overrides={'REMOTE_ADDR': ip})
    assert response.status_code == 429


def test_retry_after_header(client):
    """AC: AC-3"""
    ip = '192.168.1.2'
    for _ in range(5):
        client.get('/status', environ_overrides={'REMOTE_ADDR': ip})
    response = client.get('/status', environ_overrides={'REMOTE_ADDR': ip})
    assert response.status_code == 429
    assert 'Retry-After' in response.headers
    assert int(response.headers['Retry-After']) > 0


def test_independent_ip_tracking(client):
    """AC: AC-4"""
    ip1 = '192.168.1.3'
    ip2 = '192.168.1.4'
    for _ in range(5):
        client.get('/status', environ_overrides={'REMOTE_ADDR': ip1})
    response1 = client.get('/status', environ_overrides={'REMOTE_ADDR': ip1})
    assert response1.status_code == 429
    response2 = client.get('/status', environ_overrides={'REMOTE_ADDR': ip2})
    assert response2.status_code == 200
