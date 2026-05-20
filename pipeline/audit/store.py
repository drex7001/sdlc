"""Audit store: SQLite index + filesystem artifacts.

Every pipeline event is persisted twice:
 - SQLite row in audit.db (queryable summary)
 - artifact file under runs/<run_id>/ (full payload)

Write order is artifact-first, then index. A row whose artifact file is
missing is treated as stale and surfaced by the dashboard as "incomplete".
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..llm.client import LLMResponse

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass
class RunRecord:
    run_id: str
    artifacts_dir: Path
    spec_name: str
    spec_hash: str


class AuditStore:
    """Single facade over the index DB and the artifact filesystem.

    Intentionally synchronous and connection-per-call. Concurrency for the
    prototype is single-writer (one pipeline run at a time).
    """

    def __init__(self, db_path: Path, runs_dir: Path) -> None:
        self.db_path = db_path
        self.runs_dir = runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # --- lifecycle -----------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            # Idempotent migration: ADD COLUMN on pre-existing DBs that were
            # created before target_dir was part of the schema.
            existing = {row["name"] for row in con.execute("PRAGMA table_info(runs)")}
            if "target_dir" not in existing:
                con.execute("ALTER TABLE runs ADD COLUMN target_dir TEXT")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    # --- runs ----------------------------------------------------------------

    def start_run(
        self,
        *,
        spec_name: str,
        spec_hash: str,
        spec_source_path: Path,
        llm_provider: str,
        llm_model: str,
        prompt_version: str,
        approver: str,
        target_dir: Path,
    ) -> RunRecord:
        run_id = f"{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        artifacts_dir = self.runs_dir / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "patches").mkdir(exist_ok=True)
        with self._connect() as con:
            con.execute(
                """INSERT INTO runs(run_id, spec_name, spec_hash, spec_source_path,
                       llm_provider, llm_model, prompt_version, status, approver,
                       started_at, artifacts_dir, target_dir)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
                (
                    run_id, spec_name, spec_hash, str(spec_source_path),
                    llm_provider, llm_model, prompt_version, approver,
                    _utc_now(), str(artifacts_dir), str(target_dir),
                ),
            )
        return RunRecord(
            run_id=run_id, artifacts_dir=artifacts_dir,
            spec_name=spec_name, spec_hash=spec_hash,
        )

    def set_run_status(self, run_id: str, status: str, stage: str | None = None) -> None:
        with self._connect() as con:
            if status in {"succeeded", "failed", "rejected"}:
                con.execute(
                    "UPDATE runs SET status=?, current_stage=?, ended_at=? WHERE run_id=?",
                    (status, stage, _utc_now(), run_id),
                )
            else:
                con.execute(
                    "UPDATE runs SET status=?, current_stage=? WHERE run_id=?",
                    (status, stage, run_id),
                )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- stages --------------------------------------------------------------

    def start_stage(self, run_id: str, stage: str) -> int:
        with self._connect() as con:
            cur = con.execute(
                "INSERT INTO stages(run_id, stage, status, started_at) VALUES (?, ?, 'running', ?)",
                (run_id, stage, _utc_now()),
            )
            return int(cur.lastrowid or 0)

    def finish_stage(
        self,
        stage_id: int,
        *,
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE stages SET status=?, ended_at=?, duration_ms=?, error=? WHERE id=?",
                (status, _utc_now(), duration_ms, error, stage_id),
            )

    def stages_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM stages WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- prompt calls (LLM interactions) -------------------------------------

    def record_prompt(
        self,
        run: RunRecord,
        *,
        stage: str,
        system: str,
        prompt: str,
        response: LLMResponse,
    ) -> Path:
        # Append to a JSONL log; SQLite stores summary only.
        jsonl = run.artifacts_dir / "prompts.jsonl"
        record = {
            "stage": stage,
            "provider": response.provider,
            "model": response.model,
            "system": system,
            "prompt": prompt,
            "response_text": response.text,
            "usage": response.usage,
            "latency_ms": response.latency_ms,
            "created_at": _utc_now(),
        }
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        with self._connect() as con:
            con.execute(
                """INSERT INTO prompt_calls(run_id, stage, provider, model,
                       input_tokens, output_tokens, latency_ms, artifact_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id, stage, response.provider, response.model,
                    response.usage.get("input_tokens"),
                    response.usage.get("output_tokens"),
                    response.latency_ms, "prompts.jsonl", _utc_now(),
                ),
            )
        return jsonl

    def prompt_calls_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """LLM call summary rows for ``run_id``, ordered by id (== insertion order)."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM prompt_calls WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- approvals -----------------------------------------------------------

    def record_approval(
        self,
        run: RunRecord,
        *,
        checkpoint: str,
        decision: str,
        approver: str,
        comment: str | None = None,
    ) -> None:
        approvals_file = run.artifacts_dir / "approvals.json"
        existing: list[dict[str, Any]] = []
        if approvals_file.exists():
            existing = json.loads(approvals_file.read_text(encoding="utf-8"))
        existing.append({
            "checkpoint": checkpoint,
            "decision": decision,
            "approver": approver,
            "comment": comment,
            "created_at": _utc_now(),
        })
        approvals_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        with self._connect() as con:
            con.execute(
                """INSERT INTO approvals(run_id, checkpoint, decision, approver, comment, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run.run_id, checkpoint, decision, approver, comment, _utc_now()),
            )

    def approvals_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM approvals WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- gates ---------------------------------------------------------------

    def record_gate(
        self,
        run: RunRecord,
        *,
        gate: str,
        status: str,
        duration_ms: int,
        summary: str,
        artifact_relpath: str | None = None,
    ) -> None:
        gates_file = run.artifacts_dir / "gates.json"
        existing: list[dict[str, Any]] = []
        if gates_file.exists():
            existing = json.loads(gates_file.read_text(encoding="utf-8"))
        existing.append({
            "gate": gate,
            "status": status,
            "duration_ms": duration_ms,
            "summary": summary,
            "artifact_path": artifact_relpath,
            "created_at": _utc_now(),
        })
        gates_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        with self._connect() as con:
            con.execute(
                """INSERT INTO gate_results(run_id, gate, status, duration_ms, summary, artifact_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run.run_id, gate, status, duration_ms, summary, artifact_relpath, _utc_now()),
            )

    def gates_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM gate_results WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- metrics -------------------------------------------------------------

    def record_metrics(self, run: RunRecord, metrics: dict[str, Any]) -> None:
        (run.artifacts_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2), encoding="utf-8"
        )
        with self._connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO metrics(
                       run_id, total_tokens, total_duration_ms,
                       ac_total, ac_covered, gates_passed, gates_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id,
                    metrics.get("total_tokens"),
                    metrics.get("total_duration_ms"),
                    metrics.get("ac_total"),
                    metrics.get("ac_covered"),
                    metrics.get("gates_passed"),
                    metrics.get("gates_total"),
                ),
            )

    def metrics_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM metrics WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    # --- artifact helpers ----------------------------------------------------

    def write_artifact(self, run: RunRecord, relpath: str, content: str) -> Path:
        path = run.artifacts_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_json_artifact(self, run: RunRecord, relpath: str, payload: Any) -> Path:
        return self.write_artifact(run, relpath, json.dumps(payload, indent=2, default=str))
