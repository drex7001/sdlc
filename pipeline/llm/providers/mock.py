"""Deterministic mock LLM provider.

Used for offline development, CI, and unit tests. Produces canned but
realistic outputs for each pipeline stage so the full end-to-end flow can be
exercised without any API keys.

Dispatch is by *stage sentinel* embedded in the system prompt. This keeps the
mock decoupled from the actual prompt wording.

For the tool-using codegen / repair stages the mock supports two modes:

* *Default* (no scripted scenario): a single turn that emits one ``write_files``
  tool_use carrying the canned codegen response. Lets the existing
  integration tests exercise the agent loop end-to-end with no API key.
* *Scripted* (per-test): set :attr:`MockClient.scenarios` keyed by stage to a
  list of turn descriptors. Each descriptor either commits files
  (``{"write": {...}}``) or proposes a sandbox-violating path
  (``{"write_bad": ...}``) or emits a non-tool turn (``{"text": "..."}``)
  to test exotic failure modes.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..client import LLMResponse, ToolUse

STAGE_SENTINELS = {
    "<<STAGE:PLAN>>": "plan",
    "<<STAGE:CODEGEN>>": "codegen",
    "<<STAGE:CODEGEN_AGENT>>": "codegen_agent",
    "<<STAGE:TESTGEN>>": "testgen",
    "<<STAGE:REPAIR>>": "repair",
}


class MockClient:
    provider_name = "mock"

    # Class-level so tests can monkey-patch without re-instantiating. Each
    # entry is a list of turn descriptors; the mock pops one per call.
    scenarios: dict[str, list[dict[str, Any]]] = {}

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> LLMResponse:
        stage = self._detect_stage(system)
        start = time.perf_counter()
        text = _CANNED[stage]
        latency_ms = int((time.perf_counter() - start) * 1000)
        return LLMResponse(
            text=text,
            model=model,
            provider=self.provider_name,
            usage={"input_tokens": len(prompt) // 4, "output_tokens": len(text) // 4},
            raw={"stage": stage, "deterministic": True},
            latency_ms=latency_ms,
            stop_reason="end_turn",
        )

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> LLMResponse:
        stage = self._detect_stage(system)
        start = time.perf_counter()

        descriptor = self._next_turn(stage)
        if descriptor is None:
            # Default: terminal write_files call with the canned codegen blob.
            descriptor = {"write": _CODEGEN_RESPONSE}

        tool_use_id = f"toolu_{uuid.uuid4().hex[:12]}"
        assistant_content: list[dict[str, Any]] = []
        tool_uses: list[ToolUse] = []
        text = descriptor.get("text", "")
        if text:
            assistant_content.append({"type": "text", "text": text})

        if "write" in descriptor:
            assistant_content.append({
                "type": "tool_use",
                "id": tool_use_id,
                "name": "write_files",
                "input": descriptor["write"],
            })
            tool_uses.append(ToolUse(id=tool_use_id, name="write_files", input=descriptor["write"]))
            stop_reason = "tool_use"
        elif "read" in descriptor:
            assistant_content.append({
                "type": "tool_use",
                "id": tool_use_id,
                "name": "read_file",
                "input": {"path": descriptor["read"]},
            })
            tool_uses.append(ToolUse(id=tool_use_id, name="read_file", input={"path": descriptor["read"]}))
            stop_reason = "tool_use"
        elif "read_gate_log" in descriptor:
            assistant_content.append({
                "type": "tool_use",
                "id": tool_use_id,
                "name": "read_gate_log",
                "input": {"gate": descriptor["read_gate_log"]},
            })
            tool_uses.append(ToolUse(
                id=tool_use_id, name="read_gate_log",
                input={"gate": descriptor["read_gate_log"]},
            ))
            stop_reason = "tool_use"
        else:
            # No tool — pure text turn (used to test "agent stopped" failure).
            stop_reason = "end_turn"

        latency_ms = int((time.perf_counter() - start) * 1000)
        prompt_len = sum(len(str(m.get("content", ""))) for m in messages)
        return LLMResponse(
            text=text,
            model=model,
            provider=self.provider_name,
            usage={"input_tokens": prompt_len // 4, "output_tokens": 256},
            raw={"stage": stage, "deterministic": True},
            latency_ms=latency_ms,
            stop_reason=stop_reason,
            tool_uses=tool_uses,
            assistant_content=assistant_content,
        )

    @staticmethod
    def _detect_stage(system: str) -> str:
        for sentinel, stage in STAGE_SENTINELS.items():
            if sentinel in system:
                return stage
        raise ValueError(
            "mock provider could not detect stage sentinel in system prompt; "
            "ensure prompt templates contain one of: " + ", ".join(STAGE_SENTINELS)
        )

    @classmethod
    def _next_turn(cls, stage: str) -> dict[str, Any] | None:
        queue = cls.scenarios.get(stage)
        if queue:
            return queue.pop(0)
        return None

    @classmethod
    def set_scenario(cls, stage: str, turns: list[dict[str, Any]]) -> None:
        """Test helper: queue the next ``turns`` responses for ``stage``."""
        cls.scenarios[stage] = list(turns)

    @classmethod
    def reset_scenarios(cls) -> None:
        cls.scenarios = {}


# --- Canned responses --------------------------------------------------------
#
# These are tied to the example spec `rate-limit-status-endpoint`. The mock is
# deliberately not "smart" — it returns these blobs regardless of the spec
# content. That is fine for a demo / CI: the goal is to exercise the pipeline
# mechanics, not to validate LLM intelligence.

_PLAN_RESPONSE: dict[str, Any] = {
    "tasks": [
        {"id": "T1", "title": "Add FixedWindowRateLimiter utility"},
        {"id": "T2", "title": "Add /status route returning JSON status + uptime"},
        {"id": "T3", "title": "Wire rate limiter to /status with per-IP keying"},
        {"id": "T4", "title": "Return 429 with Retry-After header for over-limit requests"},
    ],
    "design_summary": (
        "Introduce a small in-memory FixedWindowRateLimiter keyed by client IP "
        "(5 requests per 10s). Add a /status Flask view that records app start "
        "time and reports uptime_seconds. The view consults the limiter; on "
        "overage it returns HTTP 429 with a Retry-After header equal to the "
        "remaining seconds in the current window. State is process-local — "
        "matches the spec's 'no external store' constraint."
    ),
    "impacted_files": [
        "src/sample_app/__init__.py",
        "src/sample_app/rate_limit.py",
        "src/sample_app/status.py",
        "tests/test_status.py",
        "tests/test_rate_limit.py",
    ],
    "risks": [
        "Fixed-window limiter allows up to 2x burst across the window boundary; acceptable for /status.",
        "In-memory state resets on restart — explicitly listed as acceptable in the spec.",
        "time.monotonic() is used for window math; clock changes do not affect correctness.",
    ],
    "test_strategy": (
        "Unit tests for FixedWindowRateLimiter cover the allow/deny boundary and "
        "per-key isolation. Integration tests use Flask's test client to drive "
        "/status: AC-1 verifies the 200 path; AC-2 issues 6 requests from a "
        "fixed REMOTE_ADDR and asserts 429; AC-3 asserts Retry-After header is "
        "present and a positive integer; AC-4 uses two distinct REMOTE_ADDRs "
        "and asserts both stay under their independent quotas."
    ),
}


_RATE_LIMIT_PY = '''"""In-memory fixed-window rate limiter."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _WindowState:
    window_start: float
    count: int


