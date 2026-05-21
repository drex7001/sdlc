"""Test generation stage.

Tests are produced by the LLM and must:
  - live under tests/ in the target repo
  - carry an `AC: AC-N` marker in each test docstring for coverage tracking
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from ..audit import AuditStore, RunRecord
from ..implementation.codegen import GeneratedChanges
from ..implementation.normalization import normalize_python_changes
from ..implementation.sandbox import SandboxViolation, validate_paths
from ..intake import FeatureSpec
from ..llm import LLMClient, load_prompt
from ..planning import Plan
from ..planning.planner import _extract_json


def _validate_test_paths(
    changes: GeneratedChanges,
    allowed: set[str],
    target_dir: Path,
) -> None:
    """Use the shared sandbox, then constrain testgen to tests/."""
    validate_paths(
        paths=[fc.path for fc in changes.files],
        allowed=allowed,
        target_dir=target_dir,
    )
    for fc in changes.files:
        p = PurePosixPath(fc.path)
        if not p.parts or p.parts[0] != "tests":
            raise SandboxViolation(
                f"test file {fc.path!r} must live under tests/."
            )


def generate_tests(
    *,
    spec: FeatureSpec,
    plan: Plan,
    impl_changes: GeneratedChanges,
    target_dir: Path,
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
    existing_test_files: dict[str, str] = {}
    for path in allowed_test_paths:
        target = target_dir / path
        if target.exists() and target.is_file():
            existing_test_files[path] = target.read_text(encoding="utf-8")
    user_prompt = json.dumps(
        {
            "allowed_test_paths": allowed_test_paths,
            "existing_test_files": existing_test_files,
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

    _validate_test_paths(changes, plan.impacted_set(), target_dir)
    changes = normalize_python_changes(changes=changes, target_dir=target_dir)

    audit.write_json_artifact(
        run,
        "testgen_output.json",
        {
            "summary": changes.summary,
            "files": [{"path": f.path, "action": f.action} for f in changes.files],
        },
    )
    return changes
