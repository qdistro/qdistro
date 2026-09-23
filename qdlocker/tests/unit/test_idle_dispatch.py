"""Thin IdleWatcher callback-dispatch tests (no Wayland required).

`_handle_idled` / `_handle_resumed` run on the pywayland poll thread and
invoke the user-supplied callbacks. A raised callback MUST be caught and
logged so it cannot tear down the poll loop and silently disable all
future idle events. These tests poke the dispatcher entry points
directly — they do not touch a wl_display.
"""

from __future__ import annotations

import pytest
from qdlocker.idle import IdleWatcher


def test_idled_invokes_callback():
    w = IdleWatcher(timeout_ms=1000)
    calls = []
    w.on_idle(lambda: calls.append("idle"))
    w._handle_idled()
    assert calls == ["idle"]


def test_resumed_invokes_callback():
    w = IdleWatcher(timeout_ms=1000)
    calls = []
    w.on_resume(lambda: calls.append("resume"))
    w._handle_resumed()
    assert calls == ["resume"]


def test_idled_without_callback_is_noop():
    w = IdleWatcher(timeout_ms=1000)
    w._handle_idled()  # must not raise even with no callback set


def test_idled_swallows_callback_exception():
    w = IdleWatcher(timeout_ms=1000)

    def boom():
        raise RuntimeError("callback blew up")

    w.on_idle(boom)
    # Must NOT propagate — a raised callback would kill the poll loop.
    w._handle_idled()


def test_resumed_swallows_callback_exception():
    w = IdleWatcher(timeout_ms=1000)
    w.on_resume(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    w._handle_resumed()


def test_constructor_rejects_nonpositive_timeout():
    with pytest.raises(ValueError):
        IdleWatcher(timeout_ms=0)
    with pytest.raises(ValueError):
        IdleWatcher(timeout_ms=-1)
