"""Integration tests for the items CRUD endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from crud_app import create_app


def test_create_item_minimal():
    """AC-1: POST /items with {"name":"widget"} returns 201 and a JSON body
    containing the same name and a positive integer id.
    """
    client = TestClient(create_app())
    resp = client.post("/items", json={"name": "widget"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "widget"
    assert isinstance(body["id"], int)
    assert body["id"] > 0


def test_create_item_sequential_ids():
    """AC-2: POST /items with {"name":"a"} then {"name":"b"} yields ids 1 and 2
    in that order.
    """
    client = TestClient(create_app())
    resp1 = client.post("/items", json={"name": "a"})
    assert resp1.status_code == 201
    assert resp1.json()["id"] == 1

    resp2 = client.post("/items", json={"name": "b"})
    assert resp2.status_code == 201
    assert resp2.json()["id"] == 2


def test_create_item_missing_name():
    """AC-3: POST /items with a missing name field returns HTTP 422."""
    client = TestClient(create_app())
    resp = client.post("/items", json={})
    assert resp.status_code == 422


def test_create_item_with_description():
    """AC-4: POST /items with {"name":"x","description":"hello"} echoes
    the description back in the response body.
    """
    client = TestClient(create_app())
    resp = client.post(
        "/items", json={"name": "x", "description": "hello"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "x"
    assert body["description"] == "hello"
