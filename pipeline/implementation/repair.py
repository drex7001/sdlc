"""Repair stage: when gates fail, drive a tool-using agent to fix the diff.

Reuses :class:`pipeline.implementation.agent_tools.ToolLoop` with the
``REPAIR_TOOLS`` set (adds ``read_gate_log`` so the agent can inspect the
failing gate's captured output). The loop's ``allowed_paths`` is still the
plan's ``impacted_files`` — repair cannot escape the sandbox to fix things.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..audit import AuditStore, RunRecord
from ..gates import GateReport
from ..intake import FeatureSpec
from ..llm import LLMClient, load_prompt
from ..planning import Plan
from ..planning.planner import _list_tree
from .agent_tools import REPAIR_TOOLS, AgentLoopError, ToolLoop
from .codegen_schema import GeneratedChanges


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
                {"gate": o.name, "summary": o.summary}
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
