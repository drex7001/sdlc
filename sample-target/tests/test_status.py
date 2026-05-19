import time
from flask.testing import FlaskClient


def test_status_endpoint(client: FlaskClient) -> None:
    response = client.get("/status")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert isinstance(data["uptime_seconds"], int) and data["uptime_seconds"] >= 0
