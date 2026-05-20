"""Test generation sandbox checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.implementation.codegen_schema import FileChange, GeneratedChanges
from pipeline.implementation.sandbox import SandboxViolation
from pipeline.testing.testgen import _validate_test_paths


def _changes(path: str) -> GeneratedChanges:
    return GeneratedChanges(
        files=[FileChange(path=path, action="create", content="def test_x(): pass\n")],
        summary="tests",
    )


def test_testgen_rejects_path_traversal_even_when_plan_allowed(tmp_path: Path) -> None:
    path = "tests/../pyproject.toml"

    with pytest.raises(SandboxViolation, match="traversal"):
        _validate_test_paths(_changes(path), {path}, tmp_path)


def test_testgen_rejects_non_test_path_even_when_plan_allowed(tmp_path: Path) -> None:
    path = "src/test_support.py"

    with pytest.raises(SandboxViolation, match="tests/"):
        _validate_test_paths(_changes(path), {path}, tmp_path)
