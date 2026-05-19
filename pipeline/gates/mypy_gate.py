"""Mypy type-check gate."""

from __future__ import annotations

from pathlib import Path

from .base import GateOutcome, run_module


def run(target_dir: Path) -> GateOutcome:
    return run_module("mypy", "mypy", ["src"], cwd=target_dir)