class FixedWindowRateLimiter:
    """Per-key fixed-window limiter.

    Allows ``limit`` requests per ``window_seconds``. State lives in memory and
    is process-local. Safe for concurrent access via an internal lock.
    """

    def __init__(self, limit: int, window_seconds: float, clock=time.monotonic) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, _WindowState] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds).

        retry_after_seconds is 0 when allowed and otherwise the integer number
        of seconds remaining in the current window (always >= 1).
        """
        now = self._clock()
        with self._lock:
            state = self._state.get(key)
            if state is None or now - state.window_start >= self.window_seconds:
                self._state[key] = _WindowState(window_start=now, count=1)
                return True, 0
            if state.count < self.limit:
                state.count += 1
                return True, 0
            remaining = self.window_seconds - (now - state.window_start)
            return False, max(1, int(remaining) + 1)
'''


_STATUS_PY = '''"""GET /status view: liveness + uptime, rate-limited per IP."""

from __future__ import annotations

import time
from typing import Any

from flask import Blueprint, jsonify, request

from .rate_limit import FixedWindowRateLimiter

bp = Blueprint("status", __name__)

_START_TIME = time.monotonic()
_LIMITER = FixedWindowRateLimiter(limit=5, window_seconds=10.0)


def _client_ip() -> str:
    return request.remote_addr or "unknown"


@bp.get("/status")
def status() -> Any:
    allowed, retry_after = _LIMITER.check(_client_ip())
    if not allowed:
        response = jsonify({"error": "rate_limited"})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response
    uptime = int(time.monotonic() - _START_TIME)
    return jsonify({"status": "ok", "uptime_seconds": uptime})
'''


_INIT_PY = '''"""Sample Flask application used as the pipeline\'s code-gen target."""

from __future__ import annotations

