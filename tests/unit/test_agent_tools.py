"""Unit tests for the tool-using agent loop.

Drives :class:`ToolLoop` against the mock provider's scripted-scenario API so
we can exercise the loop's turn-counting, sandbox-rejection retry, and
terminal-tool semantics without an API key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.audit import AuditStore
from pipeline.implementation.agent_tools import (
    CODEGEN_TOOLS,
    AgentLoopError,
    ToolLoop,
)
from pipeline.implementation.codegen_schema import GeneratedChanges
from pipeline.llm.providers.mock import MockClient


@pytest.fixture(autouse=True)
def _reset_mock_scenarios() -> None:
    MockClient.reset_scenarios()
    yield
    MockClient.reset_scenarios()


def _make_loop(
    *,
    target: Path,
    allowed: set[str],
    audit_db: Path,
    runs_dir: Path,
    max_turns: int = 12,
    stage_label: str = "codegen",
) -> ToolLoop:
    audit = AuditStore(db_path=audit_db, runs_dir=runs_dir)
    run = audit.start_run(
        spec_name="t", spec_hash="h", spec_source_path=Path("/tmp/spec.yaml"),
        llm_provider="mock", llm_model="mock-v1", prompt_version="v1",
        approver="t@x", target_dir=target,
    )
    return ToolLoop(
        llm=MockClient(),
        model="mock-v1",
        system_prompt="<<STAGE:CODEGEN_AGENT>>",
        tools=CODEGEN_TOOLS,
        target_dir=target,
        allowed_paths=allowed,
        audit=audit,
        run=run,
        stage_label=stage_label,
        max_turns=max_turns,
    )


def test_terminal_write_files_returns_changes(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    MockClient.set_scenario("codegen_agent", [
        {"write": {
            "files": [{"path": "src/sample_app/__init__.py", "action": "modify", "content": "# noop\n"}],
            "summary": "trivial",
        }},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db, runs_dir=runs_dir,
    )
    result = loop.run_loop("go")
    assert isinstance(result.changes, GeneratedChanges)
    assert result.turns == 1
    assert result.changes.summary == "trivial"


def test_write_files_accepts_json_encoded_files_list(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    file_changes = [
        {"path": "src/sample_app/__init__.py", "action": "modify", "content": "# normalized\n"}
    ]
    MockClient.set_scenario("codegen_agent", [
        {"write": {"files": json.dumps(file_changes), "summary": "decoded list"}},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db,
        runs_dir=runs_dir,
    )

    result = loop.run_loop("go")

    assert result.changes.summary == "decoded list"
    assert result.changes.files[0].content == "# normalized\n"


def test_write_files_recovers_object_encoded_in_files_argument(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    file_changes = [
        {"path": "src/sample_app/__init__.py", "action": "modify", "content": "# nested\n"}
    ]
    MockClient.set_scenario("codegen_agent", [
        {"write": {"files": json.dumps({"files": file_changes, "summary": "nested object"})}},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db,
        runs_dir=runs_dir,
    )

    result = loop.run_loop("go")

    assert result.changes.summary == "nested object"
    assert result.changes.files[0].content == "# nested\n"


def test_write_files_recovers_split_payload_encoded_in_files_argument(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    file_changes = [
        {"path": "src/sample_app/__init__.py", "action": "modify", "content": "# split\n"}
    ]
    malformed_files = f'{json.dumps(file_changes)}, "summary": "split payload"}}'
    MockClient.set_scenario("codegen_agent", [
        {"write": {"files": malformed_files}},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db,
        runs_dir=runs_dir,
    )

    result = loop.run_loop("go")

    assert result.changes.summary == "split payload"
    assert result.changes.files[0].content == "# split\n"


def test_write_files_synthesizes_summary_when_missing(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    MockClient.set_scenario("codegen_agent", [
        {"write": {
            "files": [
                {"path": "src/sample_app/__init__.py", "action": "modify", "content": "# no summary\n"}
            ],
        }},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db,
        runs_dir=runs_dir,
    )

    result = loop.run_loop("go")

    assert result.changes.summary == "Generated 1 file change(s)."
    assert result.changes.files[0].content == "# no summary\n"


def test_sandbox_violation_recovers_on_next_turn(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    """First write_files tries an unauthorised path. Second uses an approved one."""
    MockClient.set_scenario("codegen_agent", [
        {"write": {
            "files": [{"path": "src/sample_app/forbidden.py", "action": "create", "content": "x"}],
            "summary": "oops",
        }},
        {"write": {
            "files": [{"path": "src/sample_app/__init__.py", "action": "modify", "content": "# fixed\n"}],
            "summary": "corrected",
        }},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db, runs_dir=runs_dir,
    )
    result = loop.run_loop("go")
    assert result.turns == 2
    assert result.changes.files[0].path == "src/sample_app/__init__.py"


def test_two_consecutive_sandbox_violations_halt(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    MockClient.set_scenario("codegen_agent", [
        {"write": {
            "files": [{"path": "src/sample_app/forbidden.py", "action": "create", "content": "x"}],
            "summary": "1",
        }},
        {"write": {
            "files": [{"path": "tests/escape.py", "action": "create", "content": "y"}],
            "summary": "2",
        }},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db, runs_dir=runs_dir,
    )
    with pytest.raises(AgentLoopError, match="twice in a row"):
        loop.run_loop("go")


def test_turn_budget_exhaustion_halts(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    # Three read-only turns and no commit — should hit max_turns=2.
    MockClient.set_scenario("codegen_agent", [
        {"read": "src/sample_app/__init__.py"},
        {"read": "src/sample_app/__init__.py"},
        {"read": "src/sample_app/__init__.py"},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db, runs_dir=runs_dir,
        max_turns=2,
    )
    with pytest.raises(AgentLoopError, match="exhausted"):
        loop.run_loop("go")


def test_no_tool_call_fails_loop(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    MockClient.set_scenario("codegen_agent", [
        {"text": "I'm done, no diff needed"},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db, runs_dir=runs_dir,
    )
    with pytest.raises(AgentLoopError, match="without calling write_files"):
        loop.run_loop("go")


def test_read_file_tool_returns_contents(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    MockClient.set_scenario("codegen_agent", [
        {"read": "src/sample_app/__init__.py"},
        {"write": {
            "files": [{"path": "src/sample_app/__init__.py", "action": "modify", "content": "# ok\n"}],
            "summary": "after reading",
        }},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db, runs_dir=runs_dir,
    )
    result = loop.run_loop("go")
    assert result.turns == 2

    # Verify the read_file result actually appeared in the JSONL transcript.
    jsonl = loop.run.artifacts_dir / "prompts.jsonl"
    text = jsonl.read_text(encoding="utf-8")
    assert "read_file" in text


def test_tool_results_paired_with_tool_uses_in_jsonl(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    """Every tool_use must have a matching tool_result record in the JSONL."""
    MockClient.set_scenario("codegen_agent", [
        {"read": "src/sample_app/__init__.py"},
        {"write": {
            "files": [{"path": "src/sample_app/__init__.py", "action": "modify", "content": "# ok\n"}],
            "summary": "done",
        }},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db, runs_dir=runs_dir,
    )
    loop.run_loop("go")

    entries = [
        json.loads(line)
        for line in (loop.run.artifacts_dir / "prompts.jsonl").read_text().splitlines()
        if line.strip()
    ]
    use_ids = {tu["id"] for e in entries if e.get("kind") == "agent_turn" for tu in e.get("tool_uses", [])}
    result_ids = {e["tool_use_id"] for e in entries if e.get("kind") == "tool_result"}
    assert use_ids and use_ids == result_ids, f"unpaired: uses={use_ids} results={result_ids}"
    # write_files terminator records a success preview.
    write_results = [e for e in entries if e.get("kind") == "tool_result" and e["tool_name"] == "write_files"]
    assert write_results and write_results[0]["is_error"] is False
    assert "committed" in write_results[0]["content_preview"]


def test_sandbox_violation_persists_error_tool_result(
    sample_target: Path, runs_dir: Path, audit_db: Path,
) -> None:
    MockClient.set_scenario("codegen_agent", [
        {"write": {
            "files": [{"path": "src/sample_app/forbidden.py", "action": "create", "content": "x"}],
            "summary": "oops",
        }},
        {"write": {
            "files": [{"path": "src/sample_app/__init__.py", "action": "modify", "content": "# ok\n"}],
            "summary": "fixed",
        }},
    ])
    loop = _make_loop(
        target=sample_target,
        allowed={"src/sample_app/__init__.py"},
        audit_db=audit_db, runs_dir=runs_dir,
    )
    loop.run_loop("go")

    entries = [
        json.loads(line)
        for line in (loop.run.artifacts_dir / "prompts.jsonl").read_text().splitlines()
        if line.strip()
    ]
    error_results = [e for e in entries if e.get("kind") == "tool_result" and e["is_error"]]
    assert error_results, "expected at least one error tool_result for the sandbox violation"
    assert "sandbox" in error_results[0]["content_preview"].lower()
