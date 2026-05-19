"""Custom deterministic policy gate.

This catches misuse the LLM might slip past general lint/security tools:
  - written files outside the plan's impacted_files
  - hardcoded secrets (AWS keys, generic high-entropy tokens)
  - ``eval`` / ``exec`` calls in generated code
  - ``subprocess`` / network calls inside test files
"""

from __future__ import annotations

import re
import time

from ..implementation.codegen import GeneratedChanges
from .base import GateOutcome

AWS_KEY = re.compile(r"AKIA[0-9A-Z]{16}")
GENERIC_SECRET = re.compile(
    r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][A-Za-z0-9+/=_\-]{20,}['\"]"
)
EVAL_EXEC = re.compile(r"\b(?:eval|exec)\s*\(")
TEST_NETWORK = re.compile(r"\b(?:requests\.|urllib\.|httpx\.|socket\.)")
TEST_SUBPROCESS = re.compile(r"\bsubprocess\.")


def run(
    *,
    impl_changes: GeneratedChanges,
    test_changes: GeneratedChanges,
    plan_impacted: set[str],
) -> GateOutcome:
    start = time.perf_counter()
    violations: list[str] = []

    all_changes = list(impl_changes.files) + list(test_changes.files)

    for fc in all_changes:
        if fc.path not in plan_impacted:
            violations.append(f"{fc.path}: not in plan.impacted_files (defence in depth)")

    for fc in impl_changes.files:
        if fc.action == "delete":
            continue
        if AWS_KEY.search(fc.content):
            violations.append(f"{fc.path}: AWS access key detected")
        if GENERIC_SECRET.search(fc.content):
            violations.append(f"{fc.path}: possible hardcoded secret")
        if EVAL_EXEC.search(fc.content):
            violations.append(f"{fc.path}: eval/exec is not allowed")

    for fc in test_changes.files:
        if fc.action == "delete":
            continue
        if TEST_NETWORK.search(fc.content):
            violations.append(f"{fc.path}: network calls are not allowed in tests")
        if TEST_SUBPROCESS.search(fc.content):
            violations.append(f"{fc.path}: subprocess calls are not allowed in tests")
        if EVAL_EXEC.search(fc.content):
            violations.append(f"{fc.path}: eval/exec is not allowed")

    duration_ms = int((time.perf_counter() - start) * 1000)
    if violations:
        return GateOutcome(
            name="policy",
            passed=False,
            duration_ms=duration_ms,
            summary=f"{len(violations)} violation(s)",
            output="\n".join(violations),
        )
    return GateOutcome(
        name="policy",
        passed=True,
        duration_ms=duration_ms,
        summary="no violations",
        output=f"checked {len(all_changes)} files",
    )
