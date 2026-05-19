"""In-memory fixed-window rate limiter."""

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
