"""Typer-based CLI entrypoint."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .approval import ApprovalMode
from .audit import AuditStore
from .config import Settings
from .intake import SpecValidationError, load_and_validate
from .orchestrator import PipelineError, run_pipeline

app = typer.Typer(
    help="AI-native, spec-driven development pipeline.",
    no_args_is_help=True,
)
console = Console()


def _settings() -> Settings:
    return Settings.load()


def _store(settings: Settings) -> AuditStore:
    return AuditStore(db_path=settings.audit_db, runs_dir=settings.runs_dir)


@app.command("run")
def run_cmd(
    spec: Path = typer.Argument(..., exists=True, readable=True, help="Path to spec file."),
    approval: ApprovalMode = typer.Option(
        ApprovalMode.CLI,
        "--approval-mode",
        help="cli (prompt), dashboard (poll for approval), or auto (no human).",
    ),
    approver: str | None = typer.Option(None, help="Reviewer identity. Overrides $APPROVER."),
) -> None:
    """Run the full pipeline against a spec."""
    settings = _settings()
    if approver:
        import os
        os.environ["APPROVER"] = approver
        settings = Settings.load()

    console.print(f"[bold]Running pipeline[/bold] on {spec}")
    console.print(f"  provider={settings.llm_provider}")
    console.print(
        f"  models: plan={settings.plan_model} "
        f"codegen={settings.codegen_model} testgen={settings.testgen_model}"
    )
    console.print(f"  approval_mode={approval.value} approver={settings.approver}")

    try:
        result = run_pipeline(spec_path=spec, settings=settings, approval_mode=approval)
    except PipelineError as e:
        console.print(f"[red]Pipeline failed:[/red] {e}")
        raise typer.Exit(code=1) from e

    color = {"succeeded": "green", "rejected": "yellow", "failed": "red"}.get(result.status, "white")
    console.print(f"\n[bold {color}]Run {result.status}[/bold {color}]: {result.run_id}")
    console.print(f"Artifacts: {result.artifacts_dir}")
    if result.metrics:
        m = result.metrics
        console.print(
            f"Tokens: {m['total_tokens']}  "
            f"Duration: {m['total_duration_ms']} ms  "
            f"AC coverage: {m['ac_covered']}/{m['ac_total']} ({m['ac_coverage_pct']}%)  "
            f"Gates: {m['gates_passed']}/{m['gates_total']}"
        )
    if result.status != "succeeded":
        raise typer.Exit(code=1)


@app.command("validate")
def validate_cmd(
    spec: Path = typer.Argument(..., exists=True, readable=True),
) -> None:
    """Validate a spec file without running the pipeline."""
    try:
        validated = load_and_validate(spec)
    except SpecValidationError as e:
        console.print(f"[red]Invalid:[/red] {e}")
        raise typer.Exit(code=1) from e
    console.print(f"[green]OK[/green]: {validated.name} (hash={validated.content_hash()[:12]})")
    console.print(f"  {len(validated.acceptance_criteria)} acceptance criteria")
    console.print(f"  {len(validated.business_rules)} business rules")


@app.command("status")
def status_cmd(
    run_id: str | None = typer.Argument(None, help="Run ID. Omit to list recent runs."),
) -> None:
    """Show run status. With no argument, lists recent runs."""
    settings = _settings()
    store = _store(settings)
    if run_id is None:
        runs = store.list_runs(limit=20)
        table = Table(title="Recent runs")
        table.add_column("run_id")
        table.add_column("spec")
        table.add_column("status")
        table.add_column("stage")
        table.add_column("started")
        for r in runs:
            table.add_row(
                r["run_id"], r["spec_name"], r["status"],
                r.get("current_stage") or "-", r["started_at"],
            )
        console.print(table)
        return

    run = store.get_run(run_id)
    if not run:
        console.print(f"[red]unknown run:[/red] {run_id}")
        raise typer.Exit(code=1)
    console.print_json(json.dumps(run))
    console.print("\n[bold]Stages[/bold]")
    for s in store.stages_for_run(run_id):
        console.print(f"  {s['stage']}: {s['status']} ({s['duration_ms']}ms)")
    console.print("\n[bold]Gates[/bold]")
    for g in store.gates_for_run(run_id):
        console.print(f"  {g['gate']}: {g['status']} - {g['summary']}")
    console.print("\n[bold]Approvals[/bold]")
    for ap in store.approvals_for_run(run_id):
        console.print(f"  {ap['checkpoint']}: {ap['decision']} by {ap['approver']}")


@app.command("approve")
def approve_cmd(
    run_id: str = typer.Argument(...),
    checkpoint: str = typer.Argument(..., help="plan | finalize"),
    decision: str = typer.Argument(..., help="approved | rejected"),
    approver: str | None = typer.Option(None),
    comment: str | None = typer.Option(None),
) -> None:
    """Record an approval decision out-of-band (dashboard equivalent)."""
    if decision not in {"approved", "rejected"}:
        console.print("[red]decision must be 'approved' or 'rejected'[/red]")
        raise typer.Exit(code=1)
    settings = _settings()
    store = _store(settings)
    run = store.get_run(run_id)
    if not run:
        console.print(f"[red]unknown run:[/red] {run_id}")
        raise typer.Exit(code=1)
    from .audit import RunRecord
    record = RunRecord(
        run_id=run_id, artifacts_dir=Path(run["artifacts_dir"]),
        spec_name=run["spec_name"], spec_hash=run["spec_hash"],
    )
    store.record_approval(
        record, checkpoint=checkpoint, decision=decision,
        approver=approver or settings.approver, comment=comment,
    )
    console.print(f"[green]recorded[/green]: {checkpoint} -> {decision}")


if __name__ == "__main__":
    app()
