"""Deterministic normalization for generated Python changes."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .codegen_schema import FileChange, GeneratedChanges

_IGNORED_TREE_NAMES = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
}


def normalize_python_changes(
    *,
    changes: GeneratedChanges,
    target_dir: Path,
) -> GeneratedChanges:
    """Return changes after running ruff auto-fixes in an isolated workspace."""
    with changed_target_workspace(target_dir=target_dir, changes=changes) as workspace:
        return normalize_python_changes_in_workspace(changes=changes, workspace=workspace)


@contextmanager
def changed_target_workspace(
    *,
    target_dir: Path,
    changes: GeneratedChanges,
    strict: bool = False,
) -> Iterator[Path]:
    """Yield a temp copy of target_dir with changes applied."""
    with tempfile.TemporaryDirectory(prefix="pipeline-candidate-") as tmp:
        workspace = Path(tmp) / "target"
        shutil.copytree(
            target_dir,
            workspace,
            ignore=shutil.ignore_patterns(*_IGNORED_TREE_NAMES),
        )
        _apply_changes_to_workspace(changes=changes, workspace=workspace, strict=strict)
        yield workspace


def normalize_python_changes_in_workspace(
    *,
    changes: GeneratedChanges,
    workspace: Path,
) -> GeneratedChanges:
    """Run ruff --fix in an already-prepared workspace and return final content."""
    py_paths = _python_file_paths(changes)
    if py_paths:
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "ruff", "check", "--fix", *py_paths],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
    return _changes_from_workspace(changes=changes, workspace=workspace)


def _apply_changes_to_workspace(
    *,
    changes: GeneratedChanges,
    workspace: Path,
    strict: bool,
) -> None:
    for change in changes.files:
        target = workspace / change.path
        if change.action == "delete":
            if target.exists():
                target.unlink()
            continue

        if strict:
            if change.action == "create" and target.exists():
                raise FileExistsError(f"create target already exists: {change.path}")
            if change.action == "modify" and not target.exists():
                raise FileNotFoundError(f"modify target does not exist: {change.path}")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.content, encoding="utf-8")


def _changes_from_workspace(*, changes: GeneratedChanges, workspace: Path) -> GeneratedChanges:
    normalized: list[FileChange] = []
    for change in changes.files:
        if change.action == "delete":
            normalized.append(change)
            continue
        content = (workspace / change.path).read_text(encoding="utf-8")
        normalized.append(change.model_copy(update={"content": content}))
    return changes.model_copy(update={"files": normalized})


def _python_file_paths(changes: GeneratedChanges) -> list[str]:
    return [
        change.path
        for change in changes.files
        if change.action != "delete" and PurePosixPath(change.path).suffix == ".py"
    ]
