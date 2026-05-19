"""Bandit security gate."""

from __future__ import annotations

from pathlib import Path

from .base import GateOutcome, run_module


def run(target_dir: Path) -> GateOutcome:
    # -q quiet, -ll medium+ severity only (avoid noisy low-severity false positives)
    return run_module(
        "bandit", "bandit", ["-q", "-ll", "-r", "src"], cwd=target_dir
    )
