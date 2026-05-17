"""Tests for qdistro_admin_ratelimit.RateLimiter.

Uses an injected clock so tests are deterministic — real time is the
one thing a rate-limit test cannot afford to depend on.
"""
from __future__ import annotations

import pytest

from qdistro_admin_ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock():
    return FakeClock()


def test_allows_up_to_limit(clock):
    rl = RateLimiter(limit=3, window_s=1.0, now=clock)
    assert rl.check(2000, "act") is True
    assert rl.check(2000, "act") is True
    assert rl.check(2000, "act") is True


def test_rejects_over_limit(clock):
    rl = RateLimiter(limit=3, window_s=1.0, now=clock)
    for _ in range(3):
        rl.check(2000, "act")
    assert rl.check(2000, "act") is False


def test_rejections_do_not_extend_window(clock):
    """A rejected call must not re-enter the bucket — otherwise a flood
    keeps the bucket full forever and the caller never recovers."""
    rl = RateLimiter(limit=2, window_s=1.0, now=clock)
    rl.check(2000, "act"); rl.check(2000, "act")
    # Floods for most of the window
    for _ in range(100):
        clock.advance(0.005)
        assert rl.check(2000, "act") is False
    # Advance past the window from the first recorded call; caller
    # should be able to make two more.
    clock.t = 1.01
    assert rl.check(2000, "act") is True
    assert rl.check(2000, "act") is True
    assert rl.check(2000, "act") is False


def test_sliding_window_recovers(clock):
    rl = RateLimiter(limit=2, window_s=1.0, now=clock)
    rl.check(2000, "act")
    clock.advance(0.5)
    rl.check(2000, "act")
    assert rl.check(2000, "act") is False
    clock.advance(0.51)  # first entry now outside window
    assert rl.check(2000, "act") is True


def test_buckets_are_per_uid(clock):
    rl = RateLimiter(limit=1, window_s=1.0, now=clock)
    assert rl.check(2000, "act") is True
    assert rl.check(2000, "act") is False
    # A different uid is independent.
    assert rl.check(3000, "act") is True


def test_buckets_are_per_action(clock):
    rl = RateLimiter(limit=1, window_s=1.0, now=clock)
    assert rl.check(2000, "act.a") is True
    assert rl.check(2000, "act.a") is False
    # A different action is independent.
    assert rl.check(2000, "act.b") is True


def test_rejects_invalid_config():
    with pytest.raises(ValueError):
        RateLimiter(limit=0)
    with pytest.raises(ValueError):
        RateLimiter(window_s=0)
    with pytest.raises(ValueError):
        RateLimiter(window_s=-1)


def test_exposes_knobs(clock):
    rl = RateLimiter(limit=5, window_s=2.5, now=clock)
    assert rl.limit == 5
    assert rl.window_s == 2.5
