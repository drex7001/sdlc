"""GET /status view: liveness + uptime, rate-limited per IP."""

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
