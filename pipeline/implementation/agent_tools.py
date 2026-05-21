"""Tool definitions + multi-turn loop driver for the codegen / repair agent.

The agent is given a small, sandbox-aware tool set:

* ``list_files`` — recursive listing inside the target repository (no escape).
* ``read_file`` — UTF-8 read, target-dir bounded.
* ``read_gate_log`` — read a captured gate log from this run (repair only).
* ``write_files`` — commit final changes. Validates against
  ``plan.impacted_files`` via the existing :func:`pipeline.implementation.sandbox.validate_paths`.
  A successful call ends the loop.

The loop driver — :class:`ToolLoop` — owns the conversation, dispatches tool
calls, persists every turn into ``runs/<run-id>/prompts.jsonl``, and enforces
turn / sandbox-error budgets.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..audit import AuditStore, RunRecord
from ..llm.client import LLMClient, ToolUse
from .codegen_schema import GeneratedChanges
from .sandbox import SandboxViolation, validate_paths

# --- Tool schemas (Anthropic shape; OpenAI provider translates) --------------

LIST_FILES_TOOL: dict[str, Any] = {
    "name": "list_files",
    "description": (
        "List files (relative paths) inside the target repository. Recursive. "
        "Hidden dirs and __pycache__ are excluded. Returns up to 200 paths."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative directory inside the target repo. Default is the repo root.",
            },
        },
    },
}

READ_FILE_TOOL: dict[str, Any] = {
    "name": "read_file",
    "description": "Read a UTF-8 text file inside the target repository.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path inside the target repo"},
        },
        "required": ["path"],
    },
}

READ_GATE_LOG_TOOL: dict[str, Any] = {
    "name": "read_gate_log",
    "description": (
        "Read the captured output of a failing gate from the current pipeline run. "
        "Available gate names: 'ruff', 'mypy', 'pytest', 'bandit', 'policy'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "gate": {"type": "string", "description": "Gate name."},
        },
        "required": ["gate"],
    },
}

WRITE_FILES_TOOL: dict[str, Any] = {
    "name": "write_files",
    "description": (
        "Commit the final file changes. Each entry is the complete file content, "
        "not a diff. Paths are validated against the plan's impacted_files sandbox: "
        "any violation returns an error so you can correct it and try again. A "
        "successful call ends the agent loop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "action": {"type": "string", "enum": ["create", "modify", "delete"]},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "action"],
                },
            },
            "summary": {"type": "string", "description": "2-4 sentence summary of the change."},
        },
        "required": ["files", "summary"],
    },
}


CODEGEN_TOOLS: list[dict[str, Any]] = [LIST_FILES_TOOL, READ_FILE_TOOL, WRITE_FILES_TOOL]
REPAIR_TOOLS: list[dict[str, Any]] = [
    LIST_FILES_TOOL, READ_FILE_TOOL, READ_GATE_LOG_TOOL, WRITE_FILES_TOOL,
]


# --- Loop ---------------------------------------------------------------------


class AgentLoopError(RuntimeError):
    """Raised when the agent loop cannot reach a clean termination."""


class WriteValidationError(RuntimeError):
    """Raised when a candidate write is syntactically valid but gate-invalid."""


@dataclass
class WriteValidationResult:
    changes: GeneratedChanges | None
    error: str = ""

    @classmethod
    def accepted(cls, changes: GeneratedChanges) -> WriteValidationResult:
        return cls(changes=changes)

    @classmethod
    def rejected(cls, error: str) -> WriteValidationResult:
        return cls(changes=None, error=error)


@dataclass
class ToolLoopResult:
    changes: GeneratedChanges
    turns: int
    tool_calls: int


@dataclass
class ToolLoop:
    """Drives a single tool-using agent invocation to completion.

    The loop ends when the agent successfully calls ``write_files`` (paths
    cleared by the sandbox), or fails with :class:`AgentLoopError` when:

    * the turn budget is exhausted,
    * the sandbox rejects two ``write_files`` calls in a row, or
    * the agent stops without ever calling a tool.
    """

    llm: LLMClient
    model: str
    system_prompt: str
    tools: list[dict[str, Any]]
    target_dir: Path
    allowed_paths: set[str]
    audit: AuditStore
    run: RunRecord
    stage_label: str  # e.g. "codegen" or "repair_1"
    max_tokens: int = 8192
    max_turns: int = 12
    # Repair-only: gate logs to expose via read_gate_log.
    gate_logs: dict[str, str] = field(default_factory=dict)
    validate_write: Callable[[GeneratedChanges], WriteValidationResult] | None = None

    def run_loop(self, initial_user_message: str) -> ToolLoopResult:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": initial_user_message},
        ]
        consecutive_sandbox_errors = 0
        tool_calls_total = 0

        for turn in range(1, self.max_turns + 1):
            response = self.llm.complete_with_tools(
                system=self.system_prompt,
                messages=messages,
                tools=self.tools,
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=0.1,
            )
            self._persist_turn(turn, response)

            if not response.tool_uses:
                # Model stopped without committing — surface as a stage failure.
                raise AgentLoopError(
                    f"{self.stage_label}: agent stopped on turn {turn} without calling write_files. "
                    f"Last text: {response.text[:240]}"
                )

            messages.append({"role": "assistant", "content": response.assistant_content})

            tool_results: list[dict[str, Any]] = []
            committed: GeneratedChanges | None = None
            had_sandbox_error_this_turn = False

            for tu in response.tool_uses:
                tool_calls_total += 1
                try:
                    payload, terminated = self._dispatch(tu)
                except SandboxViolation as e:
                    msg = f"sandbox rejected write_files: {e}"
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": tu.id,
                        "content": msg, "is_error": True,
                    })
                    self._persist_tool_result(turn, tu, msg, is_error=True)
                    had_sandbox_error_this_turn = True
                    continue
                except ValidationError as e:
                    msg = f"invalid arguments: {e}"
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": tu.id,
                        "content": msg, "is_error": True,
                    })
                    self._persist_tool_result(turn, tu, msg, is_error=True)
                    continue
                except WriteValidationError as e:
                    msg = f"write_files failed validation: {e}"
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": tu.id,
                        "content": msg, "is_error": True,
                    })
                    self._persist_tool_result(turn, tu, msg, is_error=True)
                    continue
                except Exception as e:  # noqa: BLE001
                    msg = f"error: {e}"
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": tu.id,
                        "content": msg, "is_error": True,
                    })
                    self._persist_tool_result(turn, tu, msg, is_error=True)
                    continue

                if terminated:
                    # Synthesise a success result for the JSONL audit. We don't
                    # send this back to the model (the loop is terminating), but
                    # the transcript should still show "write_files committed".
                    summary = (
                        f"committed {len(payload.files)} file(s): "
                        + ", ".join(f"{f.path} ({f.action})" for f in payload.files)
                    )
                    self._persist_tool_result(turn, tu, summary, is_error=False)
                    committed = payload  # GeneratedChanges
                    break

                content_str = payload if isinstance(payload, str) else json.dumps(payload)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": content_str, "is_error": False,
                })
                self._persist_tool_result(turn, tu, content_str, is_error=False)

            if had_sandbox_error_this_turn:
                consecutive_sandbox_errors += 1
                if consecutive_sandbox_errors >= 2:
                    raise AgentLoopError(
                        f"{self.stage_label}: write_files rejected twice in a row by sandbox; halting."
                    )
            else:
                consecutive_sandbox_errors = 0

            if committed is not None:
                return ToolLoopResult(changes=committed, turns=turn, tool_calls=tool_calls_total)

            messages.append({"role": "user", "content": tool_results})

        raise AgentLoopError(
            f"{self.stage_label}: agent exhausted {self.max_turns} turns without committing files."
        )

    # --- dispatch helpers ----------------------------------------------------

    def _dispatch(self, tu: ToolUse) -> tuple[Any, bool]:
        """Return (result_payload, terminated). result_payload is either a
        plain string/dict to show the agent, or a GeneratedChanges when the
        loop should terminate."""
        if tu.name == "list_files":
            return self._list_files(str(tu.input.get("path") or ".")), False
        if tu.name == "read_file":
            return self._read_file(str(tu.input["path"])), False
        if tu.name == "read_gate_log":
            return self._read_gate_log(str(tu.input["gate"])), False
        if tu.name == "write_files":
            changes = self._write_files(tu.input)
            return changes, True
        return f"unknown tool: {tu.name}", False

    def _list_files(self, rel: str) -> str:
        base = (self.target_dir / rel).resolve()
        try:
            base.relative_to(self.target_dir.resolve())
        except ValueError:
            return f"error: path {rel!r} escapes target directory"
        if not base.exists():
            return f"error: path {rel!r} does not exist"
        if not base.is_dir():
            return f"error: path {rel!r} is not a directory"
        paths: list[str] = []
        for p in sorted(base.rglob("*")):
            try:
                rel_p = p.relative_to(self.target_dir)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel_p.parts):
                continue
            if "__pycache__" in rel_p.parts:
                continue
            if p.is_file():
                paths.append(rel_p.as_posix())
                if len(paths) >= 200:
                    break
        return "\n".join(paths) if paths else "(no files)"

    def _read_file(self, rel: str) -> str:
        target = (self.target_dir / rel).resolve()
        try:
            target.relative_to(self.target_dir.resolve())
        except ValueError:
            return f"error: path {rel!r} escapes target directory"
        if not target.exists():
            return f"error: file {rel!r} does not exist"
        if not target.is_file():
            return f"error: {rel!r} is not a file"
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"error: file {rel!r} is not valid UTF-8 text"

    def _read_gate_log(self, gate: str) -> str:
        if gate not in self.gate_logs:
            return (
                f"error: no captured log for gate {gate!r}. "
                f"Available: {sorted(self.gate_logs)}"
            )
        return self.gate_logs[gate] or "(empty log)"

    def _write_files(self, args: dict[str, Any]) -> GeneratedChanges:
        args = _normalise_write_files_args(args)
        # Pydantic validation surfaces malformed file entries as a ValidationError.
        changes = GeneratedChanges.model_validate(args)
        # Sandbox check — raises SandboxViolation, which the loop will turn
        # into a tool_result error so the model can retry.
        validate_paths(
            paths=[fc.path for fc in changes.files],
            allowed=self.allowed_paths,
            target_dir=self.target_dir,
        )
        if self.validate_write is not None:
            validation = self.validate_write(changes)
            if validation.changes is None:
                raise WriteValidationError(validation.error)
            changes = validation.changes
        return changes

    # --- persistence ---------------------------------------------------------

    def _persist_tool_result(
        self, turn: int, tu: ToolUse, content: str, *, is_error: bool,
    ) -> None:
        """Append a ``tool_result`` record to prompts.jsonl right after dispatch.

        The full content stays on disk (for ``read_file`` it's the target file
        itself, for ``read_gate_log`` it's ``gate_<name>.log``). We persist a
        4 KB preview here so a single ``jq`` sweep of prompts.jsonl tells the
        whole tool conversation without dragging in megabytes.
        """
        cap = 4096
        truncated = len(content) > cap
        record = {
            "kind": "tool_result",
            "stage": self.stage_label,
            "turn": turn,
            "tool_use_id": tu.id,
            "tool_name": tu.name,
            "is_error": is_error,
            "content_bytes": len(content),
            "content_truncated": truncated,
            "content_preview": content[:cap] + ("…" if truncated else ""),
            "created_at": _utc_iso(),
        }
        jsonl = self.run.artifacts_dir / "prompts.jsonl"
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def _persist_turn(self, turn: int, response: Any) -> None:
        """Append one turn to prompts.jsonl and a summary row to prompt_calls."""
        jsonl = self.run.artifacts_dir / "prompts.jsonl"
        record = {
            "stage": self.stage_label,
            "turn": turn,
            "kind": "agent_turn",
            "provider": response.provider,
            "model": response.model,
            "system": self.system_prompt,
            "assistant_text": response.text,
            "tool_uses": [
                {"id": tu.id, "name": tu.name, "input": tu.input}
                for tu in response.tool_uses
            ],
            "stop_reason": response.stop_reason,
            "usage": response.usage,
            "latency_ms": response.latency_ms,
            "created_at": _utc_iso(),
        }
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        # Mirror into the SQLite prompt_calls table for fast queries.
        # We rebuild a thin LLMResponse-like dict for record_prompt by hand
        # since the per-turn shape differs from the single-shot call.
        try:
            with self.audit._connect() as con:  # noqa: SLF001
                con.execute(
                    """INSERT INTO prompt_calls(run_id, stage, provider, model,
                           input_tokens, output_tokens, latency_ms, artifact_path, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.run.run_id,
                        f"{self.stage_label}.turn{turn}",
                        response.provider, response.model,
                        response.usage.get("input_tokens"),
                        response.usage.get("output_tokens"),
                        response.latency_ms, "prompts.jsonl", _utc_iso(),
                    ),
                )
        except Exception:  # noqa: BLE001
            # Non-fatal — the JSONL is the source of truth, SQLite is an index.
            pass


def _utc_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _normalise_write_files_args(args: dict[str, Any]) -> dict[str, Any]:
    """Repair common malformed tool payloads before schema validation.

    Some smaller models over-stringify nested tool inputs or accidentally put
    the whole object body inside the ``files`` argument. Normalizing here saves
    retry turns while still letting Pydantic reject genuinely invalid payloads.
    """
    normalised = dict(args)
    files_arg = normalised.get("files")
    if isinstance(files_arg, str):
        decoded = _decode_files_string(files_arg)
        if isinstance(decoded, dict):
            normalised = {**normalised, **decoded}
            if "summary" in args:
                normalised["summary"] = args["summary"]
        else:
            normalised["files"] = decoded

    if "summary" not in normalised and "files" in normalised:
        normalised["summary"] = _default_write_files_summary(normalised["files"])

    return normalised


def _decode_files_string(raw: str) -> Any:
    """Decode a stringified ``files`` argument, including split object bodies."""
    for candidate in (raw, f'{{"files": {raw}'):
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(candidate)
    return raw


def _default_write_files_summary(files: Any) -> str:
    if isinstance(files, list):
        return f"Generated {len(files)} file change(s)."
    return "Generated file changes."
