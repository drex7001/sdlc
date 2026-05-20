"""End-to-end integration tests for the repair loop.

These run the full pipeline against the bundled flask-status target with the
mock provider scripted to emit broken-then-clean codegen / repair outputs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.approval import ApprovalMode
from pipeline.audit import AuditStore
from pipeline.config import Settings
from pipeline.llm.providers.mock import _CODEGEN_RESPONSE, _INIT_PY, MockClient
from pipeline.orchestrator import PipelineError, run_pipeline

pytestmark = pytest.mark.integration


def _settings(sample_target: Path, runs_dir: Path, audit_db: Path) -> Settings:
    base = Settings.load()
    return replace(
        base,
        llm_provider="mock",
        llm_model="mock-v1",
        target_dir=sample_target,
        runs_dir=runs_dir,
        audit_db=audit_db,
        approver="test@local",
    )


def _broken_codegen() -> dict:
    """Same canned codegen response, but __init__.py has an unused import.

    ``import os`` triggers ruff's F401 — the repair agent's job is to remove it.
    """
    broken_init = "import os  # F401 — repair me\n" + _INIT_PY
    files = [dict(f) for f in _CODEGEN_RESPONSE["files"]]
    for entry in files:
        if entry["path"].endswith("__init__.py"):
            entry["content"] = broken_init
    return {"files": files, "summary": _CODEGEN_RESPONSE["summary"]}


def _clean_repair() -> dict:
    """Fix the unused import — restore __init__.py to the canonical form."""
    return {
        "files": [
            {"path": "src/sample_app/__init__.py", "action": "modify", "content": _INIT_PY},
        ],
        "summary": "Removed unused 'import os' (ruff F401).",
    }


@pytest.fixture(autouse=True)
def _reset_mock_scenarios() -> None:
    MockClient.reset_scenarios()
    yield
    MockClient.reset_scenarios()


def test_repair_loop_recovers_a_failing_run(
    sample_target: Path, runs_dir: Path, audit_db: Path, example_spec_path: Path,
) -> None:
    MockClient.set_scenario("codegen_agent", [{"write": _broken_codegen()}])
    MockClient.set_scenario("repair", [{"write": _clean_repair()}])

    settings = _settings(sample_target, runs_dir, audit_db)
    result = run_pipeline(
        spec_path=example_spec_path, settings=settings, approval_mode=ApprovalMode.AUTO,
    )

    assert result.status == "succeeded"
    assert result.repair_attempts == 1
    assert result.gate_report is not None
    assert result.gate_report.all_passed

    store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    stages = [s["stage"] for s in store.stages_for_run(result.run_id)]
    assert "repair_1" in stages
    assert "gates_after_repair_1" in stages


def test_repair_loop_gives_up_after_max_attempts(
    sample_target: Path, runs_dir: Path, audit_db: Path, example_spec_path: Path,
) -> None:
    MockClient.set_scenario("codegen_agent", [{"write": _broken_codegen()}])
    # Repair attempts both return __init__.py *still broken* — neither fix sticks.
    broken_init_modify = {
        "files": [{
            "path": "src/sample_app/__init__.py",
            "action": "modify",
            "content": "import os  # F401 — still broken\n" + _INIT_PY,
        }],
        "summary": "(no real fix)",
    }
    MockClient.set_scenario("repair", [
        {"write": broken_init_modify},
        {"write": broken_init_modify},
    ])

    settings = _settings(sample_target, runs_dir, audit_db)
    settings = replace(settings, max_repair_attempts=2)

    with pytest.raises(PipelineError, match="after 2 repair attempt"):
        run_pipeline(
            spec_path=example_spec_path, settings=settings, approval_mode=ApprovalMode.AUTO,
        )

    store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    run = store.list_runs(limit=1)[0]
    assert run["status"] == "failed"
    stage_names = [s["stage"] for s in store.stages_for_run(run["run_id"])]
    assert "repair_1" in stage_names
    assert "repair_2" in stage_names
    assert "gates_after_repair_2" in stage_names
