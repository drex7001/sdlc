"""Audit store: SQLite index + filesystem artifacts.

Every pipeline event is persisted twice:
 - SQLite row in audit.db (queryable summary)
 - artifact file under runs/<run_id>/ (full payload)

Write order is artifact-first, then index. A row whose artifact file is
missing is treated as stale and surfaced by the dashboard as "incomplete".
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
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


_USAGE_PRICE_KEYS = {
    "input_tokens": ("input", "input_tokens"),
    "output_tokens": ("output", "output_tokens"),
    "cache_creation_input_tokens": (
        "cache_creation_input",
        "cache_creation_input_tokens",
    ),
    "cache_read_input_tokens": ("cache_read_input", "cache_read_input_tokens"),
}

_GENERIC_PRICE_ENV = {
    "input_tokens": "PIPELINE_INPUT_TOKEN_COST_PER_1M",
    "output_tokens": "PIPELINE_OUTPUT_TOKEN_COST_PER_1M",
    "cache_creation_input_tokens": "PIPELINE_CACHE_CREATION_INPUT_TOKEN_COST_PER_1M",
    "cache_read_input_tokens": "PIPELINE_CACHE_READ_INPUT_TOKEN_COST_PER_1M",
}


def _estimate_prompt_cost_usd(
    *,
    provider: str,
    model: str,
    usage: dict[str, Any],
) -> tuple[float, bool]:
    rates = _pricing_for(provider=provider, model=model)
    configured = bool(rates)
    cost = 0.0
    for usage_key, aliases in _USAGE_PRICE_KEYS.items():
        tokens = usage.get(usage_key)
        if not isinstance(tokens, int | float):
            continue
        rate = _rate_for(rates, usage_key, aliases)
        if rate is None:
            continue
        configured = True
        cost += (float(tokens) * rate) / 1_000_000
    return cost, configured


def _pricing_for(*, provider: str, model: str) -> dict[str, float]:
    pricing = _pricing_json()
    for key in (f"{provider}/{model}", model, provider, "default"):
        raw_rates = pricing.get(key) if key else None
        if isinstance(raw_rates, dict):
            return {
                str(rate_key): float(rate)
                for rate_key, rate in raw_rates.items()
                if isinstance(rate, int | float)
            }

    rates: dict[str, float] = {}
    for usage_key, env_name in _GENERIC_PRICE_ENV.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        with contextlib.suppress(ValueError):
            rates[usage_key] = float(raw)
    return rates


def _pricing_json() -> dict[str, Any]:
    raw = os.environ.get("PIPELINE_LLM_PRICING_JSON")
    if not raw:
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return decoded
    return {}


def _rate_for(
    rates: dict[str, float],
    usage_key: str,
    aliases: tuple[str, ...],
) -> float | None:
    if usage_key in rates:
        return rates[usage_key]
    for alias in aliases:
        if alias in rates:
            return rates[alias]
    return None


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
            run_columns = {row["name"] for row in con.execute("PRAGMA table_info(runs)")}
            if "target_dir" not in run_columns:
                con.execute("ALTER TABLE runs ADD COLUMN target_dir TEXT")
            metric_columns = {
                row["name"] for row in con.execute("PRAGMA table_info(metrics)")
            }
            metric_migrations = {
                "total_input_tokens": "INTEGER",
                "total_output_tokens": "INTEGER",
                "ac_coverage_pct": "REAL",
                "llm_calls": "INTEGER",
                "llm_latency_ms": "INTEGER",
                "stage_duration_ms": "INTEGER",
                "gate_duration_ms": "INTEGER",
                "gate_results_count": "INTEGER",
                "estimated_cost_usd": "REAL",
            }
            for column, column_type in metric_migrations.items():
                if column not in metric_columns:
                    con.execute(f"ALTER TABLE metrics ADD COLUMN {column} {column_type}")

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
                "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
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

    def record_metrics(self, run: RunRecord, metrics: dict[str, Any]) -> dict[str, Any]:
        metrics = self._complete_metrics(run.run_id, metrics)
        (run.artifacts_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2), encoding="utf-8"
        )
        with self._connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO metrics(
                       run_id, total_tokens, total_duration_ms,
                       ac_total, ac_covered, gates_passed, gates_total,
                       total_input_tokens, total_output_tokens, ac_coverage_pct,
                       llm_calls, llm_latency_ms, stage_duration_ms,
                       gate_duration_ms, gate_results_count, estimated_cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id,
                    metrics.get("total_tokens"),
                    metrics.get("total_duration_ms"),
                    metrics.get("ac_total"),
                    metrics.get("ac_covered"),
                    metrics.get("gates_passed"),
                    metrics.get("gates_total"),
                    metrics.get("total_input_tokens"),
                    metrics.get("total_output_tokens"),
                    metrics.get("ac_coverage_pct"),
                    metrics.get("llm_calls"),
                    metrics.get("llm_latency_ms"),
                    metrics.get("stage_duration_ms"),
                    metrics.get("gate_duration_ms"),
                    metrics.get("gate_results_count"),
                    metrics.get("estimated_cost_usd"),
                ),
            )
        return metrics

    def metrics_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM metrics WHERE run_id=?", (run_id,)
            ).fetchone()
            run_row = con.execute(
                "SELECT artifacts_dir FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row and not run_row:
            return None

        metrics: dict[str, Any] = dict(row) if row else {"run_id": run_id}
        if run_row:
            metrics_file = Path(run_row["artifacts_dir"]) / "metrics.json"
            if metrics_file.exists():
                with contextlib.suppress(json.JSONDecodeError):
                    artifact_metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
                    if isinstance(artifact_metrics, dict):
                        metrics.update(artifact_metrics)
        return self._complete_metrics(run_id, metrics)

    def aggregate_metrics_for_run(self, run_id: str) -> dict[str, Any]:
        """Compute every metric available from audit rows and run artifacts."""
        run = self.get_run(run_id)
        if not run:
            return {}

        prompt_usage = self._prompt_usage_totals(Path(run["artifacts_dir"]))
        if not prompt_usage["llm_calls"]:
            prompt_usage = self._prompt_usage_from_index(run_id)

        stages = self.stages_for_run(run_id)
        stage_durations = {
            s["stage"]: int(s["duration_ms"] or 0)
            for s in stages
            if s.get("duration_ms") is not None
        }
        stage_duration_ms = sum(stage_durations.values())

        gates = self.gates_for_run(run_id)
        gate_durations = [
            int(g["duration_ms"] or 0)
            for g in gates
            if g.get("duration_ms") is not None
        ]

        return {
            "total_input_tokens": prompt_usage["usage"].get("input_tokens", 0),
            "total_output_tokens": prompt_usage["usage"].get("output_tokens", 0),
            "total_tokens": (
                prompt_usage["usage"].get("input_tokens", 0)
                + prompt_usage["usage"].get("output_tokens", 0)
            ),
            "llm_calls": prompt_usage["llm_calls"],
            "llm_latency_ms": prompt_usage["llm_latency_ms"],
            "prompt_usage": prompt_usage["usage"],
            "stage_count": len(stages),
            "stage_duration_ms": stage_duration_ms,
            "stage_durations_ms": stage_durations,
            "gate_results_count": len(gates),
            "gate_duration_ms": sum(gate_durations),
            "estimated_cost_usd": prompt_usage["estimated_cost_usd"],
            "cost_configured": prompt_usage["cost_configured"],
        }

    def _complete_metrics(self, run_id: str, metrics: dict[str, Any]) -> dict[str, Any]:
        completed = dict(metrics)
        aggregate = self.aggregate_metrics_for_run(run_id)

        for key in (
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "llm_calls",
            "llm_latency_ms",
            "prompt_usage",
            "stage_count",
            "stage_duration_ms",
            "stage_durations_ms",
            "gate_results_count",
            "gate_duration_ms",
            "estimated_cost_usd",
            "cost_configured",
        ):
            value = aggregate.get(key)
            if value not in (None, {}, []):
                completed[key] = value

        if not completed.get("total_duration_ms"):
            completed["total_duration_ms"] = completed.get("stage_duration_ms", 0)

        ac_total = completed.get("ac_total") or 0
        ac_covered = completed.get("ac_covered") or 0
        completed["ac_coverage_pct"] = (
            round(100.0 * ac_covered / ac_total, 1) if ac_total else 0.0
        )
        return completed

    def _prompt_usage_totals(self, artifacts_dir: Path) -> dict[str, Any]:
        usage_totals: dict[str, int] = {}
        llm_calls = 0
        llm_latency_ms = 0
        jsonl = artifacts_dir / "prompts.jsonl"
        if not jsonl.exists():
            return {
            "usage": usage_totals,
            "llm_calls": llm_calls,
            "llm_latency_ms": llm_latency_ms,
            "estimated_cost_usd": 0.0,
            "cost_configured": False,
        }

        total_cost = 0.0
        cost_configured = False
        with jsonl.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    rec = json.loads(line)
                    usage = rec.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    provider = str(rec.get("provider") or "")
                    model = str(rec.get("model") or "")
                    llm_calls += 1
                    for key, value in usage.items():
                        if isinstance(value, int | float):
                            usage_totals[key] = usage_totals.get(key, 0) + int(value)
                    latency = rec.get("latency_ms")
                    if isinstance(latency, int | float):
                        llm_latency_ms += int(latency)
                    call_cost, call_cost_configured = _estimate_prompt_cost_usd(
                        provider=provider,
                        model=model,
                        usage=usage,
                    )
                    total_cost += call_cost
                    cost_configured = cost_configured or call_cost_configured
        return {
            "usage": usage_totals,
            "llm_calls": llm_calls,
            "llm_latency_ms": llm_latency_ms,
            "estimated_cost_usd": round(total_cost, 6),
            "cost_configured": cost_configured,
        }

    def _prompt_usage_from_index(self, run_id: str) -> dict[str, Any]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT provider, model,
                          COUNT(*) AS llm_calls,
                          COALESCE(SUM(input_tokens), 0) AS input_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(latency_ms), 0) AS llm_latency_ms
                   FROM prompt_calls
                   WHERE run_id=?
                   GROUP BY provider, model""",
                (run_id,),
            ).fetchall()
        total_input_tokens = 0
        total_output_tokens = 0
        llm_calls = 0
        llm_latency_ms = 0
        estimated_cost_usd = 0.0
        cost_configured = False
        for row in rows:
            usage = {
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
            }
            total_input_tokens += usage["input_tokens"]
            total_output_tokens += usage["output_tokens"]
            llm_calls += int(row["llm_calls"] or 0)
            llm_latency_ms += int(row["llm_latency_ms"] or 0)
            group_cost, group_cost_configured = _estimate_prompt_cost_usd(
                provider=str(row["provider"] or ""),
                model=str(row["model"] or ""),
                usage=usage,
            )
            estimated_cost_usd += group_cost
            cost_configured = cost_configured or group_cost_configured
        usage = {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        }
        return {
            "usage": usage,
            "llm_calls": llm_calls,
            "llm_latency_ms": llm_latency_ms,
            "estimated_cost_usd": round(estimated_cost_usd, 6),
            "cost_configured": cost_configured,
        }

    # --- artifact helpers ----------------------------------------------------

    def write_artifact(self, run: RunRecord, relpath: str, content: str) -> Path:
        path = run.artifacts_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_json_artifact(self, run: RunRecord, relpath: str, payload: Any) -> Path:
        return self.write_artifact(run, relpath, json.dumps(payload, indent=2, default=str))
