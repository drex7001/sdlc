"""Two-checkpoint human approval workflow.

Modes:
  - ``cli``: pause and prompt on stdin (default).
  - ``dashboard``: write awaiting_approval status and poll the audit DB
    until a reviewer records a decision via the dashboard endpoint.
  - ``auto``: auto-approve. Useful for CI / mock runs. Records the approval
    with approver=``auto`` so it remains visible in audit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from ..audit import AuditStore, RunRecord


class ApprovalMode(str, Enum):
    CLI = "cli"
    DASHBOARD = "dashboard"
    AUTO = "auto"


class ApprovalRejected(Exception):
    """Raised when a reviewer rejects an approval checkpoint."""


@dataclass
class ApprovalDecision:
    checkpoint: str
    decision: str
    approver: str


def request_approval(
    *,
    run: RunRecord,
    checkpoint: str,
    mode: ApprovalMode,
    approver: str,
    audit: AuditStore,
    summary: str,
    poll_interval: float = 2.0,
    poll_timeout: float = 1800.0,
) -> ApprovalDecision:
    """Block until a decision is recorded, then return it.

    Persists awaiting_approval state so the dashboard can surface the run
    even if the operator is approving via CLI.
    """
    audit.set_run_status(run.run_id, "awaiting_approval", stage=f"approval:{checkpoint}")

    if mode is ApprovalMode.AUTO:
        decision = "approved"
        audit.record_approval(
            run, checkpoint=checkpoint, decision=decision,
            approver="auto", comment="auto-approved",
        )
        audit.set_run_status(run.run_id, "running")
        return ApprovalDecision(checkpoint=checkpoint, decision=decision, approver="auto")

    if mode is ApprovalMode.CLI:
        print(f"\n=== Approval required: {checkpoint} ===")
        print(summary)
        print(f"Artifacts at: {run.artifacts_dir}")
        answer = input(f"Approve {checkpoint}? [y/N]: ").strip().lower()
        decision = "approved" if answer in {"y", "yes"} else "rejected"
        audit.record_approval(run, checkpoint=checkpoint, decision=decision, approver=approver)
        if decision == "rejected":
            audit.set_run_status(run.run_id, "rejected", stage=f"approval:{checkpoint}")
            raise ApprovalRejected(f"{checkpoint} rejected by {approver}")
        audit.set_run_status(run.run_id, "running")
        return ApprovalDecision(checkpoint=checkpoint, decision=decision, approver=approver)

    # dashboard mode: poll the DB
    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        approvals = audit.approvals_for_run(run.run_id)
        for ap in approvals:
            if ap["checkpoint"] == checkpoint:
                if ap["decision"] == "rejected":
                    audit.set_run_status(run.run_id, "rejected", stage=f"approval:{checkpoint}")
                    raise ApprovalRejected(f"{checkpoint} rejected by {ap['approver']}")
                audit.set_run_status(run.run_id, "running")
                return ApprovalDecision(
                    checkpoint=checkpoint, decision=ap["decision"], approver=ap["approver"]
                )
        time.sleep(poll_interval)
    audit.set_run_status(run.run_id, "failed", stage=f"approval:{checkpoint}")
    raise TimeoutError(f"approval at {checkpoint} timed out after {poll_timeout}s")
