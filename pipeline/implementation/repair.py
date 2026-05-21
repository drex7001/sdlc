"""Repair stage: when gates fail, drive a tool-using agent to fix the diff.

Reuses :class:`pipeline.implementation.agent_tools.ToolLoop` with the
``REPAIR_TOOLS`` set (adds ``read_gate_log`` so the agent can inspect the
failing gate's captured output). The loop's ``allowed_paths`` is still the
plan's ``impacted_files`` — repair cannot escape the sandbox to fix things.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..audit import AuditStore, RunRecord
from ..gates import GateReport
from ..intake import FeatureSpec
from ..llm import LLMClient, load_prompt
from ..planning import Plan
from ..planning.planner import _list_tree
from .agent_tools import REPAIR_TOOLS, AgentLoopError, ToolLoop, WriteValidationResult
from .codegen_schema import GeneratedChanges
from .normalization import changed_target_workspace, normalize_python_changes_in_workspace


def run_repair_agent(
    *,
    spec: FeatureSpec,
    plan: Plan,
    impl_summary: str,
    test_summary: str,
    gate_report: GateReport,
    target_dir: Path,
    llm: LLMClient,
    model: str,
    prompts_dir: Path,
    prompt_version: str,
    run: RunRecord,
    audit: AuditStore,
    attempt: int,
    max_tokens: int = 8192,
) -> GeneratedChanges:
    """Single repair attempt. Caller controls the outer retry budget."""
    system = load_prompt(prompts_dir, prompt_version, "repair")
    failing = [o for o in gate_report.outcomes if not o.passed]
    user_prompt = json.dumps(
        {
            "spec": spec.model_dump(),
            "plan": plan.model_dump(),
            "codegen_summary": impl_summary,
            "testgen_summary": test_summary,
            "failing_gates": [
                {"gate": o.name, "summary": o.summary, "log": o.output}
                for o in failing
            ],
            "target_tree": _list_tree(target_dir),
            "attempt": attempt,
        },
        indent=2,
    )

    gate_logs = {o.name: o.output for o in gate_report.outcomes}

    loop = ToolLoop(
        llm=llm,
        model=model,
        system_prompt=system,
        tools=REPAIR_TOOLS,
        target_dir=target_dir,
        allowed_paths=plan.impacted_set(),
        audit=audit,
        run=run,
        stage_label=f"repair_{attempt}",
        max_tokens=max_tokens,
        max_turns=_max_turns(),
        gate_logs=gate_logs,
        validate_write=_repair_write_validator(target_dir),
    )

    try:
        result = loop.run_loop(initial_user_message=user_prompt)
    except AgentLoopError as e:
        raise ValueError(f"repair agent failed: {e}") from e

    audit.write_json_artifact(
        run,
        f"repair_{attempt}_output.json",
        {
            "summary": result.changes.summary,
            "turns": result.turns,
            "tool_calls": result.tool_calls,
            "files": [{"path": f.path, "action": f.action} for f in result.changes.files],
            "failing_gates": [o.name for o in failing],
        },
    )
    return result.changes


def _max_turns() -> int:
    try:
        return max(1, int(os.environ.get("PIPELINE_MAX_AGENT_TURNS", "12")))
    except ValueError:
        return 12


@dataclass
class _CommandResult:
    name: str
    args: list[str]
    returncode: int
    output: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def _repair_write_validator(target_dir: Path):
    def validate(changes: GeneratedChanges) -> WriteValidationResult:
        try:
            with changed_target_workspace(
                target_dir=target_dir,
                changes=changes,
                strict=True,
            ) as workspace:
                normalized = normalize_python_changes_in_workspace(
                    changes=changes,
                    workspace=workspace,
                )
                results = _run_fast_validation(workspace=workspace, changes=normalized)
        except (FileExistsError, FileNotFoundError) as e:
            return WriteValidationResult.rejected(str(e))

        failed = [result for result in results if not result.passed]
        if failed:
            return WriteValidationResult.rejected(_format_validation_failure(failed))
        return WriteValidationResult.accepted(normalized)

    return validate


def _run_fast_validation(*, workspace: Path, changes: GeneratedChanges) -> list[_CommandResult]:
    results: list[_CommandResult] = []
    py_paths = [
        change.path
        for change in changes.files
        if change.action != "delete" and PurePosixPath(change.path).suffix == ".py"
    ]
    if py_paths:
        results.append(_run_python_module("ruff", ["check", *py_paths], cwd=workspace))
    if (workspace / "src").is_dir():
        results.append(_run_python_module("mypy", ["src"], cwd=workspace))
    return results


def _run_python_module(module: str, args: list[str], *, cwd: Path) -> _CommandResult:
    command = [sys.executable, "-m", module, *args]
    proc = subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return _CommandResult(
        name=module,
        args=command,
        returncode=proc.returncode,
        output=_truncate((proc.stdout or "") + (proc.stderr or "")),
    )


def _format_validation_failure(failed: list[_CommandResult]) -> str:
    lines = [
        "candidate failed fast validation after ruff normalization; "
        "fix the reported issues and call write_files again.",
    ]
    for result in failed:
        lines.append(f"\n{result.name} exit={result.returncode}:")
        lines.append(result.output or "(no output)")
    return "\n".join(lines)


def _truncate(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
