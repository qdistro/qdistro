"""Unit tests for GreetController state hygiene.

Focus: the typed password must not linger in the controller's bound
``currentText`` property after a login attempt finishes on any failure
or exception path. The success path quits the app, so a lingering
secret never matters there; clearing is harmless either way.

submit() spawns a real ``qdgreeter-auth`` worker thread that runs the
asyncio auth flow, then marshals its results back onto the GUI thread
via QMetaObject.invokeMethod(..., QueuedConnection). These tests drive
the real submit() entry point (matching test_qdgreeter_thread_marshal)
and pump the GUI event loop until the worker's queued resets land —
no arbitrary sleeps.
"""

from __future__ import annotations

import os
import sys

import pytest

_HEADLESS = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
if _HEADLESS:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


PyQt6 = pytest.importorskip("PyQt6", reason="PyQt6 not installed")
from PyQt6.QtCore import QCoreApplication, QThread  # noqa: E402
from qdgreeter.controller import GreetController  # noqa: E402


class _AsyncFakeClient:
    """Scripted greetd client whose awaits actually yield to the loop,
    so the worker thread is genuinely off the GUI thread while the flow
    runs (mirrors test_qdgreeter_thread_marshal._AsyncFakeClient)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.sent = []
        self._connected = False
        self.closed = False

    @property
    def connected(self):
        return self._connected

    async def connect(self):
        import asyncio
        await asyncio.sleep(0)
        self._connected = True

    async def close(self):
        self._connected = False
        self.closed = True

    async def create_session(self, username):
        import asyncio
        await asyncio.sleep(0)
        self.sent.append({"type": "create_session", "username": username})
        return self._replies.pop(0)

    async def post_auth(self, response):
        import asyncio
        await asyncio.sleep(0)
        self.sent.append({"type": "post_auth_message_response",
                          "response": response})
        return self._replies.pop(0)

    async def start_session(self, cmd, env=None):
        import asyncio
        await asyncio.sleep(0)
        self.sent.append({"type": "start_session", "cmd": cmd})
        return self._replies.pop(0)

    async def cancel_session(self):
        self.sent.append({"type": "cancel_session"})
        return self._replies.pop(0) if self._replies else {"type": "success"}


class _RaisingClient:
    """greetd client whose create_session raises mid-flow (timeout) to
    exercise the controller's exception path."""

    def __init__(self, exc):
        self._exc = exc
        self._connected = False
        self.closed = False

    @property
    def connected(self):
        return self._connected

    async def connect(self):
        import asyncio
        await asyncio.sleep(0)
        self._connected = True

    async def close(self):
        self._connected = False
        self.closed = True

    async def create_session(self, username):
        import asyncio
        await asyncio.sleep(0)
        raise self._exc

    async def cancel_session(self):
        return {"type": "success"}


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


def _pump_until(qapp, predicate, timeout_ms=4000):
    """Spin the GUI event loop until predicate() or timeout."""
    elapsed = {"t": 0}
    step = 10
    while not predicate() and elapsed["t"] < timeout_ms:
        qapp.processEvents()
        QThread.msleep(step)
        elapsed["t"] += step
    qapp.processEvents()
    return predicate()


def test_failed_auth_clears_current_text(qapp):
    """A secret prompt answered with the wrong password (auth_error)
    must leave currentText empty once the worker finishes."""
    client = _AsyncFakeClient(
        [
            {"type": "auth_message", "auth_message_type": "secret",
             "auth_message": "Password:"},
            {"type": "error", "error_type": "auth_error",
             "description": "incorrect password"},
            {"type": "success"},  # cancel_session reply
        ]
    )
    ctl = GreetController(client=client)
    ctl._current_text = "wrong"
    fails = []
    ctl.failed.connect(lambda: fails.append(True))

    ctl.submit()
    assert _pump_until(qapp, lambda: fails == [True]), "failed never emitted"
    assert _pump_until(qapp, lambda: ctl.busy is False)
    assert ctl.currentText == "", "typed password lingered after failed auth"


def test_exception_path_clears_current_text(qapp):
    """A timeout/exception mid-flow must still clear the typed password."""
    client = _RaisingClient(TimeoutError())
    ctl = GreetController(client=client)
    ctl._current_text = "secret-pw"
    fails = []
    ctl.failed.connect(lambda: fails.append(True))

    ctl.submit()
    assert _pump_until(qapp, lambda: fails == [True]), "failed never emitted"
    assert _pump_until(qapp, lambda: ctl.busy is False)
    assert ctl.currentText == "", "typed password lingered after exception"


def test_success_path_still_emits_succeeded(qapp):
    """Clearing is harmless on the success path; succeeded must fire."""
    client = _AsyncFakeClient(
        [
            {"type": "auth_message", "auth_message_type": "secret",
             "auth_message": "Password:"},
            {"type": "success"},
            {"type": "success"},
        ]
    )
    ctl = GreetController(client=client,
                          session_cmd=["qdwin-session.target"])
    ctl._current_text = "hunter2"
    ok = []
    ctl.succeeded.connect(lambda: ok.append(True))

    ctl.submit()
    assert _pump_until(qapp, lambda: ok == [True]), "succeeded never emitted"
    assert _pump_until(qapp, lambda: ctl.busy is False)
    assert any(m["type"] == "start_session" for m in client.sent)
