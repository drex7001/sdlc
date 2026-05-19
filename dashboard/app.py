"""Minimal read-mostly dashboard.

Endpoints:
  GET  /                          → run list
  GET  /runs/{run_id}             → run detail with stages, gates, approvals, metrics
  GET  /runs/{run_id}/artifact    → ?path=<rel> serves a single artifact file
  POST /runs/{run_id}/approve/{checkpoint}  → record a decision (approve/reject)

No auth (prototype only — documented limitation).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pipeline.audit import AuditStore, RunRecord
from pipeline.config import Settings

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Spec-driven Pipeline Dashboard")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _store() -> AuditStore:
    s = Settings.load()
    return AuditStore(db_path=s.audit_db, runs_dir=s.runs_dir)


def _run_or_404(store: AuditStore, run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/", response_class=HTMLResponse)
def runs_list(request: Request) -> HTMLResponse:
    store = _store()
    runs = store.list_runs(limit=50)
    return templates.TemplateResponse(
        request, "runs_list.html", {"runs": runs}
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str) -> HTMLResponse:
    store = _store()
    run = _run_or_404(store, run_id)
    stages = store.stages_for_run(run_id)
    gates = store.gates_for_run(run_id)
    approvals = store.approvals_for_run(run_id)
    metrics = store.metrics_for_run(run_id)

    artifacts_dir = Path(run["artifacts_dir"])
    artifacts: list[str] = []
    if artifacts_dir.exists():
        for p in sorted(artifacts_dir.rglob("*")):
            if p.is_file():
                artifacts.append(str(p.relative_to(artifacts_dir)))

    # Highlight which checkpoints are currently awaiting approval.
    pending_checkpoints = []
    if run["status"] == "awaiting_approval":
        existing = {a["checkpoint"] for a in approvals}
        for cp in ("plan", "finalize"):
            if cp not in existing:
                pending_checkpoints.append(cp)
                break

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "stages": stages,
            "gates": gates,
            "approvals": approvals,
            "metrics": metrics,
            "artifacts": artifacts,
            "pending_checkpoints": pending_checkpoints,
        },
    )


@app.get("/runs/{run_id}/artifact", response_class=PlainTextResponse)
def run_artifact(run_id: str, path: str) -> PlainTextResponse:
    store = _store()
    run = _run_or_404(store, run_id)
    artifacts_dir = Path(run["artifacts_dir"]).resolve()
    target = (artifacts_dir / path).resolve()
    if not str(target).startswith(str(artifacts_dir)):
        raise HTTPException(status_code=400, detail="path escapes artifacts dir")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    import contextlib
    content = target.read_text(encoding="utf-8")
    # Pretty-print JSON for easier reading.
    if target.suffix == ".json":
        with contextlib.suppress(json.JSONDecodeError):
            content = json.dumps(json.loads(content), indent=2)
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@app.post("/runs/{run_id}/approve/{checkpoint}")
def post_approval(
    run_id: str,
    checkpoint: str,
    decision: str = Form(...),
    approver: str = Form("dashboard@local"),
    comment: str = Form(""),
) -> RedirectResponse:
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="invalid decision")
    if checkpoint not in {"plan", "finalize"}:
        raise HTTPException(status_code=400, detail="invalid checkpoint")
    store = _store()
    run = _run_or_404(store, run_id)
    record = RunRecord(
        run_id=run_id,
        artifacts_dir=Path(run["artifacts_dir"]),
        spec_name=run["spec_name"],
        spec_hash=run["spec_hash"],
    )
    store.record_approval(
        record, checkpoint=checkpoint, decision=decision,
        approver=approver, comment=comment or None,
    )
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)
