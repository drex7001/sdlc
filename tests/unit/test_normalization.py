"""Tests for deterministic generated-code normalization."""

from __future__ import annotations

from pathlib import Path

from pipeline.implementation.codegen_schema import FileChange, GeneratedChanges
from pipeline.implementation.normalization import normalize_python_changes


def test_normalize_python_changes_sorts_imports_without_mutating_target(
    sample_target: Path,
) -> None:
    target_file = sample_target / "src/sample_app/__init__.py"
    original = target_file.read_text(encoding="utf-8")
    changes = GeneratedChanges(
        files=[
            FileChange(
                path="src/sample_app/__init__.py",
                action="modify",
                content='''"""Sample Flask application used as the pipeline's code-gen target."""

from __future__ import annotations

from flask import request, Flask


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": request.method}

    return app
''',
            )
        ],
        summary="candidate",
    )

    normalized = normalize_python_changes(changes=changes, target_dir=sample_target)

    assert target_file.read_text(encoding="utf-8") == original
    assert "from flask import Flask, request" in normalized.files[0].content
    assert "from flask import request, Flask" not in normalized.files[0].content
