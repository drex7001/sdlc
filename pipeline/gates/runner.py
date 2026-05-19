"""Gate runner. Sequential execution, fail-closed semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..audit import AuditStore, RunRecord
from ..implementation.codegen import GeneratedChanges
from ..planning import Plan
from . import bandit_gate, mypy_gate, policy_gate, pytest_gate, ruff_gate
from .base import GateOutcome


@dataclass
class GateReport:
    outcomes: list[GateOutcome] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(o.passed for o in self.outcomes)

    @property
    def passed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    @property
    def total(self) -> int:
        return len(self.outcomes)


def run_gates(
    *,
    target_dir: Path,
    plan: Plan,
    impl_changes: GeneratedChanges,
    test_changes: GeneratedChanges,
    run: RunRecord,
    audit: AuditStore,
) -> GateReport:
    """Run all gates in order. Continues through failures so the operator
    sees the full report — the pipeline-level decision to halt is taken by
    the orchestrator based on ``GateReport.all_passed``.
    """
    report = GateReport()

    def _record(outcome: GateOutcome) -> None:
        artifact = audit.write_artifact(
            run, f"gate_{outcome.name}.log", outcome.output or "(no output)"
        )
        report.outcomes.append(outcome)
        audit.record_gate(
            run,
            gate=outcome.name,
            status="passed" if outcome.passed else "failed",
            duration_ms=outcome.duration_ms,
            summary=outcome.summary,
            artifact_relpath=str(artifact.relative_to(run.artifacts_dir)),
        )

    # 1. Policy (deterministic, runs first — fast feedback before subprocess gates)
    _record(policy_gate.run(
        impl_changes=impl_changes, test_changes=test_changes,
        plan_impacted=plan.impacted_set(),
    ))
    # 2. Lint
    _record(ruff_gate.run(target_dir))
    # 3. Types
    _record(mypy_gate.run(target_dir))
    # 4. Security
    _record(bandit_gate.run(target_dir))
    # 5. Tests
    _record(pytest_gate.run(target_dir))

    return report
