"""Pytest gate.

Also parses test files for ``AC: AC-N`` markers and computes coverage of the
spec's acceptance criteria. Coverage is recorded as a metric and surfaced on
the dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import GateOutcome, run_module

AC_TAG = re.compile(r"\b(?:AC:\s*)?(AC-\d+)\b")


def run(target_dir: Path) -> GateOutcome:
    return run_module(
        "pytest", "pytest",
        ["--cov=src", "--cov-report=term-missing", "-q"],
        cwd=target_dir,
    )


def compute_ac_coverage(tests_dir: Path, ac_ids: set[str]) -> dict[str, list[str]]:
    """Return {ac_id: [test_file:line, ...]} for ACs that have at least one test."""
    coverage: dict[str, list[str]] = {ac: [] for ac in ac_ids}
    if not tests_dir.exists():
        return coverage
    for path in tests_dir.rglob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in AC_TAG.finditer(line):
                ac = m.group(1)
                if ac in coverage:
                    coverage[ac].append(f"{path.name}:{lineno}")
    return coverage
