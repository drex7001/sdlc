"""Unit tests for the repair stage agent.

Exercises the read_gate_log tool path and the orchestrator-level retry budget
by driving the mock provider with scripted scenarios.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.audit import AuditStore
from pipeline.gates import GateReport
from pipeline.gates.base import GateOutcome
from pipeline.implementation.repair import run_repair_agent
from pipeline.intake import load_and_validate
from pipeline.llm.providers.mock import MockClient
from pipeline.planning import Plan
from pipeline.planning.planner import Task


@pytest.fixture(autouse=True)
def _reset_mock_scenarios() -> None:
    MockClient.reset_scenarios()
    yield
    MockClient.reset_scenarios()


def _plan() -> Plan:
    return Plan(
        tasks=[Task(id="T1", title="x")],
        design_summary="d",
        impacted_files=["src/sample_app/__init__.py"],
        risks=[],
        test_strategy="t",
    )


def _failing_report(gate: str, log: str) -> GateReport:
    report = GateReport()
    report.outcomes.append(
        GateOutcome(name=gate, passed=False, duration_ms=10, summary="exit=1", output=log)
    )
    return report


def test_repair_agent_can_read_failing_gate_log(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    MockClient.set_scenario("repair", [
        {"read_gate_log": "ruff"},
        {"write": {
            "files": [{"path": "src/sample_app/__init__.py", "action": "modify", "content": "# fixed\n"}],
            "summary": "fixed ruff issue",
        }},
    ])
    audit = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    spec = load_and_validate(Path(__file__).resolve().parents[2] / "specs" / "example.yaml")
    run = audit.start_run(
        spec_name="t", spec_hash="h", spec_source_path=Path("/tmp/spec.yaml"),
        llm_provider="mock", llm_model="mock-v1", prompt_version="v1",
        approver="t@x", target_dir=sample_target,
    )
    changes = run_repair_agent(
        spec=spec, plan=_plan(),
        impl_summary="s1", test_summary="s2",
        gate_report=_failing_report("ruff", "F401 unused import 'os'"),
        target_dir=sample_target,
        llm=MockClient(), model="mock-v1",
        prompts_dir=Path(__file__).resolve().parents[2] / "pipeline" / "llm" / "prompts",
        prompt_version="v1",
        run=run, audit=audit, attempt=1,
    )
    assert changes.files[0].path == "src/sample_app/__init__.py"

    # The captured tool transcript should include the gate log we exposed.
    transcript = (run.artifacts_dir / "prompts.jsonl").read_text(encoding="utf-8")
    assert "read_gate_log" in transcript


def test_repair_agent_rejects_invalid_write_and_accepts_corrected_candidate(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    bad_candidate = _flask_before_request_candidate(return_none=False)
    good_candidate = _flask_before_request_candidate(return_none=True)
    MockClient.set_scenario("repair", [
        {"write": {
            "files": [{
                "path": "src/sample_app/__init__.py",
                "action": "modify",
                "content": bad_candidate,
            }],
            "summary": "widened hook return type",
        }},
        {"write": {
            "files": [{
                "path": "src/sample_app/__init__.py",
                "action": "modify",
                "content": good_candidate,
            }],
            "summary": "added explicit None return",
        }},
    ])

    audit = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    spec = load_and_validate(Path(__file__).resolve().parents[2] / "specs" / "example.yaml")
    run = audit.start_run(
        spec_name="t", spec_hash="h", spec_source_path=Path("/tmp/spec.yaml"),
        llm_provider="mock", llm_model="mock-v1", prompt_version="v1",
        approver="t@x", target_dir=sample_target,
    )

    changes = run_repair_agent(
        spec=spec,
        plan=_plan(),
        impl_summary="s1",
        test_summary="s2",
        gate_report=_failing_report("mypy", "src/sample_app/__init__.py:16: error"),
        target_dir=sample_target,
        llm=MockClient(),
        model="mock-v1",
        prompts_dir=Path(__file__).resolve().parents[2] / "pipeline" / "llm" / "prompts",
        prompt_version="v1",
        run=run,
        audit=audit,
        attempt=1,
    )

    content = changes.files[0].content
    assert "return None" in content
    assert "request, Response" not in content

    transcript = (run.artifacts_dir / "prompts.jsonl").read_text(encoding="utf-8")
    assert "write_files failed validation" in transcript
    assert "Missing return statement" in transcript


def test_repair_agent_unknown_gate_returns_error(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    """Asking for a gate that did not run yields an error string the agent can recover from."""
    MockClient.set_scenario("repair", [
        {"read_gate_log": "does_not_exist"},
        {"write": {
            "files": [{"path": "src/sample_app/__init__.py", "action": "modify", "content": "# ok\n"}],
            "summary": "moved on",
        }},
    ])
    audit = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    spec = load_and_validate(Path(__file__).resolve().parents[2] / "specs" / "example.yaml")
    run = audit.start_run(
        spec_name="t", spec_hash="h", spec_source_path=Path("/tmp/spec.yaml"),
        llm_provider="mock", llm_model="mock-v1", prompt_version="v1",
        approver="t@x", target_dir=sample_target,
    )
    changes = run_repair_agent(
        spec=spec, plan=_plan(),
        impl_summary="s1", test_summary="s2",
        gate_report=_failing_report("ruff", "log"),
        target_dir=sample_target,
        llm=MockClient(), model="mock-v1",
        prompts_dir=Path(__file__).resolve().parents[2] / "pipeline" / "llm" / "prompts",
        prompt_version="v1",
        run=run, audit=audit, attempt=1,
    )
    assert len(changes.files) == 1


def test_repair_prompt_warns_against_test_module_global_reset() -> None:
    prompt = (
        Path(__file__).resolve().parents[2]
        / "pipeline"
        / "llm"
        / "prompts"
        / "v1"
        / "repair.md"
    ).read_text(encoding="utf-8")

    assert "global next_id" in prompt
    assert "does not reset `src.crud_app.endpoints.next_id`" in prompt


def _flask_before_request_candidate(*, return_none: bool) -> str:
    none_line = "        return None\n" if return_none else ""
    return f'''"""Sample Flask application used as the pipeline's code-gen target."""

from __future__ import annotations

from flask import Flask, jsonify, make_response, request, Response


def create_app() -> Flask:
    app = Flask(__name__)

    @app.before_request
    def rate_limit() -> Response | None:
        if request.args.get("blocked"):
            response = make_response(jsonify({{"error": "blocked"}}), 429)
            return response
{none_line}
    @app.get("/")
    def index() -> dict[str, str]:
        return {{"message": "hello"}}

    return app
'''
