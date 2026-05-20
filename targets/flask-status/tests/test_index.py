"""Baseline tests that exist before any pipeline run."""

from __future__ import annotations

from sample_app import create_app


def test_index_returns_hello():
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.get_json() == {"message": "hello"}
