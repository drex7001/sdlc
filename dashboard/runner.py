"""Background pipeline launcher for the dashboard UI.

The dashboard process imports ``run_pipeline`` directly (no subprocess) and
runs it on a daemon thread. The orchestrator already writes every audit row
itself, so a crashed thread still leaves a coherent ``failed`` record behind.

Approval mode is hard-coded to DASHBOARD: the orchestrator pauses at each
checkpoint and polls audit.db for an approval the dashboard endpoint will
write. See pipeline/approval/workflow.py.
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
import time
from pathlib import Path

from pipeline.approval import ApprovalMode
from pipeline.audit import AuditStore
from pipeline.config import Settings
from pipeline.intake import load_and_validate
from pipeline.orchestrator import PipelineError, run_pipeline


def launch_run(spec_path: Path, approver: str, target_dir: Path) -> str:
    """Validate the spec, start the pipeline on a background thread, return run_id.

    ``target_dir`` overrides whatever the environment defaults to — every run
    chooses its own target. The override is propagated through an immutable
    per-call ``Settings`` copy, so concurrent launches do not stomp on each
    other (unlike mutating ``os.environ``).

    Raises whatever ``load_and_validate`` raises if the spec is malformed —
    the caller surfaces this back to the form.
    """
    load_and_validate(spec_path)

    base = Settings.load()
    settings = dataclasses.replace(
        base,
        target_dir=target_dir,
        approver=approver or base.approver,
    )

    store = AuditStore(db_path=settings.audit_db, runs_dir=settings.runs_dir)
    # Snapshot the latest run_id so we can detect the new one the thread will
    # create. Single-user demo only; not race-proof under concurrent launches.
    before = store.list_runs(limit=1)
    latest_before = before[0]["run_id"] if before else None

    def _run_safe() -> None:
        with contextlib.suppress(PipelineError):
            run_pipeline(
                spec_path=spec_path,
                settings=settings,
                approval_mode=ApprovalMode.DASHBOARD,
            )

    threading.Thread(target=_run_safe, daemon=True).start()

    for _ in range(50):
        time.sleep(0.1)
        latest = store.list_runs(limit=1)
        if latest and latest[0]["run_id"] != latest_before:
            return latest[0]["run_id"]
    raise RuntimeError("pipeline thread did not register a run within 5s")
