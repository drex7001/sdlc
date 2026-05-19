"""Background pipeline launcher for the dashboard UI.

The dashboard process imports ``run_pipeline`` directly (no subprocess) and
runs it on a daemon thread. The orchestrator already writes every audit row
itself, so a crashed thread still leaves a coherent ``failed`` record behind.

Approval mode is hard-coded to DASHBOARD: the orchestrator pauses at each
checkpoint and polls audit.db for an approval the dashboard endpoint will
write. See pipeline/approval/workflow.py.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from pipeline.approval import ApprovalMode
from pipeline.audit import AuditStore
from pipeline.config import Settings
from pipeline.intake import load_and_validate
from pipeline.orchestrator import PipelineError, run_pipeline


def launch_run(spec_path: Path, approver: str) -> str:
    """Validate the spec, start the pipeline on a background thread, return run_id.

    Raises whatever ``load_and_validate`` raises if the spec is malformed —
    the caller surfaces this back to the form.
    """
    # Pre-flight validation: aborts before any LLM call, matching CLI behaviour.
    load_and_validate(spec_path)

    settings = Settings.load()
    if approver:
        import os
        os.environ["APPROVER"] = approver
        settings = Settings.load()

    store = AuditStore(db_path=settings.audit_db, runs_dir=settings.runs_dir)
    # Snapshot the latest run_id so we can detect the new one the thread will
    # create. Single-user demo only; not race-proof under concurrent launches.
    before = store.list_runs(limit=1)
    latest_before = before[0]["run_id"] if before else None

    def _run_safe() -> None:
        try:
            run_pipeline(
                spec_path=spec_path,
                settings=settings,
                approval_mode=ApprovalMode.DASHBOARD,
            )
        except PipelineError:
            # Orchestrator has already written the failed audit row.
            pass

    threading.Thread(target=_run_safe, daemon=True).start()

    # Poll briefly for the new run_id to appear in audit.db.
    for _ in range(50):  # up to ~5s, generous for sqlite + spec hashing
        time.sleep(0.1)
        latest = store.list_runs(limit=1)
        if latest and latest[0]["run_id"] != latest_before:
            return latest[0]["run_id"]
    raise RuntimeError("pipeline thread did not register a run within 5s")
