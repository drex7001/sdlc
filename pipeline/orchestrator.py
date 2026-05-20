"""Pipeline orchestrator.

Wires every stage together. Lives in its own module (not in cli.py) so it is
testable end-to-end without touching argv or stdout.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .approval import ApprovalMode, ApprovalRejected, request_approval
from .audit import AuditStore, RunRecord
from .config import Settings
from .gates import GateReport, run_gates
from .gates.pytest_gate import compute_ac_coverage
from .implementation import GeneratedChanges, apply_changes, generate_code
from .implementation.repair import run_repair_agent
from .intake import FeatureSpec, SpecValidationError, load_and_validate
from .llm import LLMClient, build_client
from .metrics import MetricsRecorder
from .planning import Plan, plan_from_spec
from .testing import generate_tests

PIPELINE_STAGES = ["intake", "plan", "approve_plan", "codegen", "testgen", "gates", "approve_finalize", "finalize"]


@dataclass
class RunResult:
    run_id: str
    status: str
    artifacts_dir: Path
    plan: Plan | None = None
    impl_changes: GeneratedChanges | None = None
    test_changes: GeneratedChanges | None = None
    gate_report: GateReport | None = None
    repair_attempts: int = 0
    error: str | None = None
    metrics: dict[str, Any] | None = None


class PipelineError(Exception):
    """Wraps stage failures so the CLI can render them nicely."""


def run_pipeline(
    *,
    spec_path: Path,
    settings: Settings,
    approval_mode: ApprovalMode,
    audit: AuditStore | None = None,
    llm: LLMClient | None = None,
) -> RunResult:
    """Execute the full pipeline against ``spec_path``.

    Side effects:
      - mutates the working tree at ``settings.target_dir``
      - writes a run directory under ``settings.runs_dir``
      - inserts rows in ``settings.audit_db``
    """
    audit = audit or AuditStore(db_path=settings.audit_db, runs_dir=settings.runs_dir)
    llm = llm or build_client(settings.llm_provider)
    metrics = MetricsRecorder()

    # --- 1. Intake (no audit row yet — we need a valid spec to start the run) -
    try:
        spec: FeatureSpec = load_and_validate(spec_path)
    except SpecValidationError as e:
        raise PipelineError(str(e)) from e

    run = audit.start_run(
        spec_name=spec.name,
        spec_hash=spec.content_hash(),
        spec_source_path=spec_path,
        llm_provider=llm.provider_name,
        llm_model=settings.llm_model,
        prompt_version=settings.prompt_version,
        approver=settings.approver,
        target_dir=settings.target_dir,
    )
    audit.write_json_artifact(run, "spec.json", spec.model_dump())
    shutil.copy(spec_path, run.artifacts_dir / f"spec_source{spec_path.suffix}")

    result = RunResult(
        run_id=run.run_id, status="running", artifacts_dir=run.artifacts_dir
    )

    try:
        # --- 2. Plan ------------------------------------------------------
        plan = _stage(
            audit, run, "plan", metrics,
            lambda: plan_from_spec(
                spec=spec, target_dir=settings.target_dir, llm=llm,
                model=settings.plan_model, prompts_dir=settings.prompts_dir,
                prompt_version=settings.prompt_version, run=run, audit=audit,
                max_tokens=settings.max_tokens,
            ),
        )
        result.plan = plan

        # --- 3. Approval #1 -----------------------------------------------
        request_approval(
            run=run, checkpoint="plan", mode=approval_mode,
            approver=settings.approver, audit=audit,
            summary=(
                f"Plan: {len(plan.tasks)} tasks, "
                f"{len(plan.impacted_files)} files. "
                f"Risks: {len(plan.risks)}.\n"
                f"Design: {plan.design_summary[:300]}"
            ),
        )

        # --- 4. Codegen ---------------------------------------------------
        def _codegen_and_apply() -> GeneratedChanges:
            changes = generate_code(
                spec=spec, plan=plan, target_dir=settings.target_dir, llm=llm,
                model=settings.codegen_model, prompts_dir=settings.prompts_dir,
                prompt_version=settings.prompt_version, run=run, audit=audit,
                max_tokens=settings.max_tokens,
            )
            apply_changes(
                changes=changes, target_dir=settings.target_dir,
                run=run, audit=audit, kind="codegen",
            )
            return changes

        impl_changes = _stage(
            audit, run, "codegen", metrics, _codegen_and_apply,
        )
        result.impl_changes = impl_changes

        # --- 5. Testgen ---------------------------------------------------
        def _testgen_and_apply() -> GeneratedChanges:
            changes = generate_tests(
                spec=spec, plan=plan, impl_changes=impl_changes,
                target_dir=settings.target_dir, llm=llm,
                model=settings.testgen_model, prompts_dir=settings.prompts_dir,
                prompt_version=settings.prompt_version, run=run, audit=audit,
                max_tokens=settings.max_tokens,
            )
            apply_changes(
                changes=changes, target_dir=settings.target_dir,
                run=run, audit=audit, kind="testgen",
            )
            return changes

        test_changes = _stage(
            audit, run, "testgen", metrics, _testgen_and_apply,
        )
        result.test_changes = test_changes

        # --- 6. Gates -----------------------------------------------------
        gate_report = _stage(
            audit, run, "gates", metrics,
            lambda: run_gates(
                target_dir=settings.target_dir, plan=plan,
                impl_changes=impl_changes, test_changes=test_changes,
                run=run, audit=audit,
            ),
        )
        result.gate_report = gate_report

        # --- 6b. Repair loop ----------------------------------------------
        # When gates fail, drive a tool-using agent to inspect the failing
        # logs and propose minimal fixes (bounded by the plan sandbox). Try
        # up to ``settings.max_repair_attempts`` times, re-running gates
        # after each successful re-apply.
        for attempt in range(1, settings.max_repair_attempts + 1):
            if gate_report.all_passed:
                break
            stage_label = f"repair_{attempt}"

            def _repair_and_apply(
                _attempt: int = attempt,
                _label: str = stage_label,
                _failing_report: GateReport = gate_report,
            ) -> GeneratedChanges:
                fixes = run_repair_agent(
                    spec=spec, plan=plan,
                    impl_summary=impl_changes.summary,
                    test_summary=test_changes.summary,
                    gate_report=_failing_report,
                    target_dir=settings.target_dir, llm=llm,
                    model=settings.codegen_model, prompts_dir=settings.prompts_dir,
                    prompt_version=settings.prompt_version, run=run, audit=audit,
                    attempt=_attempt, max_tokens=settings.max_tokens,
                )
                apply_changes(
                    changes=fixes, target_dir=settings.target_dir,
                    run=run, audit=audit, kind=_label,
                )
                return fixes

            _stage(audit, run, stage_label, metrics, _repair_and_apply)
            result.repair_attempts = attempt

            # Re-run gates after the repair. Each attempt's gate results get
            # their own rows; the run-level gate_report tracks the latest.
            gate_report = _stage(
                audit, run, f"gates_after_{stage_label}", metrics,
                lambda: run_gates(
                    target_dir=settings.target_dir, plan=plan,
                    impl_changes=impl_changes, test_changes=test_changes,
                    run=run, audit=audit,
                ),
            )
            result.gate_report = gate_report

        metrics.gates(gate_report.passed_count, gate_report.total)

        ac_coverage = compute_ac_coverage(
            settings.target_dir / "tests",
            {ac.id for ac in spec.acceptance_criteria},
        )
        covered = sum(1 for v in ac_coverage.values() if v)
        metrics.ac(covered, len(spec.acceptance_criteria))
        audit.write_json_artifact(run, "ac_coverage.json", ac_coverage)

        if not gate_report.all_passed:
            failed = [o.name for o in gate_report.outcomes if not o.passed]
            raise PipelineError(
                f"gates failed after {result.repair_attempts} repair attempt(s): {failed}"
            )

        # --- 7. Approval #2 ----------------------------------------------
        request_approval(
            run=run, checkpoint="finalize", mode=approval_mode,
            approver=settings.approver, audit=audit,
            summary=(
                f"All {gate_report.total} gates passed. "
                f"AC coverage: {covered}/{len(spec.acceptance_criteria)}.\n"
                f"Codegen: {impl_changes.summary}\n"
                f"Testgen: {test_changes.summary}"
            ),
        )

        # --- 8. Finalize --------------------------------------------------
        _finalize(run, audit, plan, impl_changes, test_changes, gate_report, ac_coverage)
        audit.set_run_status(run.run_id, "succeeded", stage="finalize")
        result.status = "succeeded"

    except ApprovalRejected as e:
        result.status = "rejected"
        result.error = str(e)
    except Exception as e:
        audit.set_run_status(run.run_id, "failed", stage=None)
        result.status = "failed"
        result.error = str(e)
        raise PipelineError(str(e)) from e
    finally:
        snapshot = metrics.snapshot()
        result.metrics = snapshot
        audit.record_metrics(run, snapshot)

    return result


def _stage(audit: AuditStore, run: RunRecord, name: str, metrics: MetricsRecorder, fn):
    stage_id = audit.start_stage(run.run_id, name)
    start = time.perf_counter()
    try:
        out = fn()
        duration = int((time.perf_counter() - start) * 1000)
        metrics.stage(name, duration)
        audit.finish_stage(stage_id, status="succeeded", duration_ms=duration)
        return out
    except Exception as e:
        duration = int((time.perf_counter() - start) * 1000)
        metrics.stage(name, duration)
        audit.finish_stage(stage_id, status="failed", duration_ms=duration, error=str(e))
        raise


def _finalize(
    run: RunRecord,
    audit: AuditStore,
    plan: Plan,
    impl: GeneratedChanges,
    tests: GeneratedChanges,
    gates: GateReport,
    ac_coverage: dict[str, list[str]],
) -> None:
    """Write the deployment-evidence bundle.

    For the prototype this is a structured JSON artifact listing every
    governance signal collected during the run. A production version would
    additionally produce a signed manifest and tag a git commit.
    """
    evidence = {
        "run_id": run.run_id,
        "plan_summary": plan.design_summary,
        "tasks": [t.model_dump() for t in plan.tasks],
        "implementation": {
            "summary": impl.summary,
            "files": [{"path": f.path, "action": f.action} for f in impl.files],
        },
        "tests": {
            "summary": tests.summary,
            "files": [{"path": f.path, "action": f.action} for f in tests.files],
        },
        "gates": [
            {"gate": o.name, "passed": o.passed, "duration_ms": o.duration_ms, "summary": o.summary}
            for o in gates.outcomes
        ],
        "acceptance_criteria_coverage": ac_coverage,
        "ready_for_deployment": True,
    }
    audit.write_json_artifact(run, "deployment_evidence.json", evidence)
