"""Dashboard runner: per-call target_dir override.

The runner must construct a fresh, immutable Settings for every launch so
concurrent runs against different projects do not stomp on each other.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dashboard import runner
from pipeline.audit import AuditStore
from pipeline.config import Settings


@pytest.fixture
def spec_path() -> Path:
    return Path(__file__).resolve().parents[2] / "specs" / "example.yaml"


def _make_project(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.mkdir()
    (p / "pyproject.toml").write_text(f"[project]\nname = '{name}'\n")
    return p


def test_launch_run_threads_target_dir_to_run_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spec_path: Path,
) -> None:
    """Per-call target_dir lands in the Settings handed to run_pipeline,
    and a second launch against a different target does NOT bleed into it."""
    audit_db = tmp_path / "audit.db"
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("PIPELINE_AUDIT_DB", str(audit_db))
    monkeypatch.setenv("PIPELINE_RUNS_DIR", str(runs_dir))

    project_a = _make_project(tmp_path, "project-a")
    project_b = _make_project(tmp_path, "project-b")

    captured: list[Settings] = []

    def fake_run_pipeline(
        *, spec_path: Path, settings: Settings, approval_mode: object,
    ) -> None:
        captured.append(settings)
        store = AuditStore(db_path=settings.audit_db, runs_dir=settings.runs_dir)
        store.start_run(
            spec_name="t", spec_hash="h", spec_source_path=spec_path,
            llm_provider="mock", llm_model="mock-v1", prompt_version="v1",
            approver=settings.approver, target_dir=settings.target_dir,
        )

    monkeypatch.setattr(runner, "run_pipeline", fake_run_pipeline)

    run_id_a = runner.launch_run(
        spec_path=spec_path, approver="alice@x", target_dir=project_a,
    )
    # ``started_at`` is second-resolution; sleep so the second run gets a
    # later timestamp and the runner's polling can tell them apart.
    time.sleep(1.1)
    run_id_b = runner.launch_run(
        spec_path=spec_path, approver="bob@x", target_dir=project_b,
    )

    assert run_id_a != run_id_b
    assert len(captured) == 2
    assert captured[0].target_dir == project_a
    assert captured[0].approver == "alice@x"
    assert captured[1].target_dir == project_b
    assert captured[1].approver == "bob@x"

    # And the audit rows record per-run target_dir.
    store = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    row_a = store.get_run(run_id_a)
    row_b = store.get_run(run_id_b)
    assert row_a is not None
    assert row_b is not None
    assert row_a["target_dir"] == str(project_a)
    assert row_b["target_dir"] == str(project_b)
