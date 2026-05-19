"""Shared gate primitives.

Gates invoke their tools via ``sys.executable -m <module>`` so they work
regardless of whether the venv is on PATH — the same Python that runs the
pipeline runs the gate tools.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GateOutcome:
    name: str
    passed: bool
    duration_ms: int
    summary: str
    output: str  # captured stdout + stderr, truncated for storage


def _truncate(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def run_module(
    name: str,
    module: str,
    args: list[str],
    *,
    cwd: Path,
    allow_missing: bool = False,
) -> GateOutcome:
    """Run a Python module gate as ``sys.executable -m <module> <args...>``.

    Pass/fail is driven by exit code. If the module is not importable in the
    current interpreter, the gate is marked failed (or skipped if
    ``allow_missing``).
    """
    start = time.perf_counter()
    if importlib.util.find_spec(module) is None:
        duration_ms = int((time.perf_counter() - start) * 1000)
        if allow_missing:
            return GateOutcome(
                name=name, passed=True, duration_ms=duration_ms,
                summary=f"{module} not installed — gate skipped",
                output="",
            )
        return GateOutcome(
            name=name, passed=False, duration_ms=duration_ms,
            summary=f"{module} is not installed",
            output="",
        )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", module, *args],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    duration_ms = int((time.perf_counter() - start) * 1000)
    output = _truncate((proc.stdout or "") + (proc.stderr or ""))
    return GateOutcome(
        name=name,
        passed=proc.returncode == 0,
        duration_ms=duration_ms,
        summary=f"exit={proc.returncode}",
        output=output,
    )
