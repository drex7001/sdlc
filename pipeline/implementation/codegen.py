"""Codegen stage: plan + spec → file operations via LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..audit import AuditStore, RunRecord
from ..intake import FeatureSpec
from ..llm import LLMClient, load_prompt
from ..planning import Plan, gather_plan_context
from ..planning.planner import _extract_json, _list_tree
from .sandbox import validate_paths


class FileChange(BaseModel):
    path: str
    action: Literal["create", "modify", "delete"]
    content: str = ""


class GeneratedChanges(BaseModel):
    files: list[FileChange] = Field(min_length=1)
    summary: str


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
    system = load_prompt(prompts_dir, prompt_version, "codegen")
    user_prompt = json.dumps(
        {
            "spec": spec.model_dump(),
            "plan": plan.model_dump(),
            "existing_files": gather_plan_context(target_dir, plan),
            "target_tree": _list_tree(target_dir),
        },
        indent=2,
    )

    response = llm.complete(
        system=system, prompt=user_prompt, model=model,
        temperature=0.1, max_tokens=max_tokens,
    )
    audit.record_prompt(run, stage="codegen", system=system, prompt=user_prompt, response=response)

    try:
        raw = _extract_json(response.text)
        changes = GeneratedChanges.model_validate(raw)
    except (ValueError, ValidationError) as e:
        raise ValueError(f"codegen output invalid: {e}") from e

    # Governance check: every path must be in plan.impacted_files.
    validate_paths(
        paths=[fc.path for fc in changes.files],
        allowed=plan.impacted_set(),
        target_dir=target_dir,
    )

    audit.write_json_artifact(
        run,
        "codegen_output.json",
        {
            "summary": changes.summary,
            "files": [{"path": f.path, "action": f.action} for f in changes.files],
        },
    )
    return changes
