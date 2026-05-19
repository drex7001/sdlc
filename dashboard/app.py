"""Dashboard: drive the full pipeline workflow through a browser.

Endpoints:
  GET  /                                    → run list + CTA to /new
  GET  /new                                 → launch form (pick spec or upload)
  POST /runs                                → validate + spawn pipeline thread
  GET  /runs/{run_id}                       → run detail with workflow diagram
  GET  /runs/{run_id}/artifact?path=<rel>   → serve one artifact file
  GET  /runs/{run_id}/prompts?stage=<name>  → prompts.jsonl entries for a stage
  POST /runs/{run_id}/approve/{checkpoint}  → record approve/reject decision

No auth (prototype only — documented limitation).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pipeline.audit import AuditStore, RunRecord
from pipeline.config import REPO_ROOT, Settings
from pipeline.intake import SpecValidationError

from . import runner

BASE_DIR = Path(__file__).resolve().parent
SPECS_DIR = REPO_ROOT / "specs"
UPLOADS_DIR = SPECS_DIR / "uploads"
SPEC_EXTENSIONS = {".yaml", ".yml", ".md", ".json"}

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


def _list_specs() -> list[str]:
    """Specs found on disk, relative to repo root, sorted."""
    if not SPECS_DIR.exists():
        return []
    found: list[str] = []
    for p in SPECS_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in SPEC_EXTENSIONS:
            found.append(str(p.relative_to(REPO_ROOT)))
    return sorted(found)


@app.get("/", response_class=HTMLResponse)
def runs_list(request: Request) -> HTMLResponse:
    store = _store()
    runs = store.list_runs(limit=50)
    return templates.TemplateResponse(request, "runs_list.html", {"runs": runs})


@app.get("/new", response_class=HTMLResponse)
def new_run_form(request: Request) -> HTMLResponse:
    settings = Settings.load()
    return templates.TemplateResponse(
        request,
        "new_run.html",
        {
            "specs": _list_specs(),
            "settings": {
                "llm_provider": settings.llm_provider,
                "plan_model": settings.plan_model,
                "codegen_model": settings.codegen_model,
                "testgen_model": settings.testgen_model,
                "prompt_version": settings.prompt_version,
            },
            "error": None,
            "form": {"spec_path": "", "approver": "dashboard@local"},
        },
    )


@app.post("/runs")
async def launch_run(
    request: Request,
    spec_path: str = Form(""),
    approver: str = Form("dashboard@local"),
    spec_file: UploadFile | None = File(None),
) -> Any:
    """Launch a new pipeline run. Uploaded file wins over the dropdown choice."""

    def _form_error(message: str) -> HTMLResponse:
        settings = Settings.load()
        return templates.TemplateResponse(
            request,
            "new_run.html",
            {
                "specs": _list_specs(),
                "settings": {
                    "llm_provider": settings.llm_provider,
                    "plan_model": settings.plan_model,
                    "codegen_model": settings.codegen_model,
                    "testgen_model": settings.testgen_model,
                    "prompt_version": settings.prompt_version,
                },
                "error": message,
                "form": {"spec_path": spec_path, "approver": approver},
            },
            status_code=400,
        )

    chosen_path: Path | None = None

    if spec_file is not None and spec_file.filename:
        suffix = Path(spec_file.filename).suffix.lower()
        if suffix not in SPEC_EXTENSIONS:
            return _form_error(
                f"Unsupported file type {suffix!r}. Allowed: {sorted(SPEC_EXTENSIONS)}"
            )
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_name = Path(spec_file.filename).name
        dest = UPLOADS_DIR / f"{stamp}_{safe_name}"
        dest.write_bytes(await spec_file.read())
        chosen_path = dest
    elif spec_path:
        candidate = (REPO_ROOT / spec_path).resolve()
        if not str(candidate).startswith(str(SPECS_DIR.resolve())):
            return _form_error("spec_path must live under specs/")
        if not candidate.exists() or not candidate.is_file():
            return _form_error(f"spec not found: {spec_path}")
        chosen_path = candidate
    else:
        return _form_error("Pick a spec from the dropdown or upload a file.")

    try:
        run_id = runner.launch_run(spec_path=chosen_path, approver=approver)
    except SpecValidationError as e:
        return _form_error(f"Spec validation failed: {e}")
    except Exception as e:
        return _form_error(f"Failed to launch run: {e}")

    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


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

    plan: dict[str, Any] | None = None
    plan_file = artifacts_dir / "plan.json"
    if plan_file.exists():
        with contextlib.suppress(json.JSONDecodeError):
            plan = json.loads(plan_file.read_text(encoding="utf-8"))

    ac_coverage: dict[str, list[str]] | None = None
    ac_file = artifacts_dir / "ac_coverage.json"
    if ac_file.exists():
        with contextlib.suppress(json.JSONDecodeError):
            ac_coverage = json.loads(ac_file.read_text(encoding="utf-8"))

    codegen_summary = _summary_from_json(artifacts_dir / "codegen_output.json")
    testgen_summary = _summary_from_json(artifacts_dir / "testgen_output.json")

    pending_checkpoint, just_approved_checkpoint = _approval_state(run, approvals)

    # Build a status map for the 8-step workflow diagram.
    workflow = _workflow_status(stages, approvals, gates, run["status"])

    is_live = run["status"] in {"pending", "running", "awaiting_approval"}

    # If the run failed at gates, surface that to explain the missing approve#2.
    gates_failed_summary = _gates_failed_summary(run["status"], gates)

    signature = _structural_signature(
        run, stages, gates, approvals, artifacts,
        pending_checkpoint, just_approved_checkpoint, gates_failed_summary,
    )

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
            "plan": plan,
            "ac_coverage": ac_coverage,
            "codegen_summary": codegen_summary,
            "testgen_summary": testgen_summary,
            "pending_checkpoint": pending_checkpoint,
            "just_approved_checkpoint": just_approved_checkpoint,
            "workflow": workflow,
            "is_live": is_live,
            "gates_failed_summary": gates_failed_summary,
            "structural_signature": signature,
        },
    )


def _approval_state(
    run: dict[str, Any], approvals: list[dict]
) -> tuple[str | None, str | None]:
    """Return ``(pending_checkpoint, just_approved_checkpoint)``.

    The orchestrator writes ``current_stage = "approval:<cp>"`` at
    [workflow.py:53](pipeline/approval/workflow.py#L53) while parked at a
    checkpoint. We treat that as the source of truth:

      - ``pending_checkpoint``: the run is parked at that checkpoint and the
        user has not yet posted an approval row for it.
      - ``just_approved_checkpoint``: the user has posted an approval, but the
        pipeline thread hasn't polled audit.db yet to advance ``current_stage``
        (a ~2s race window in [workflow.py:79-90](pipeline/approval/workflow.py#L79-L90)).
        We show a "waiting for pipeline to resume" indicator instead of
        misclassifying the run as awaiting the NEXT checkpoint.
    """
    if run["status"] != "awaiting_approval":
        return None, None
    cs = run.get("current_stage") or ""
    if not cs.startswith("approval:"):
        return None, None
    cp = cs.split(":", 1)[1]
    if cp not in {"plan", "finalize"}:
        return None, None
    decided = {a["checkpoint"] for a in approvals}
    if cp in decided:
        return None, cp
    return cp, None


def _gates_failed_summary(
    run_status: str, gates: list[dict]
) -> list[str] | None:
    if run_status != "failed" or not gates:
        return None
    failed = [g["gate"] for g in gates if g["status"] == "failed"]
    return failed or None


def _structural_signature(
    run: dict[str, Any],
    stages: list[dict],
    gates: list[dict],
    approvals: list[dict],
    artifacts: list[str],
    pending_checkpoint: str | None,
    just_approved_checkpoint: str | None,
    gates_failed_summary: list[str] | None,
) -> str:
    """Stable hash of fields that, on change, demand a soft page reload.

    Pill colors and metric numbers patch in place; new rows / new checkpoints
    do not. When this signature changes between polls, the JS triggers a
    `location.reload()` — but only when the user isn't interacting.
    """
    parts = [
        run["status"],
        run.get("current_stage") or "",
        str(len(stages)),
        str(len(gates)),
        str(len(approvals)),
        str(len(artifacts)),
        pending_checkpoint or "",
        just_approved_checkpoint or "",
        ",".join(gates_failed_summary or []),
    ]
    return "|".join(parts)


def _summary_from_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(path.read_text(encoding="utf-8"))
    return None


WORKFLOW_STEPS = [
    ("intake", "Intake", "stage"),
    ("plan", "Plan", "stage"),
    ("approve_plan", "Approve #1", "approval:plan"),
    ("codegen", "Codegen", "stage"),
    ("testgen", "Testgen", "stage"),
    ("gates", "Gates", "stage"),
    ("approve_finalize", "Approve #2", "approval:finalize"),
    ("finalize", "Finalize", "stage"),
]


def _workflow_status(
    stages: list[dict],
    approvals: list[dict],
    gates: list[dict],
    run_status: str,
) -> list[dict[str, Any]]:
    by_stage = {s["stage"]: s for s in stages}
    approval_by_cp = {a["checkpoint"]: a for a in approvals}
    decided_cps = set(approval_by_cp)
    any_gate_failed = any(g["status"] == "failed" for g in gates)
    # The first checkpoint without an approval record is the one we're parked at.
    current_pending_cp = next(
        (cp for cp in ("plan", "finalize") if cp not in decided_cps), None
    )

    out: list[dict[str, Any]] = []
    for key, label, kind in WORKFLOW_STEPS:
        if kind == "stage":
            row = by_stage.get(key)
            if row:
                status = row["status"]
                # The gates stage row says "succeeded" if the runner finished,
                # even when individual gates failed. Surface the real outcome.
                if key == "gates" and status == "succeeded" and any_gate_failed:
                    status = "failed"
            elif key == "intake":
                # Intake runs before the audit row exists, so it never gets a
                # stages entry. If we have any run record at all, intake passed.
                status = "succeeded"
            elif key == "finalize" and run_status == "succeeded":
                # Same shape for finalize — orchestrator writes the run row
                # but no stages entry for finalize.
                status = "succeeded"
            else:
                status = "pending"
        else:
            cp = kind.split(":", 1)[1]
            ap = approval_by_cp.get(cp)
            if ap:
                status = ap["decision"]  # approved | rejected
            elif run_status == "awaiting_approval" and cp == current_pending_cp:
                status = "awaiting_approval"
            else:
                status = "pending"
        out.append({"key": key, "label": label, "status": status})
    return out


@app.get("/runs/{run_id}/state.json")
def run_state(run_id: str) -> JSONResponse:
    """Compact snapshot of one run — consumed by the live poller in run_detail.html.

    Built from the same helpers as ``run_detail`` so a poll result is always
    consistent with what the server-side template would render right now.
    """
    store = _store()
    run = _run_or_404(store, run_id)
    stages = store.stages_for_run(run_id)
    gates = store.gates_for_run(run_id)
    approvals = store.approvals_for_run(run_id)
    metrics = store.metrics_for_run(run_id) or {}

    artifacts_dir = Path(run["artifacts_dir"])
    artifact_count = 0
    if artifacts_dir.exists():
        artifact_count = sum(1 for p in artifacts_dir.rglob("*") if p.is_file())

    pending_checkpoint, just_approved_checkpoint = _approval_state(run, approvals)
    workflow = _workflow_status(stages, approvals, gates, run["status"])
    gates_failed_summary = _gates_failed_summary(run["status"], gates)
    is_live = run["status"] in {"pending", "running", "awaiting_approval"}
    signature = _structural_signature(
        run, stages, gates,
        approvals,
        [""] * artifact_count,  # only the count matters for the signature
        pending_checkpoint, just_approved_checkpoint, gates_failed_summary,
    )

    return JSONResponse({
        "status": run["status"],
        "current_stage": run.get("current_stage"),
        "workflow": workflow,
        "stages": [
            {"stage": s["stage"], "status": s["status"],
             "duration_ms": s.get("duration_ms"), "error": s.get("error")}
            for s in stages
        ],
        "gates": [
            {"gate": g["gate"], "status": g["status"],
             "duration_ms": g.get("duration_ms"),
             "summary": g.get("summary"),
             "artifact_path": g.get("artifact_path")}
            for g in gates
        ],
        "approvals": [
            {"checkpoint": a["checkpoint"], "decision": a["decision"],
             "approver": a["approver"], "comment": a.get("comment"),
             "created_at": a.get("created_at")}
            for a in approvals
        ],
        "metrics": {
            "total_tokens": metrics.get("total_tokens") or 0,
            "total_duration_ms": metrics.get("total_duration_ms") or 0,
            "ac_total": metrics.get("ac_total"),
            "ac_covered": metrics.get("ac_covered"),
            "gates_passed": metrics.get("gates_passed"),
            "gates_total": metrics.get("gates_total"),
        },
        "pending_checkpoint": pending_checkpoint,
        "just_approved_checkpoint": just_approved_checkpoint,
        "gates_failed_summary": gates_failed_summary,
        "is_live": is_live,
        "artifact_count": artifact_count,
        "structural_signature": signature,
    })


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
    content = target.read_text(encoding="utf-8")
    if target.suffix == ".json":
        with contextlib.suppress(json.JSONDecodeError):
            content = json.dumps(json.loads(content), indent=2)
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


@app.get("/runs/{run_id}/prompts")
def run_prompts(run_id: str, stage: str) -> JSONResponse:
    """Return the prompts.jsonl entries for a single stage."""
    store = _store()
    run = _run_or_404(store, run_id)
    jsonl = Path(run["artifacts_dir"]) / "prompts.jsonl"
    if not jsonl.exists():
        return JSONResponse({"stage": stage, "entries": []})
    entries: list[dict[str, Any]] = []
    with jsonl.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("stage") == stage:
                entries.append(rec)
    return JSONResponse({"stage": stage, "entries": entries})


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
