"""Test generation stage.

Tests are produced by the LLM and must:
  - live under tests/ in the target repo
  - carry an `AC: AC-N` marker in each test docstring for coverage tracking
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from ..audit import AuditStore, RunRecord
from ..implementation.codegen import GeneratedChanges
from ..implementation.sandbox import SandboxViolation
from ..intake import FeatureSpec
from ..llm import LLMClient, load_prompt
from ..planning import Plan
from ..planning.planner import _extract_json


def _validate_test_paths(changes: GeneratedChanges, allowed: set[str]) -> None:
    """Two checks: paths must be in the plan AND must live under tests/."""
    for fc in changes.files:
        if fc.path not in allowed:
            raise SandboxViolation(
                f"test file {fc.path!r} is not in the plan's impacted_files."
            )
        if not fc.path.startswith("tests/"):
            raise SandboxViolation(
                f"test file {fc.path!r} must live under tests/."
            )


def generate_tests(
    *,
    spec: FeatureSpec,
    plan: Plan,
    impl_changes: GeneratedChanges,
    llm: LLMClient,
    model: str,
    prompts_dir: Path,
    prompt_version: str,
    run: RunRecord,
    audit: AuditStore,
    max_tokens: int = 8192,
) -> GeneratedChanges:
    system = load_prompt(prompts_dir, prompt_version, "testgen")
    allowed_test_paths = sorted(p for p in plan.impacted_set() if p.startswith("tests/"))
    user_prompt = json.dumps(
        {
            "allowed_test_paths": allowed_test_paths,
            "spec": spec.model_dump(),
            "plan": plan.model_dump(),
            "implementation_files": [
                {"path": fc.path, "content": fc.content}
                for fc in impl_changes.files
                if fc.action != "delete"
            ],
        },
        indent=2,
    )

    response = llm.complete(
        system=system, prompt=user_prompt, model=model,
        temperature=0.1, max_tokens=max_tokens,
    )
    audit.record_prompt(run, stage="testgen", system=system, prompt=user_prompt, response=response)

    try:
        raw = _extract_json(response.text)
        changes = GeneratedChanges.model_validate(raw)
    except (ValueError, ValidationError) as e:
        raise ValueError(f"testgen output invalid: {e}") from e

    _validate_test_paths(changes, plan.impacted_set())

    audit.write_json_artifact(
        run,
        "testgen_output.json",
        {
            "summary": changes.summary,
            "files": [{"path": f.path, "action": f.action} for f in changes.files],
        },
    )
    return changes
