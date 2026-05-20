"""Codegen stage: plan + spec → file operations via a tool-using LLM agent.

The agent is given ``list_files``, ``read_file``, and ``write_files`` tools
(see :mod:`.agent_tools`). It runs in a multi-turn loop bounded by
``PIPELINE_MAX_AGENT_TURNS`` and terminates when it calls ``write_files``
with paths cleared by the sandbox.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..audit import AuditStore, RunRecord
from ..intake import FeatureSpec
from ..llm import LLMClient, load_prompt
from ..planning import Plan, gather_plan_context
from ..planning.planner import _list_tree
from .agent_tools import CODEGEN_TOOLS, AgentLoopError, ToolLoop
from .codegen_schema import FileChange, GeneratedChanges

__all__ = ["FileChange", "GeneratedChanges", "generate_code"]


def generate_code(
    *,
    spec: FeatureSpec,
    plan: Plan,
    target_dir: Path,
    llm: LLMClient,
    model: str,
    prompts_dir: Path,
    prompt_version: str,
    run: RunRecord,
    audit: AuditStore,
    max_tokens: int = 8192,
) -> GeneratedChanges:
    system = load_prompt(prompts_dir, prompt_version, "codegen_agent")
    user_prompt = json.dumps(
        {
            "spec": spec.model_dump(),
            "plan": plan.model_dump(),
            "existing_files": gather_plan_context(target_dir, plan),
            "target_tree": _list_tree(target_dir),
        },
        indent=2,
    )

    loop = ToolLoop(
        llm=llm,
        model=model,
        system_prompt=system,
        tools=CODEGEN_TOOLS,
        target_dir=target_dir,
        allowed_paths=plan.impacted_set(),
        audit=audit,
        run=run,
        stage_label="codegen",
        max_tokens=max_tokens,
        max_turns=_max_turns(),
    )

    try:
        result = loop.run_loop(initial_user_message=user_prompt)
    except AgentLoopError as e:
        raise ValueError(f"codegen agent failed: {e}") from e

    changes = result.changes
    audit.write_json_artifact(
        run,
        "codegen_output.json",
        {
            "summary": changes.summary,
            "turns": result.turns,
            "tool_calls": result.tool_calls,
            "files": [{"path": f.path, "action": f.action} for f in changes.files],
        },
    )
    return changes


def _max_turns() -> int:
    try:
        return max(1, int(os.environ.get("PIPELINE_MAX_AGENT_TURNS", "12")))
    except ValueError:
        return 12
