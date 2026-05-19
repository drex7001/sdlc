"""Shared fixtures.

Each test that touches the filesystem (pipeline runs, audit store) gets an
isolated tmp_path-backed workspace so tests don't collide and don't leak state
into the real sample-target.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


_PRISTINE_INIT = '''"""Sample Flask application used as the pipeline's code-gen target."""

from __future__ import annotations

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": "hello"}

    return app
'''

# Files the pipeline creates during a run. If the source sample-target was
# left dirty by a previous CLI run, the fixture must drop these so each test
# starts from a clean baseline.
_GENERATED_FILES = (
    "src/sample_app/rate_limit.py",
    "src/sample_app/status.py",
    "tests/test_status.py",
    "tests/test_rate_limit.py",
)


@pytest.fixture
def sample_target(tmp_path: Path) -> Path:
    """Pristine copy of sample-target/ in a tmp dir.

    Defends against a polluted source: any pipeline-generated files in the
    source are pruned, and the baseline __init__.py is rewritten from a
    canonical string. This means tests pass even if a CLI demo run left
    state behind on disk.
    """
    dst = tmp_path / "sample-target"
    shutil.copytree(
        REPO_ROOT / "sample-target", dst,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"),
    )
    for rel in _GENERATED_FILES:
        f = dst / rel
        if f.exists():
            f.unlink()
    (dst / "src/sample_app/__init__.py").write_text(_PRISTINE_INIT, encoding="utf-8")
    return dst


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def audit_db(tmp_path: Path) -> Path:
    return tmp_path / "audit.db"


@pytest.fixture
def example_spec_path() -> Path:
    return REPO_ROOT / "specs" / "example.yaml"
