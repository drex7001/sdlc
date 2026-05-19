"""Unit tests for FixedWindowRateLimiter."""

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
