"""Planning stage: produces a Plan from a validated FeatureSpec via the LLM."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..audit import AuditStore, RunRecord
from ..intake import FeatureSpec
from ..llm import LLMClient, load_prompt
from ..llm.client import LLMResponse


class Task(BaseModel):
    id: str
    title: str


class Plan(BaseModel):
    """Structured implementation plan."""

    tasks: list[Task] = Field(min_length=1)
    design_summary: str
    impacted_files: list[str] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    test_strategy: str

    def impacted_set(self) -> set[str]:
        return set(self.impacted_files)


def _list_tree(root: Path, limit: int = 200) -> list[str]:
    """Compact file tree of the target repo, for the LLM context."""
    paths: list[str] = []
    for p in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if "__pycache__" in p.parts:
            continue
        if p.is_file():
            paths.append(p.relative_to(root).as_posix())
            if len(paths) >= limit:
                break
    return paths


def _read_existing(target_dir: Path, impacted: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in impacted:
        f = target_dir / rel
        if f.exists() and f.is_file():
            out[rel] = f.read_text(encoding="utf-8")
    return out


def _extract_json(text: str) -> dict[str, Any]:
    """Tolerantly extract a JSON object from LLM output.

    Accepts either a bare JSON object or one inside a ```json fence.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    # Heuristic: find first '{' and last '}'.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("could not locate JSON object in LLM output")
    return json.loads(text[start : end + 1])


def plan_from_spec(
    *,
    spec: FeatureSpec,
    target_dir: Path,
    llm: LLMClient,
    model: str,
    prompts_dir: Path,
    prompt_version: str,
    run: RunRecord,
    audit: AuditStore,
    max_tokens: int = 4096,
) -> Plan:
    """Run the planning LLM call, persist artifacts, return a validated Plan.

    Raises ``ValueError`` if the LLM output cannot be parsed into a valid Plan.
    The pipeline treats this as a stage failure.
    """
    system = load_prompt(prompts_dir, prompt_version, "plan")
    user_prompt = json.dumps(
        {
            "spec": spec.model_dump(),
            "target_tree": _list_tree(target_dir),
        },
        indent=2,
    )

    response: LLMResponse = llm.complete(
        system=system, prompt=user_prompt, model=model,
        temperature=0.1, max_tokens=max_tokens,
    )
    audit.record_prompt(run, stage="plan", system=system, prompt=user_prompt, response=response)

    try:
        raw = _extract_json(response.text)
        plan = Plan.model_validate(raw)
    except (json.JSONDecodeError, ValidationError, ValueError) as e:
        raise ValueError(f"planning output invalid: {e}") from e

    plan = _apply_plan_guardrails(target_dir, plan)
    audit.write_json_artifact(run, "plan.json", plan.model_dump())
    return plan


def gather_plan_context(target_dir: Path, plan: Plan) -> dict[str, str]:
    """Existing file contents the codegen agent needs as context."""
    return _read_existing(target_dir, plan.impacted_files)


def _apply_plan_guardrails(target_dir: Path, plan: Plan) -> Plan:
    missing_imports = _missing_local_import_targets(target_dir, plan.impacted_files)
    new_impacted = list(plan.impacted_files)
    for rel in sorted(missing_imports):
        if rel not in plan.impacted_set():
            new_impacted.append(rel)
    if new_impacted == plan.impacted_files:
        return plan
    return plan.model_copy(update={"impacted_files": new_impacted})


def _missing_local_import_targets(target_dir: Path, rel_files: list[str]) -> set[str]:
    src_dir = target_dir / "src"
    packages = _local_package_names(src_dir)
    if not packages:
        return set()

    missing: set[str] = set()
    for rel in rel_files:
        path = target_dir / rel
        if not path.is_file() or path.suffix != ".py":
            continue
        for module in _imported_modules(path.read_text(encoding="utf-8")):
            parts = module.split(".")
            if len(parts) < 2 or parts[0] not in packages:
                continue
            module_file = src_dir.joinpath(*parts).with_suffix(".py")
            package_init = src_dir.joinpath(*parts, "__init__.py")
            if not module_file.exists() and not package_init.exists():
                missing.add(module_file.relative_to(target_dir).as_posix())
    return missing


def _local_package_names(src_dir: Path) -> set[str]:
    if not src_dir.is_dir():
        return set()
    return {
        child.name
        for child in src_dir.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }


def _imported_modules(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules
