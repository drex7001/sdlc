"""Unit tests for planner guardrails."""

from __future__ import annotations

from pathlib import Path

from pipeline.planning.planner import Plan, Task, _apply_plan_guardrails


def test_plan_guardrail_adds_missing_local_module_imported_by_existing_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "crud-api"
    (target / "src" / "crud_app").mkdir(parents=True)
    (target / "tests").mkdir()
    (target / "src" / "crud_app" / "__init__.py").write_text("", encoding="utf-8")
    (target / "tests" / "test_items.py").write_text(
        "from crud_app import create_app\nfrom crud_app.items import _reset_store\n",
        encoding="utf-8",
    )
    plan = Plan(
        tasks=[Task(id="T1", title="Add list endpoint")],
        design_summary="d",
        impacted_files=["src/crud_app/__init__.py", "tests/test_items.py"],
        risks=[],
        test_strategy="t",
    )

    guarded = _apply_plan_guardrails(target, plan)

    assert guarded.impacted_files == [
        "src/crud_app/__init__.py",
        "tests/test_items.py",
        "src/crud_app/items.py",
    ]


def test_plan_guardrail_leaves_existing_local_modules_alone(tmp_path: Path) -> None:
    target = tmp_path / "crud-api"
    (target / "src" / "crud_app").mkdir(parents=True)
    (target / "tests").mkdir()
    (target / "src" / "crud_app" / "__init__.py").write_text("", encoding="utf-8")
    (target / "src" / "crud_app" / "items.py").write_text("", encoding="utf-8")
    (target / "tests" / "test_items.py").write_text(
        "from crud_app.items import _reset_store\n",
        encoding="utf-8",
    )
    plan = Plan(
        tasks=[Task(id="T1", title="Add list endpoint")],
        design_summary="d",
        impacted_files=["src/crud_app/__init__.py", "tests/test_items.py"],
        risks=[],
        test_strategy="t",
    )

    guarded = _apply_plan_guardrails(target, plan)

    assert guarded.impacted_files == plan.impacted_files
