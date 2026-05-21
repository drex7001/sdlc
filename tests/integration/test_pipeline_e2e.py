"""End-to-end pipeline integration tests using the mock provider.

These exercise every stage and every governance boundary. They run in CI with
no API keys — the mock provider returns deterministic canned outputs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.approval import ApprovalMode
from pipeline.audit import AuditStore
from pipeline.config import Settings
from pipeline.orchestrator import PipelineError, run_pipeline

pytestmark = pytest.mark.integration


def _settings(sample_target: Path, runs_dir: Path, audit_db: Path) -> Settings:
    base = Settings.load()
    return replace(
        base,
        llm_provider="mock",
        llm_model="mock-v1",
        target_dir=sample_target,
        runs_dir=runs_dir,
        audit_db=audit_db,
        approver="test@local",
    )


def test_happy_path_succeeds(
    sample_target: Path, runs_dir: Path, audit_db: Path, example_spec_path: Path,
) -> None:
    settings = _settings(sample_target, runs_dir, audit_db)
    result = run_pipeline(
        spec_path=example_spec_path,
        settings=settings,
        approval_mode=ApprovalMode.AUTO,
    )
    assert result.status == "succeeded"
    assert result.gate_report is not None
    assert result.gate_report.all_passed
    assert result.gate_report.total == 5
    assert result.metrics is not None
    assert result.metrics["total_tokens"] > 0
    assert result.metrics["total_input_tokens"] > 0
    assert result.metrics["total_output_tokens"] > 0
    assert result.metrics["llm_calls"] >= 3
    assert result.metrics["ac_total"] == 4
    assert result.metrics["ac_covered"] == 4

    # Artifacts on disk
    artifacts = result.artifacts_dir
    for name in (
        "spec.json", "plan.json", "codegen_output.json", "testgen_output.json",
        "gates.json", "approvals.json", "metrics.json", "deployment_evidence.json",
        "change_summary.md", "ac_coverage.json",
    ):
        assert (artifacts / name).exists(), f"missing artifact {name}"

    # SQLite indexes
    store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    run = store.get_run(result.run_id)
    assert run is not None
    assert run["status"] == "succeeded"
    stages = store.stages_for_run(result.run_id)
    stage_names = [s["stage"] for s in stages]
    assert stage_names == ["plan", "codegen", "testgen", "gates"]
    assert all(s["status"] == "succeeded" for s in stages)

    # The sample-target working tree was actually mutated.
    assert (sample_target / "src/sample_app/status.py").exists()
    assert (sample_target / "src/sample_app/rate_limit.py").exists()
    assert (sample_target / "tests/test_status.py").exists()


def test_invalid_spec_aborts_before_any_llm_call(
    tmp_path: Path, sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    bad_spec = tmp_path / "bad.yaml"
    bad_spec.write_text("name: missing-everything-else\n")
    settings = _settings(sample_target, runs_dir, audit_db)
    with pytest.raises(PipelineError) as exc_info:
        run_pipeline(
            spec_path=bad_spec, settings=settings, approval_mode=ApprovalMode.AUTO,
        )
    assert "missing required section" in str(exc_info.value)
    # No run row should exist — we never started a run.
    store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    assert store.list_runs() == []


def test_rejected_approval_halts_run(
    sample_target: Path, runs_dir: Path, audit_db: Path, example_spec_path: Path,
) -> None:
    """Pre-seed a rejection in the audit DB, run in dashboard mode → run is rejected."""
    settings = _settings(sample_target, runs_dir, audit_db)
    settings = replace(settings, approver="reviewer@local")

    # Pre-create the audit DB and seed a rejection that will fire as soon as
    # the planner finishes (dashboard mode polls for it).
    AuditStore(db_path=audit_db, runs_dir=runs_dir)  # creates schema

    import threading
    import time

    def seed_rejection():
        # Wait for plan.json to appear, then reject.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
            runs = store.list_runs(limit=1)
            if runs:
                run_id = runs[0]["run_id"]
                run = store.get_run(run_id)
                if run and run["status"] == "awaiting_approval":
                    from pipeline.audit import RunRecord
                    record = RunRecord(
                        run_id=run_id, artifacts_dir=Path(run["artifacts_dir"]),
                        spec_name=run["spec_name"], spec_hash=run["spec_hash"],
                    )
                    store.record_approval(
                        record, checkpoint="plan", decision="rejected",
                        approver="reviewer@local", comment="no thanks",
                    )
                    return
            time.sleep(0.2)

    t = threading.Thread(target=seed_rejection, daemon=True)
    t.start()

    result = run_pipeline(
        spec_path=example_spec_path,
        settings=settings,
        approval_mode=ApprovalMode.DASHBOARD,
    )
    t.join(timeout=5)
    assert result.status == "rejected"

    store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    run = store.get_run(result.run_id)
    assert run is not None
    assert run["status"] == "rejected"
    # No codegen artifact should exist — we halted before that stage.
    assert not (result.artifacts_dir / "codegen_output.json").exists()


def test_test_apply_failure_is_recorded_on_testgen_stage(
    sample_target: Path, runs_dir: Path, audit_db: Path, example_spec_path: Path,
) -> None:
    """A generated test file collision should not look like a gate-less mystery fail."""
    (sample_target / "tests" / "test_status.py").write_text(
        '"""Existing test file from a previous run."""\n',
        encoding="utf-8",
    )
    settings = _settings(sample_target, runs_dir, audit_db)

    with pytest.raises(PipelineError, match="testgen wanted to create tests/test_status.py"):
        run_pipeline(
            spec_path=example_spec_path,
            settings=settings,
            approval_mode=ApprovalMode.AUTO,
        )

    store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    run = store.list_runs(limit=1)[0]
    assert run["status"] == "failed"
    stages = store.stages_for_run(run["run_id"])
    assert [s["stage"] for s in stages] == ["plan", "codegen", "testgen"]
    assert stages[-1]["status"] == "failed"
    assert "testgen wanted to create tests/test_status.py" in stages[-1]["error"]
    assert store.gates_for_run(run["run_id"]) == []


def test_missing_acceptance_coverage_fails_run(
    monkeypatch: pytest.MonkeyPatch,
    sample_target: Path,
    runs_dir: Path,
    audit_db: Path,
    example_spec_path: Path,
) -> None:
    settings = _settings(sample_target, runs_dir, audit_db)

    def fake_ac_coverage(tests_dir: Path, ac_ids: set[str]) -> dict[str, list[str]]:
        return {
            ac: ([] if ac == "AC-4" else ["test_status.py:1"])
            for ac in ac_ids
        }

    monkeypatch.setattr("pipeline.orchestrator.compute_ac_coverage", fake_ac_coverage)

    with pytest.raises(PipelineError, match="acceptance criteria missing test coverage: AC-4"):
        run_pipeline(
            spec_path=example_spec_path,
            settings=settings,
            approval_mode=ApprovalMode.AUTO,
        )

    store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    run = store.list_runs(limit=1)[0]
    assert run["status"] == "failed"
    assert (Path(run["artifacts_dir"]) / "ac_coverage.json").exists()
