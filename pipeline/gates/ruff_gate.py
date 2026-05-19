"""Ruff lint gate."""

from __future__ import annotations

from pathlib import Path

from .base import GateOutcome, run_module


def run(target_dir: Path) -> GateOutcome:
    return run_module("ruff", "ruff", ["check", "."], cwd=target_dir)
