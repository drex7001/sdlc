"""Dashboard live-state helpers."""

from __future__ import annotations

from dashboard.app import _approval_state, _workflow_status


def _run(status: str, current_stage: str | None) -> dict[str, str | None]:
    return {"status": status, "current_stage": current_stage}


def _by_key(workflow: list[dict[str, str]]) -> dict[str, str]:
    return {step["key"]: step["status"] for step in workflow}


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
