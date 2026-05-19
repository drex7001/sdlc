"""Deterministic governance: restrict writes to plan-approved paths.

This is one of two hard governance boundaries in the pipeline (the other is
the gate suite). LLM output is not trusted; the sandbox enforces the contract
the plan declared before any byte touches disk.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


class SandboxViolation(Exception):
    """A proposed file change violates the sandbox policy."""


def validate_paths(
    *,
    paths: list[str],
    allowed: set[str],
    target_dir: Path,
) -> None:
    """Reject any path that:
      - is absolute,
      - escapes the target directory via .. or symlinks,
      - is not in the plan's approved set.

    Raises SandboxViolation on the first failure; no partial validation.
    """
    target_real = target_dir.resolve()
    for raw in paths:
        if not raw:
            raise SandboxViolation("empty path is not allowed")
        p = PurePosixPath(raw)
        if p.is_absolute():
            raise SandboxViolation(f"absolute paths are not allowed: {raw!r}")
        if ".." in p.parts:
            raise SandboxViolation(f"path traversal is not allowed: {raw!r}")
        if raw not in allowed:
            raise SandboxViolation(
                f"path {raw!r} is not in the plan's impacted_files. "
                f"Approved paths: {sorted(allowed)}"
            )
        # Resolve symlink-free: the path must, when joined with the target dir,
        # remain inside the target dir.
        candidate = (target_dir / raw).resolve()
        try:
            candidate.relative_to(target_real)
        except ValueError as e:
            raise SandboxViolation(
                f"resolved path {candidate} escapes target directory {target_real}"
            ) from e
