"""Sandbox: governance boundary around codegen output."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.implementation.sandbox import SandboxViolation, validate_paths


def test_allows_paths_in_plan(tmp_path: Path) -> None:
    allowed = {"src/a.py", "tests/test_a.py"}
    validate_paths(paths=["src/a.py"], allowed=allowed, target_dir=tmp_path)


def test_rejects_path_outside_plan(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="not in the plan"):
        validate_paths(paths=["src/secret.py"], allowed={"src/a.py"}, target_dir=tmp_path)


def test_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="traversal"):
        validate_paths(paths=["../etc/passwd"], allowed={"../etc/passwd"}, target_dir=tmp_path)


def test_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="absolute"):
        validate_paths(paths=["/etc/passwd"], allowed={"/etc/passwd"}, target_dir=tmp_path)


def test_rejects_empty(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation, match="empty"):
        validate_paths(paths=[""], allowed=set(), target_dir=tmp_path)


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside the target that points outside must be rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    link = target / "escape"
    link.symlink_to(outside)
    with pytest.raises(SandboxViolation, match="escapes"):
        validate_paths(
            paths=["escape/anything.py"],
            allowed={"escape/anything.py"},
            target_dir=target,
        )
