"""In-memory per-run metrics, flushed to audit at end of run."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..llm.client import LLMResponse


@dataclass
class MetricsRecorder:
    started_at: float = field(default_factory=time.perf_counter)
    stage_durations_ms: dict[str, int] = field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    ac_total: int = 0
    ac_covered: int = 0
    gates_passed: int = 0
    gates_total: int = 0

    def stage(self, name: str, duration_ms: int) -> None:
        self.stage_durations_ms[name] = duration_ms

    def llm(self, response: LLMResponse) -> None:
        self.total_input_tokens += response.usage.get("input_tokens", 0)
        self.total_output_tokens += response.usage.get("output_tokens", 0)

    def gates(self, passed: int, total: int) -> None:
        self.gates_passed = passed
        self.gates_total = total

    def ac(self, covered: int, total: int) -> None:
        self.ac_covered = covered
        self.ac_total = total

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_duration_ms": int((time.perf_counter() - self.started_at) * 1000),
            "stage_durations_ms": self.stage_durations_ms,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "ac_total": self.ac_total,
            "ac_covered": self.ac_covered,
            "ac_coverage_pct": (
                round(100.0 * self.ac_covered / self.ac_total, 1) if self.ac_total else 0.0
            ),
            "gates_passed": self.gates_passed,
            "gates_total": self.gates_total,
        }