from flask import Flask

from .status import bp as status_bp


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": "hello"}

    app.register_blueprint(status_bp)
    return app
'''


_TEST_STATUS_PY = '''"""Integration tests for /status endpoint.

Each test docstring carries an AC tag (e.g. AC: AC-1) so the pytest gate can
compute acceptance-criteria coverage.
"""

from __future__ import annotations

import pytest

from sample_app import create_app
from sample_app.status import _LIMITER


@pytest.fixture
def client():
    _LIMITER._state.clear()
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_status_returns_ok_and_uptime(client):
    """AC: AC-1"""
    resp = client.get("/status", environ_overrides={"REMOTE_ADDR": "10.0.0.1"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0


def test_sixth_request_same_ip_is_rate_limited(client):
    """AC: AC-2"""
    env = {"REMOTE_ADDR": "10.0.0.2"}
    for _ in range(5):
        assert client.get("/status", environ_overrides=env).status_code == 200
    resp = client.get("/status", environ_overrides=env)
    assert resp.status_code == 429


def test_rate_limited_response_has_retry_after(client):
    """AC: AC-3"""
    env = {"REMOTE_ADDR": "10.0.0.3"}
    for _ in range(5):
        client.get("/status", environ_overrides=env)
    resp = client.get("/status", environ_overrides=env)
    assert resp.status_code == 429
    retry_after = resp.headers.get("Retry-After")
    assert retry_after is not None
    assert int(retry_after) > 0


def test_different_ips_have_independent_quota(client):
    """AC: AC-4"""
    env_a = {"REMOTE_ADDR": "10.0.0.4"}
    env_b = {"REMOTE_ADDR": "10.0.0.5"}
    for _ in range(5):
        assert client.get("/status", environ_overrides=env_a).status_code == 200
    # IP A is now at the limit, IP B should still succeed.
    assert client.get("/status", environ_overrides=env_b).status_code == 200
'''


_TEST_RATE_LIMIT_PY = '''"""Unit tests for FixedWindowRateLimiter."""

from __future__ import annotations

import pytest

from sample_app.rate_limit import FixedWindowRateLimiter


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_allows_up_to_limit_then_denies():
    """AC: AC-2"""
    clock = FakeClock()
    limiter = FixedWindowRateLimiter(limit=5, window_seconds=10.0, clock=clock)
    for _ in range(5):
        allowed, retry = limiter.check("a")
        assert allowed and retry == 0
    allowed, retry = limiter.check("a")
    assert not allowed
    assert retry >= 1


def test_window_resets_after_elapsed_seconds():
    """AC: AC-2"""
    clock = FakeClock()
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=10.0, clock=clock)
    limiter.check("a")
    limiter.check("a")
    assert limiter.check("a")[0] is False
    clock.advance(10.5)
    assert limiter.check("a")[0] is True


def test_keys_are_isolated():
    """AC: AC-4"""
    clock = FakeClock()
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=10.0, clock=clock)
    limiter.check("a")
    limiter.check("a")
    assert limiter.check("a")[0] is False
    assert limiter.check("b")[0] is True


def test_invalid_construction_raises():
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(limit=0, window_seconds=10.0)
    with pytest.raises(ValueError):
        FixedWindowRateLimiter(limit=1, window_seconds=0)
'''


_CODEGEN_RESPONSE: dict[str, Any] = {
    "files": [
        {"path": "src/sample_app/rate_limit.py", "action": "create", "content": _RATE_LIMIT_PY},
        {"path": "src/sample_app/status.py", "action": "create", "content": _STATUS_PY},
        {"path": "src/sample_app/__init__.py", "action": "modify", "content": _INIT_PY},
    ],
    "summary": (
        "Added rate_limit.py (FixedWindowRateLimiter) and status.py (/status "
        "blueprint with per-IP limiting). Modified __init__.py to register "
        "the status blueprint."
    ),
}


_TESTGEN_RESPONSE: dict[str, Any] = {
    "files": [
        {"path": "tests/test_status.py", "action": "create", "content": _TEST_STATUS_PY},
        {"path": "tests/test_rate_limit.py", "action": "create", "content": _TEST_RATE_LIMIT_PY},
    ],
    "summary": (
        "Generated integration tests for /status covering AC-1..AC-4 and unit "
        "tests for the rate limiter. Each test carries an AC tag in its "
        "docstring for coverage tracking."
    ),
}


_CANNED = {
    "plan": json.dumps(_PLAN_RESPONSE, indent=2),
    "codegen": json.dumps(_CODEGEN_RESPONSE, indent=2),
    "testgen": json.dumps(_TESTGEN_RESPONSE, indent=2),
}
