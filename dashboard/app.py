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
TARGETS_DIR = REPO_ROOT / "targets"
CUSTOM_PROJECT_KEY = "__custom__"

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


def _list_projects() -> list[dict[str, Any]]:
    """Bundled targets under ./targets/* the pipeline can operate on.

    Each entry has: ``key`` (basename, used as form value), ``label`` (display
    name), ``path`` (absolute), ``valid`` (has a pyproject.toml so the gates
    can run against it).
    """
    if not TARGETS_DIR.exists():
        return []
    projects: list[dict[str, Any]] = []
    for p in sorted(TARGETS_DIR.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        projects.append({
            "key": p.name,
            "label": p.name,
            "path": str(p.resolve()),
            "valid": (p / "pyproject.toml").exists(),
        })
    return projects


def _resolve_project(project_key: str, project_path: str) -> tuple[Path | None, str | None]:
    """Pick a target_dir from form input. Returns ``(target_dir, error)``."""
    if project_key == CUSTOM_PROJECT_KEY:
        if not project_path.strip():
            return None, "Custom project: please type an absolute path."
        candidate = Path(project_path).expanduser()
        if not candidate.is_absolute():
            return None, f"Custom project path must be absolute: {project_path!r}"
        candidate = candidate.resolve()
        if not candidate.exists() or not candidate.is_dir():
            return None, f"Custom project directory does not exist: {candidate}"
        if not (candidate / "pyproject.toml").exists():
            return None, (
                f"Custom project at {candidate} has no pyproject.toml — "
                "the gates (ruff / mypy / pytest) need it to run."
            )
        return candidate, None
    if not project_key:
        return None, "Pick a project from the dropdown."
    for proj in _list_projects():
        if proj["key"] == project_key:
            if not proj["valid"]:
                return None, f"Project {project_key!r} is missing a pyproject.toml."
            return Path(proj["path"]), None
    return None, f"Unknown project: {project_key!r}"


@app.get("/", response_class=HTMLResponse)
def runs_list(request: Request) -> HTMLResponse:
    store = _store()
    runs = store.list_runs(limit=50)
    return templates.TemplateResponse(request, "runs_list.html", {"runs": runs})


@app.get("/new", response_class=HTMLResponse)
def new_run_form(request: Request) -> HTMLResponse:
    settings = Settings.load()
    projects = _list_projects()
    default_key = projects[0]["key"] if projects else CUSTOM_PROJECT_KEY
    return templates.TemplateResponse(
        request,
        "new_run.html",
        {
            "specs": _list_specs(),
            "projects": projects,
            "custom_key": CUSTOM_PROJECT_KEY,
            "settings": {
                "llm_provider": settings.llm_provider,
                "plan_model": settings.plan_model,
                "codegen_model": settings.codegen_model,
                "testgen_model": settings.testgen_model,
                "prompt_version": settings.prompt_version,
            },
            "error": None,
            "form": {
                "spec_path": "",
                "approver": "dashboard@local",
                "project_key": default_key,
                "project_path": "",
            },
        },
    )


@app.post("/runs")
async def launch_run(
    request: Request,
    spec_path: str = Form(""),
    approver: str = Form("dashboard@local"),
    project_key: str = Form(""),
    project_path: str = Form(""),
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
                "projects": _list_projects(),
                "custom_key": CUSTOM_PROJECT_KEY,
                "settings": {
                    "llm_provider": settings.llm_provider,
                    "plan_model": settings.plan_model,
                    "codegen_model": settings.codegen_model,
                    "testgen_model": settings.testgen_model,
                    "prompt_version": settings.prompt_version,
                },
                "error": message,
                "form": {
                    "spec_path": spec_path,
                    "approver": approver,
                    "project_key": project_key,
                    "project_path": project_path,
                },
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

    target_dir, project_error = _resolve_project(project_key, project_path)
    if project_error or target_dir is None:
        return _form_error(project_error or "Pick a project.")

    try:
        run_id = runner.launch_run(
            spec_path=chosen_path, approver=approver, target_dir=target_dir,
        )
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
    artifacts_by_group: list[dict[str, Any]] = []
    if artifacts_dir.exists():
        for p in sorted(artifacts_dir.rglob("*")):
            if p.is_file():
                artifacts.append(str(p.relative_to(artifacts_dir)))
        artifacts_by_group = _group_artifacts(artifacts_dir)

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
    workflow = _workflow_status(stages, approvals, gates, run["status"], run.get("current_stage"))

    is_live = run["status"] in {"pending", "running", "awaiting_approval"}

    # If the run failed at gates, surface that to explain the missing approve#2.
    gates_failed_summary = _gates_failed_summary(run["status"], gates)
    ac_failed_summary = _ac_failed_summary(run["status"], ac_coverage)

    signature = _structural_signature(
        run, stages, gates, approvals, artifacts,
        pending_checkpoint, just_approved_checkpoint,
        gates_failed_summary, ac_failed_summary,
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
            "artifacts_by_group": artifacts_by_group,
            "plan": plan,
            "ac_coverage": ac_coverage,
            "codegen_summary": codegen_summary,
            "testgen_summary": testgen_summary,
            "pending_checkpoint": pending_checkpoint,
            "just_approved_checkpoint": just_approved_checkpoint,
            "workflow": workflow,
            "is_live": is_live,
            "gates_failed_summary": gates_failed_summary,
            "ac_failed_summary": ac_failed_summary,
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
    final_round = _final_gate_round(gates)
    failed = [g["gate"] for g in final_round if g["status"] == "failed"]
    return failed or None


def _final_gate_round(gates: list[dict]) -> list[dict]:
    if not gates:
        return []
    names = []
    for gate in reversed(gates):
        name = gate["gate"]
        if name in names:
            break
        names.append(name)
    return list(reversed(gates[-len(names):]))


def _ac_failed_summary(
    run_status: str,
    ac_coverage: dict[str, list[str]] | None,
) -> list[str] | None:
    if run_status != "failed" or not ac_coverage:
        return None
    missing = sorted(ac_id for ac_id, tests in ac_coverage.items() if not tests)
    return missing or None


def _structural_signature(
    run: dict[str, Any],
    stages: list[dict],
    gates: list[dict],
    approvals: list[dict],
    artifacts: list[str],
    pending_checkpoint: str | None,
    just_approved_checkpoint: str | None,
    gates_failed_summary: list[str] | None,
    ac_failed_summary: list[str] | None,
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
        ",".join(ac_failed_summary or []),
    ]
    return "|".join(parts)


def _summary_from_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(path.read_text(encoding="utf-8"))
    return None


# Ordered list of (group_key, display_label) for the artifact section.
# Repair groups are spliced in dynamically below.
_BASE_ARTIFACT_GROUPS = [
    ("spec",     "Spec"),
    ("plan",     "Plan"),
    ("codegen",  "Codegen"),
    ("testgen",  "Testgen"),
    ("gates",    "Gates"),
    ("finalize", "Finalize"),
    ("metrics",  "Metrics"),
    ("logs",     "Logs"),
    ("approvals", "Approvals"),
    ("other",    "Other"),
]


def _classify_artifact(rel: str) -> str:
    """Bucket a relative artifact path into one of the workflow groups."""
    import re

    # Repair attempts get their own bucket per attempt.
    m = re.match(r"^(?:patches/)?repair_(\d+)", rel)
    if m:
        return f"repair_{m.group(1)}"
    if rel.startswith("spec"):
        return "spec"
    if rel == "plan.json":
        return "plan"
    if rel.startswith("codegen") or rel == "patches/codegen.patch" or rel == "change_summary.md":
        return "codegen"
    if rel.startswith("testgen") or rel == "patches/testgen.patch":
        return "testgen"
    if rel.startswith("gate_") or rel == "gates.json" or rel == "ac_coverage.json":
        return "gates"
    if rel == "deployment_evidence.json":
        return "finalize"
    if rel == "metrics.json":
        return "metrics"
    if rel == "prompts.jsonl":
        return "logs"
    if rel == "approvals.json":
        return "approvals"
    return "other"


def _group_artifacts(artifacts_dir: Path) -> list[dict[str, Any]]:
    """Group every artifact file into an ordered list of sections.

    Each section is `{key, label, items: [{path, mtime}]}`. Items inside a
    section are ordered by mtime so the natural reading order matches what
    happened during the run.
    """
    buckets: dict[str, list[tuple[str, float]]] = {}
    for p in artifacts_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(artifacts_dir))
        key = _classify_artifact(rel)
        buckets.setdefault(key, []).append((rel, p.stat().st_mtime))

    # Build ordered output. Splice repair_N groups in after "gates" in order.
    repair_keys = sorted(
        [k for k in buckets if k.startswith("repair_")],
        key=lambda k: int(k.split("_", 1)[1]),
    )
    ordered_groups: list[tuple[str, str]] = []
    for key, label in _BASE_ARTIFACT_GROUPS:
        ordered_groups.append((key, label))
        if key == "gates":
            for rk in repair_keys:
                n = rk.split("_", 1)[1]
                ordered_groups.append((rk, f"Repair #{n}"))

    out: list[dict[str, Any]] = []
    for key, label in ordered_groups:
        items = buckets.get(key) or []
        if not items:
            continue
        items.sort(key=lambda t: t[1])
        # NB: key is named "files" rather than "items" because Jinja's
        # `group.items` would resolve to `dict.items` (the method), not the
        # value at the "items" key.
        out.append({
            "key": key,
            "label": label,
            "files": [{"path": rel, "mtime": mt} for rel, mt in items],
        })
    return out


BASE_WORKFLOW_STEPS = [
    ("intake", "Intake", "stage"),
    ("plan", "Plan", "stage"),
    ("approve_plan", "Approve #1", "approval:plan"),
    ("codegen", "Codegen", "stage"),
    ("testgen", "Testgen", "stage"),
    ("gates", "Gates", "stage"),
    ("approve_finalize", "Approve #2", "approval:finalize"),
    ("finalize", "Finalize", "stage"),
]


def _repair_steps_for(stages: list[dict]) -> list[tuple[str, str, str]]:
    """Build the extra `repair_N` + `gates_after_repair_N` pills for this run.

    Driven by what actually appears in the stages table, so future attempts
    show up without code changes.
    """
    extras: list[tuple[str, str, str]] = []
    repair_keys = sorted(
        {s["stage"] for s in stages if s["stage"].startswith("repair_")},
        key=lambda k: int(k.split("_", 1)[1]) if k.split("_", 1)[1].isdigit() else 0,
    )
    for rkey in repair_keys:
        n = rkey.split("_", 1)[1]
        extras.append((rkey, f"Repair #{n}", "stage"))
        gates_key = f"gates_after_{rkey}"
        if any(s["stage"] == gates_key for s in stages):
            extras.append((gates_key, f"Gates #{int(n) + 1}", "stage"))
    return extras


def _workflow_steps_for(stages: list[dict]) -> list[tuple[str, str, str]]:
    """Splice repair / re-gate pills into the base 8-step workflow.

    They appear immediately after the original `gates` pill so the visual
    grouping is "gates → repair → gates → repair → gates → approve#2".
    """
    extras = _repair_steps_for(stages)
    if not extras:
        return list(BASE_WORKFLOW_STEPS)
    out: list[tuple[str, str, str]] = []
    for step in BASE_WORKFLOW_STEPS:
        out.append(step)
        if step[0] == "gates":
            out.extend(extras)
    return out


def _workflow_status(
    stages: list[dict],
    approvals: list[dict],
    gates: list[dict],
    run_status: str,
    current_stage: str | None = None,
) -> list[dict[str, Any]]:
    by_stage = {s["stage"]: s for s in stages}
    approval_by_cp = {a["checkpoint"]: a for a in approvals}
    # gates_after_repair_N is the *latest* truth about gate state, so only
    # treat the FINAL gates row as failed-with-running-gates rather than every
    # historic gate row.
    repair_gate_stages = sorted(
        (s for s in stages if s["stage"].startswith("gates_after_repair_")),
        key=lambda s: s["stage"],
    )
    final_gate_stage = repair_gate_stages[-1]["stage"] if repair_gate_stages else "gates"
    any_gate_failed_in_final = any(
        g["status"] == "failed" for g in gates[-5:]  # last full round of gates
    )
    current_pending_cp: str | None = None
    if run_status == "awaiting_approval" and current_stage:
        prefix = "approval:"
        if current_stage.startswith(prefix):
            cp = current_stage[len(prefix):]
            if cp in {"plan", "finalize"} and cp not in approval_by_cp:
                current_pending_cp = cp

    out: list[dict[str, Any]] = []
    for key, label, kind in _workflow_steps_for(stages):
        if kind == "stage":
            row = by_stage.get(key)
            if row:
                status = row["status"]
                # A gates / gates_after_repair_N row says "succeeded" if the
                # runner finished. Surface the real outcome by checking the
                # individual gate_results for the FINAL gates round.
                if key == final_gate_stage and status == "succeeded" and any_gate_failed_in_final:
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
            elif cp == current_pending_cp:
                status = "awaiting_approval"
            else:
                status = "pending"
        out.append({"key": key, "label": label, "status": status})
    return out


# Keep the old name as an alias for any external importers.
WORKFLOW_STEPS = BASE_WORKFLOW_STEPS


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
    ac_coverage: dict[str, list[str]] | None = None
    ac_file = artifacts_dir / "ac_coverage.json"
    if ac_file.exists():
        with contextlib.suppress(json.JSONDecodeError):
            ac_coverage = json.loads(ac_file.read_text(encoding="utf-8"))

    pending_checkpoint, just_approved_checkpoint = _approval_state(run, approvals)
    workflow = _workflow_status(stages, approvals, gates, run["status"], run.get("current_stage"))
    gates_failed_summary = _gates_failed_summary(run["status"], gates)
    ac_failed_summary = _ac_failed_summary(run["status"], ac_coverage)
    is_live = run["status"] in {"pending", "running", "awaiting_approval"}
    signature = _structural_signature(
        run, stages, gates,
        approvals,
        [""] * artifact_count,  # only the count matters for the signature
        pending_checkpoint, just_approved_checkpoint,
        gates_failed_summary, ac_failed_summary,
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
            "total_input_tokens": metrics.get("total_input_tokens") or 0,
            "total_output_tokens": metrics.get("total_output_tokens") or 0,
            "total_duration_ms": metrics.get("total_duration_ms") or 0,
            "llm_calls": metrics.get("llm_calls") or 0,
            "llm_latency_ms": metrics.get("llm_latency_ms") or 0,
            "estimated_cost_usd": (
                metrics.get("estimated_cost_usd") if metrics.get("cost_configured") else None
            ),
            "cost_configured": metrics.get("cost_configured") or False,
            "stage_duration_ms": metrics.get("stage_duration_ms") or 0,
            "gate_duration_ms": metrics.get("gate_duration_ms") or 0,
            "ac_total": metrics.get("ac_total"),
            "ac_covered": metrics.get("ac_covered"),
            "ac_coverage_pct": metrics.get("ac_coverage_pct"),
            "gates_passed": metrics.get("gates_passed"),
            "gates_total": metrics.get("gates_total"),
            "gate_results_count": metrics.get("gate_results_count"),
        },
        "pending_checkpoint": pending_checkpoint,
        "just_approved_checkpoint": just_approved_checkpoint,
        "gates_failed_summary": gates_failed_summary,
        "ac_failed_summary": ac_failed_summary,
        "is_live": is_live,
        "artifact_count": artifact_count,
        "structural_signature": signature,
    })


