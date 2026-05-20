#!/usr/bin/env bash
# Reset a bundled pipeline target back to its pristine baseline so a fresh
# pipeline run can re-create the files. Safe to run between demos.
#
# Usage:
#   ./scripts/reset_target.sh                 # reset all targets
#   ./scripts/reset_target.sh flask-status    # reset just one
#   ./scripts/reset_target.sh crud-api
set -euo pipefail

cd "$(dirname "$0")/.."

TARGETS_DIR="targets"

reset_flask_status() {
    local root="${TARGETS_DIR}/flask-status"
    rm -f \
        "${root}/src/sample_app/rate_limit.py" \
        "${root}/src/sample_app/status.py" \
        "${root}/tests/test_status.py" \
        "${root}/tests/test_rate_limit.py" \
        "${root}/tests/test_integration.py"
    rm -rf \
        "${root}/src/sample_app/__pycache__" \
        "${root}/tests/__pycache__" \
        "${root}/.pytest_cache" "${root}/.mypy_cache" "${root}/.ruff_cache" "${root}/.coverage"
    cat > "${root}/src/sample_app/__init__.py" <<'PY'
"""Sample Flask application used as the pipeline's code-gen target."""

from __future__ import annotations

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": "hello"}

    return app
PY
    cat > "${root}/tests/test_index.py" <<'PY'
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
PY
    echo "targets/flask-status reset to clean state."
}

reset_crud_api() {
    local root="${TARGETS_DIR}/crud-api"
    rm -f \
        "${root}/src/crud_app/items.py" \
        "${root}/src/crud_app/store.py" \
        "${root}/tests/test_create_item.py" \
        "${root}/tests/test_items.py" \
        "${root}/tests/test_list_items.py" \
        "${root}/tests/test_delete_item.py"
    rm -rf \
        "${root}/src/crud_app/__pycache__" \
        "${root}/tests/__pycache__" \
        "${root}/.pytest_cache" "${root}/.mypy_cache" "${root}/.ruff_cache" "${root}/.coverage"
    cat > "${root}/src/crud_app/__init__.py" <<'PY'
"""FastAPI CRUD application built up spec-by-spec by the pipeline.

The baseline ships with only a /health endpoint. Each CRUD spec (create / list
/ delete) layered on top adds one route and its accompanying tests.
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="crud-api", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
PY
    cat > "${root}/tests/test_health.py" <<'PY'
"""Baseline tests that exist before any pipeline run."""

from __future__ import annotations

from fastapi.testclient import TestClient

from crud_app import create_app


def test_health_returns_ok():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
PY
    echo "targets/crud-api reset to clean state."
}

case "${1:-all}" in
    flask-status) reset_flask_status ;;
    crud-api)     reset_crud_api ;;
    all)
        reset_flask_status
        reset_crud_api
        ;;
    *)
        echo "Unknown target: $1" >&2
        echo "Usage: $0 [flask-status|crud-api|all]" >&2
        exit 2
        ;;
esac
