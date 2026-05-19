#!/usr/bin/env bash
# Reset the bundled sample-target back to its pristine state so a fresh
# pipeline run can re-create the files. Safe to run between demos.
set -euo pipefail

cd "$(dirname "$0")/.."

rm -f \
    sample-target/src/sample_app/rate_limit.py \
    sample-target/src/sample_app/status.py \
    sample-target/tests/test_status.py \
    sample-target/tests/test_rate_limit.py

rm -rf \
    sample-target/src/sample_app/__pycache__ \
    sample-target/tests/__pycache__ \
    sample-target/.pytest_cache \
    sample-target/.mypy_cache \
    sample-target/.ruff_cache \
    sample-target/.coverage

cat > sample-target/src/sample_app/__init__.py <<'PY'
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

cat > sample-target/tests/test_index.py <<'PY'
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

echo "sample-target reset to clean state."
