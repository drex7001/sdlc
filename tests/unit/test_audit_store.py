"""Audit store: SQLite + filesystem round trips."""

from __future__ import annotations

import json
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
        target_dir=Path("/tmp/target"),
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
        target_dir=Path("/tmp/target"),
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
        target_dir=Path("/tmp/target"),
    )
    resp = LLMResponse(text="ok", model="m", provider="mock",
                       usage={"input_tokens": 10, "output_tokens": 5}, latency_ms=1)
    store.record_prompt(run, stage="plan", system="sys", prompt="hi", response=resp)
    store.record_prompt(run, stage="codegen", system="sys", prompt="hi2", response=resp)
    lines = (run.artifacts_dir / "prompts.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_record_metrics_hydrates_available_audit_metrics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "PIPELINE_LLM_PRICING_JSON",
        json.dumps({
            "mock/m": {
                "input": 100000,
                "output": 200000,
                "cache_read_input": 50000,
            }
        }),
    )
    store = _store(tmp_path)
    run = store.start_run(
        spec_name="x", spec_hash="h", spec_source_path=Path("/tmp/x"),
        llm_provider="mock", llm_model="m", prompt_version="v1", approver="a",
        target_dir=Path("/tmp/target"),
    )
    resp = LLMResponse(
        text="ok",
        model="m",
        provider="mock",
        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3},
        latency_ms=7,
    )
    store.record_prompt(run, stage="plan", system="sys", prompt="hi", response=resp)
    sid = store.start_stage(run.run_id, "plan")
    store.finish_stage(sid, status="succeeded", duration_ms=42)
    store.record_gate(
        run,
        gate="ruff",
        status="passed",
        duration_ms=11,
        summary="ok",
    )

    metrics = store.record_metrics(
        run,
        {
            "total_tokens": 0,
            "total_duration_ms": 100,
            "ac_total": 2,
            "ac_covered": 1,
            "gates_passed": 1,
            "gates_total": 1,
        },
    )

    assert metrics["total_input_tokens"] == 10
    assert metrics["total_output_tokens"] == 5
    assert metrics["total_tokens"] == 15
    assert metrics["llm_calls"] == 1
    assert metrics["llm_latency_ms"] == 7
    assert metrics["prompt_usage"]["cache_read_input_tokens"] == 3
    assert metrics["estimated_cost_usd"] == 2.15
    assert metrics["cost_configured"] is True
    assert metrics["stage_duration_ms"] == 42
    assert metrics["gate_duration_ms"] == 11
    assert metrics["ac_coverage_pct"] == 50.0

    from_db = store.metrics_for_run(run.run_id)
    assert from_db is not None
    assert from_db["total_tokens"] == 15
    assert from_db["estimated_cost_usd"] == 2.15
    assert json.loads((run.artifacts_dir / "metrics.json").read_text())["total_tokens"] == 15


def test_start_run_records_target_dir(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "some-project"
    target.mkdir()
    run = store.start_run(
        spec_name="x", spec_hash="h", spec_source_path=Path("/tmp/x.yaml"),
        llm_provider="mock", llm_model="m", prompt_version="v1", approver="a",
        target_dir=target,
    )
    row = store.get_run(run.run_id)
    assert row is not None
    assert row["target_dir"] == str(target)


def test_approvals_appended_and_indexed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = store.start_run(
        spec_name="x", spec_hash="h", spec_source_path=Path("/tmp/x"),
        llm_provider="mock", llm_model="m", prompt_version="v1", approver="a",
        target_dir=Path("/tmp/target"),
    )
    store.record_approval(run, checkpoint="plan", decision="approved", approver="u")
    store.record_approval(run, checkpoint="finalize", decision="approved", approver="u")
    rows = store.approvals_for_run(run.run_id)
    assert [r["checkpoint"] for r in rows] == ["plan", "finalize"]
