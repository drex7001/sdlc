"""Dashboard live-state helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from dashboard.app import (
    _ac_failed_summary,
    _approval_state,
    _gates_failed_summary,
    _workflow_status,
    post_approval,
)
from pipeline.audit import AuditStore


def _run(status: str, current_stage: str | None) -> dict[str, str | None]:
    return {"status": status, "current_stage": current_stage}


def _by_key(workflow: list[dict[str, str]]) -> dict[str, str]:
    return {step["key"]: step["status"] for step in workflow}


def _store(tmp_path: Path, monkeypatch) -> AuditStore:
    audit_db = tmp_path / "audit.db"
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("PIPELINE_AUDIT_DB", str(audit_db))
    monkeypatch.setenv("PIPELINE_RUNS_DIR", str(runs_dir))
    return AuditStore(db_path=audit_db, runs_dir=runs_dir)


def _start_run(store: AuditStore, tmp_path: Path):
    return store.start_run(
        spec_name="x",
        spec_hash="h",
        spec_source_path=tmp_path / "spec.yaml",
        llm_provider="mock",
        llm_model="mock-v1",
        prompt_version="v1",
        approver="reviewer@local",
        target_dir=tmp_path / "target",
    )


def test_approval_state_uses_current_stage_for_pending_checkpoint() -> None:
    pending, just_approved = _approval_state(_run("awaiting_approval", "approval:plan"), [])

    assert pending == "plan"
    assert just_approved is None


def test_approval_state_reports_just_approved_race_window() -> None:
    approvals = [{"checkpoint": "plan", "decision": "approved"}]

    pending, just_approved = _approval_state(
        _run("awaiting_approval", "approval:plan"),
        approvals,
    )

    assert pending is None
    assert just_approved == "plan"


def test_workflow_does_not_mark_finalize_awaiting_during_plan_race() -> None:
    approvals = [{"checkpoint": "plan", "decision": "approved"}]

    workflow = _by_key(
        _workflow_status(
            stages=[{"stage": "plan", "status": "succeeded"}],
            approvals=approvals,
            gates=[],
            run_status="awaiting_approval",
            current_stage="approval:plan",
        )
    )

    assert workflow["approve_plan"] == "approved"
    assert workflow["approve_finalize"] == "pending"


def test_workflow_marks_finalize_awaiting_only_when_current_stage_says_so() -> None:
    approvals = [{"checkpoint": "plan", "decision": "approved"}]

    workflow = _by_key(
        _workflow_status(
            stages=[
                {"stage": "plan", "status": "succeeded"},
                {"stage": "codegen", "status": "succeeded"},
                {"stage": "testgen", "status": "succeeded"},
                {"stage": "gates", "status": "succeeded"},
            ],
            approvals=approvals,
            gates=[{"gate": "ruff", "status": "passed"}],
            run_status="awaiting_approval",
            current_stage="approval:finalize",
        )
    )

    assert workflow["approve_finalize"] == "awaiting_approval"


def test_gate_failure_summary_uses_final_gate_round_only() -> None:
    gates = [
        {"gate": "policy", "status": "passed"},
        {"gate": "ruff", "status": "failed"},
        {"gate": "pytest", "status": "passed"},
        {"gate": "policy", "status": "passed"},
        {"gate": "ruff", "status": "passed"},
        {"gate": "pytest", "status": "passed"},
    ]

    assert _gates_failed_summary("failed", gates) is None


def test_ac_failure_summary_lists_uncovered_ids() -> None:
    assert _ac_failed_summary(
        "failed",
        {"AC-1": ["test_items.py:10"], "AC-2": []},
    ) == ["AC-2"]


def test_dashboard_rejects_approval_before_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    run = _start_run(store, tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        post_approval(run.run_id, "finalize", decision="approved")

    assert exc_info.value.status_code == 409
    assert store.approvals_for_run(run.run_id) == []


def test_dashboard_records_approval_only_for_current_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    run = _start_run(store, tmp_path)
    store.set_run_status(run.run_id, "awaiting_approval", stage="approval:plan")

    response = post_approval(
        run.run_id,
        "plan",
        decision="approved",
        approver="reviewer@local",
        comment="",
    )

    assert response.status_code == 303
    rows = store.approvals_for_run(run.run_id)
    assert len(rows) == 1
    assert rows[0]["checkpoint"] == "plan"
