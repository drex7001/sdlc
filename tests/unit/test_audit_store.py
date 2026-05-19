"""Audit store: SQLite + filesystem round trips."""

from __future__ import annotations

from pathlib import Path

from pipeline.audit import AuditStore
from pipeline.llm.client import LLMResponse


def _store(tmp_path: Path) -> AuditStore:
    return AuditStore(db_path=tmp_path / "audit.db", runs_dir=tmp_path / "runs")


def test_start_run_creates_artifacts_dir(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.start_run(
        spec_name="x", spec_hash="h", spec_source_path=Path("/tmp/x.yaml"),
        llm_provider="mock", llm_model="m", prompt_version="v1", approver="a",
    )
    assert run.artifacts_dir.exists()
    assert (run.artifacts_dir / "patches").exists()
    row = store.get_run(run.run_id)
    assert row is not None
    assert row["status"] == "running"


def test_stage_lifecycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.start_run(
        spec_name="x", spec_hash="h", spec_source_path=Path("/tmp/x"),
        llm_provider="mock", llm_model="m", prompt_version="v1", approver="a",
    )
    sid = store.start_stage(run.run_id, "plan")
    store.finish_stage(sid, status="succeeded", duration_ms=42)
    stages = store.stages_for_run(run.run_id)
    assert len(stages) == 1
    assert stages[0]["status"] == "succeeded"
    assert stages[0]["duration_ms"] == 42


def test_record_prompt_appends_jsonl(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.start_run(
        spec_name="x", spec_hash="h", spec_source_path=Path("/tmp/x"),
        llm_provider="mock", llm_model="m", prompt_version="v1", approver="a",
    )
    resp = LLMResponse(text="ok", model="m", provider="mock",
                       usage={"input_tokens": 10, "output_tokens": 5}, latency_ms=1)
    store.record_prompt(run, stage="plan", system="sys", prompt="hi", response=resp)
    store.record_prompt(run, stage="codegen", system="sys", prompt="hi2", response=resp)
    lines = (run.artifacts_dir / "prompts.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_approvals_appended_and_indexed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.start_run(
        spec_name="x", spec_hash="h", spec_source_path=Path("/tmp/x"),
        llm_provider="mock", llm_model="m", prompt_version="v1", approver="a",
    )
    store.record_approval(run, checkpoint="plan", decision="approved", approver="u")
    store.record_approval(run, checkpoint="finalize", decision="approved", approver="u")
    rows = store.approvals_for_run(run.run_id)
    assert [r["checkpoint"] for r in rows] == ["plan", "finalize"]