@app.get("/runs/{run_id}/timeline", response_class=HTMLResponse)
def run_timeline_page(request: Request, run_id: str) -> HTMLResponse:
    store = _store()
    run = _run_or_404(store, run_id)
    events = _merged_timeline(store, run)
    return templates.TemplateResponse(
        request, "run_timeline.html",
        {"run": run, "events": events, "event_count": len(events)},
    )


@app.get("/runs/{run_id}/timeline.json")
def run_timeline_json(run_id: str) -> JSONResponse:
    store = _store()
    run = _run_or_404(store, run_id)
    return JSONResponse({"events": _merged_timeline(store, run)})


def _merged_timeline(store: AuditStore, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every audit row for ``run`` into a single chronologically-sorted
    list of events. Reuses the same data the per-section view consumes; no new
    persistence layer.
    """
    run_id = run["run_id"]
    stages = store.stages_for_run(run_id)
    gates = store.gates_for_run(run_id)
    approvals = store.approvals_for_run(run_id)
    prompts = store.prompt_calls_for_run(run_id)

    events: list[dict[str, Any]] = []

    # Stage start events. We synthesise a virtual "intake" event at the run's
    # started_at so the timeline doesn't have a mysterious gap before "plan".
    events.append({
        "ts": run["started_at"],
        "kind": "stage_start",
        "label": "intake",
        "status": "succeeded",
        "detail": f"spec={run['spec_name']} model={run['llm_model']}",
        "anchor": "stage-intake",
    })

    for s in stages:
        events.append({
            "ts": s["started_at"],
            "kind": "stage_start",
            "label": s["stage"],
            "status": "running",
            "detail": "",
            "anchor": f"stage-{s['stage']}",
        })
        if s.get("ended_at"):
            events.append({
                "ts": s["ended_at"],
                "kind": "stage_end",
                "label": s["stage"],
                "status": s["status"],
                "detail": (s.get("duration_ms") and f"{s['duration_ms']} ms") or "",
                "anchor": f"stage-{s['stage']}",
                "error": s.get("error"),
            })

    for g in gates:
        events.append({
            "ts": g["created_at"],
            "kind": "gate",
            "label": f"gate: {g['gate']}",
            "status": g["status"],
            "detail": g.get("summary") or "",
            "anchor": None,
            "artifact_path": g.get("artifact_path"),
        })

    for a in approvals:
        events.append({
            "ts": a["created_at"],
            "kind": "approval",
            "label": f"approve #{a['checkpoint']}",
            "status": a["decision"],
            "detail": f"by {a['approver']}" + (f" — {a['comment']}" if a.get("comment") else ""),
            "anchor": None,
        })

    for p in prompts:
        events.append({
            "ts": p["created_at"],
            "kind": "llm_call",
            "label": p["stage"],
            "status": "running",
            "detail": (
                f"{p['provider']}/{p['model']} · "
                f"{p['input_tokens'] or 0}→{p['output_tokens'] or 0} tok · "
                f"{p['latency_ms'] or 0} ms"
            ),
            "anchor": f"stage-{p['stage'].split('.', 1)[0]}",
        })

    # ISO-8601 strings sort lexicographically. Within a tied timestamp, push
    # stage_starts before everything else, stage_ends last; gives a tidier
    # visual when multiple events share a second-resolution timestamp.
    KIND_ORDER = {"stage_start": 0, "llm_call": 1, "gate": 2, "approval": 3, "stage_end": 4}
    events.sort(key=lambda e: (e["ts"], KIND_ORDER.get(e["kind"], 9)))

    # Final virtual event when the run is terminal.
    if run.get("ended_at") and run["status"] in {"succeeded", "failed", "rejected"}:
        events.append({
            "ts": run["ended_at"],
            "kind": "run_end",
            "label": f"run {run['status']}",
            "status": run["status"],
            "detail": "",
            "anchor": None,
        })

    return events


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
    expected_stage = f"approval:{checkpoint}"
    if run["status"] != "awaiting_approval" or run.get("current_stage") != expected_stage:
        raise HTTPException(
            status_code=409,
            detail=f"run is not awaiting {checkpoint} approval",
        )
    if any(a["checkpoint"] == checkpoint for a in store.approvals_for_run(run_id)):
        raise HTTPException(
            status_code=409,
            detail=f"{checkpoint} approval already recorded",
        )
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
