from fastapi.testclient import TestClient

from src.crud_app import create_app
from src.crud_app.items import items_store

app = create_app()
client = TestClient(app)

def setup_function():
    """Reset the items_store before each test."""
    items_store.clear()

def test_create_item_successful():
    """AC: AC-1"""
    response = client.post("/items", json={"name": "widget"})
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "widget"
    assert data["id"] == 1

    response = client.post("/items", json={"name": "gadget"})
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "gadget"
    assert data["id"] == 2


def test_create_item_with_description():
    """AC: AC-4"""
    response = client.post("/items", json={"name": "widget", "description": "A useful widget"})
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "widget"
    assert data["description"] == "A useful widget"
    assert data["id"] == 1


def test_create_item_missing_name():
    """AC: AC-3"""
    response = client.post("/items", json={"description": "A useful widget"})
    assert response.status_code == 422


def test_create_item_id_assignment():
    """AC: AC-2"""
    response = client.post("/items", json={"name": "item1"})
    assert response.status_code == 201
    assert response.json()["id"] == 1

    response = client.post("/items", json={"name": "item2"})
    assert response.status_code == 201
    assert response.json()["id"] == 2
