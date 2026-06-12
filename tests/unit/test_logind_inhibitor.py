"""Unit tests for the logind sleep delay-inhibitor in LogindWatcher.

These tests drive the inhibitor coroutines directly (no real D-Bus /
dbus-next): a fake Manager hands out inhibitor "fds" via call_inhibit,
and os.close is patched to record releases. The goal is to pin the
core invariant the todo asks for:

    on PrepareForSleep(start=True) the locker requests the lock and
    releases the sleep delay inhibitor ONLY AFTER the lock is confirmed
    (or a bounded timeout elapses) — never before.

so the system does not suspend with pre-lock content still on screen.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from qdlocker.logind import REASON_SUSPEND, LogindWatcher

from qdlocker import logind


class FakeManager:
    """Minimal stand-in for the login1.Manager proxy. Hands out a fresh
    integer fd per Inhibit call and records the arguments."""

    def __init__(self) -> None:
        self.inhibit_calls: list[tuple] = []
        self._next_fd = 1000

    async def call_inhibit(self, what, who, why, mode):  # noqa: ANN001
        self.inhibit_calls.append((what, who, why, mode))
        fd = self._next_fd
        self._next_fd += 1
        return fd


@pytest.fixture
def closed_fds(monkeypatch):
    """Record os.close() targets instead of touching real fds (the fake
    'fds' are not real, so a real close would EBADF)."""
    recorded: list[int] = []

    def _fake_close(fd):  # noqa: ANN001
        recorded.append(fd)

    monkeypatch.setattr(logind.os, "close", _fake_close)
    return recorded


def _make_watcher(on_lock):
    w = LogindWatcher(on_lock=on_lock)
    return w


def test_inhibitor_acquired_with_delay_mode(closed_fds):
    """Startup acquires a *sleep* inhibitor in *delay* mode (not block):
    delay mode lets logind force the transition after InhibitDelayMaxSec,
    so a stuck locker can never wedge suspend forever."""
    async def run():
        w = _make_watcher(on_lock=lambda r: None)
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        assert w._inhibit_fd == 1000
        assert w._mgr.inhibit_calls == [
            ("sleep", "qdlocker",
             "Lock the screen before the system sleeps", "delay")
        ]

    asyncio.run(run())


def test_release_only_after_confirmation(closed_fds):
    """The inhibitor fd must NOT be released until the lock is confirmed.

    We start the suspend coroutine, assert the fd is still held while
    confirmation is pending, then confirm and assert it is released.
    """
    locks: list[int] = []

    async def run():
        w = _make_watcher(on_lock=lambda r: locks.append(r))
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        held_fd = w._inhibit_fd
        assert held_fd is not None

        task = asyncio.ensure_future(w._on_prepare_for_sleep_async())
        # Let the coroutine run up to its await on confirmation.
        await asyncio.sleep(0)
        # Lock must have been requested with the suspend reason...
        assert locks == [REASON_SUSPEND]
        # ...but the inhibitor is still held (not yet released).
        assert w._inhibit_fd == held_fd
        assert closed_fds == []

        # Now confirm the lock (as the compositor's locked_changed=1 path
        # would via notify_lock_confirmed()).
        w.notify_lock_confirmed()
        await task

        # fd released exactly once, after confirmation.
        assert w._inhibit_fd is None
        assert closed_fds == [held_fd]

    asyncio.run(run())


def test_release_on_timeout_is_failopen(closed_fds, monkeypatch):
    """If confirmation never arrives, the inhibitor is released after the
    bounded timeout so suspend is never wedged. Lock is still requested."""
    monkeypatch.setattr(logind, "_LOCK_CONFIRM_TIMEOUT_S", 0.05)
    locks: list[int] = []

    async def run():
        w = _make_watcher(on_lock=lambda r: locks.append(r))
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        held_fd = w._inhibit_fd

        # Never confirm — let the timeout fire.
        await w._on_prepare_for_sleep_async()

        assert locks == [REASON_SUSPEND]
        assert w._inhibit_fd is None
        assert closed_fds == [held_fd]

    asyncio.run(run())


def test_resume_reacquires_inhibitor(closed_fds):
    """After a sleep/resume cycle the inhibitor must be re-acquired so the
    NEXT suspend is also guarded."""
    async def run():
        w = _make_watcher(on_lock=lambda r: None)
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        first_fd = w._inhibit_fd

        # Suspend: confirm immediately so it releases.
        task = asyncio.ensure_future(w._on_prepare_for_sleep_async())
        await asyncio.sleep(0)
        w.notify_lock_confirmed()
        await task
        assert w._inhibit_fd is None

        # Resume re-acquires a fresh fd.
        await w._on_resume_async()
        assert w._inhibit_fd is not None
        assert w._inhibit_fd != first_fd

    asyncio.run(run())


def test_acquire_failure_is_failopen(closed_fds):
    """If Inhibit raises, we keep no fd and suspend proceeds uninhibited;
    the lock is still requested on the suspend trigger."""
    locks: list[int] = []

    class FailingManager:
        async def call_inhibit(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("logind said no")

    async def run():
        w = _make_watcher(on_lock=lambda r: locks.append(r))
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FailingManager()
        await w._acquire_inhibitor()
        assert w._inhibit_fd is None

        # Suspend still requests the lock; no fd to release/close.
        await w._on_prepare_for_sleep_async()
        assert locks == [REASON_SUSPEND]
        assert closed_fds == []

    asyncio.run(run())


def test_no_double_acquire(closed_fds):
    """Acquiring twice in a row must not leak a second fd."""
    async def run():
        w = _make_watcher(on_lock=lambda r: None)
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        first = w._inhibit_fd
        await w._acquire_inhibitor()  # no-op
        assert w._inhibit_fd == first
        assert len(w._mgr.inhibit_calls) == 1

    asyncio.run(run())


def test_notify_before_loop_is_noop():
    """notify_lock_confirmed() must be safe before the asyncio loop is up
    (e.g. an early compositor locked_changed during startup)."""
    w = LogindWatcher(on_lock=lambda r: None)
    # No loop / event yet — must not raise.
    w.notify_lock_confirmed()
