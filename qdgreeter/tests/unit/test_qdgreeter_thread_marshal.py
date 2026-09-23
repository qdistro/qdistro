"""Worker-thread marshaling tests for GreetController (finding #17).

The greetd auth flow runs in a raw Python worker thread
(``threading.Thread`` -> ``asyncio.run(_auth_flow(...))``). The
QML-bound controller lives on the GUI thread, so *every* result the
worker produces — status text, the busy flag, and the
``succeeded`` / ``failed`` signals — must be delivered back on the GUI
thread, never mutated from the worker.

These tests drive the controller through its real ``submit()`` entry
point (which spawns the worker thread) and assert:

  * the property storage is only ever written from the GUI thread, and
  * ``succeeded`` / ``failed`` are emitted on the GUI thread, and
  * the busy flag is toggled true (sync) then false (queued) by the
    time the GUI event loop has drained.

Before the fix the worker thread called ``_set_status`` /
``_set_busy`` / ``succeeded.emit`` / ``failed.emit`` directly, so the
recorded mutation thread would be the ``qdgreeter-auth`` worker rather
than the GUI thread and ``_thread_violations`` would be non-empty.
"""

from __future__ import annotations

import os
import sys
import threading

import pytest

_HEADLESS = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
if _HEADLESS:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


PyQt6 = pytest.importorskip("PyQt6", reason="PyQt6 not installed")
from PyQt6.QtCore import QCoreApplication, QThread  # noqa: E402
from qdgreeter.controller import GreetController  # noqa: E402


def test_binding_is_pyqt6():
    """Guard against PySide6 silently shadowing PyQt6."""
    assert "PySide6" not in sys.modules
    from PyQt6.QtCore import PYQT_VERSION_STR  # noqa: F401


class _AsyncFakeClient:
    """Scripted greetd client whose awaits actually yield to the loop,
    so the worker thread is genuinely off the GUI thread while the flow
    runs (a purely synchronous fake could complete before the thread
    even starts)."""

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


class _ThreadTrackingController(GreetController):
    """Records the thread that performed each state mutation / emit so
    the test can prove they all happened on the GUI thread."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mutation_threads = []
        self._thread_violations = []
        self._gui_thread = QThread.currentThread()
        self.succeeded.connect(self._record_succeeded)
        self.failed.connect(self._record_failed)

    def _note(self, what):
        cur = QThread.currentThread()
        self._mutation_threads.append((what, cur))
        if cur is not self._gui_thread:
            self._thread_violations.append((what, cur))

    def _set_status(self, msg):
        self._note("set_status")
        super()._set_status(msg)

    def _set_busy(self, value):
        self._note(f"set_busy={value}")
        super()._set_busy(value)

    def _record_succeeded(self):
        self._note("succeeded")

    def _record_failed(self):
        self._note("failed")


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


@pytest.mark.cheat_aware(
    protects="all auth-worker results (status/busy/succeeded/failed) are "
    "delivered on the GUI thread, never mutated from the worker thread",
    severity="medium",
    cheats=[
        "call _auth_flow() directly on the test thread so the worker is "
        "never exercised",
        "drop the _thread_violations assertion",
        "stop recording the emitting thread for succeeded/failed",
    ],
    consequence="cross-thread QObject mutation in the boot login path can "
    "drop UI updates or crash under slow PAM/greetd replies",
)
def test_success_results_marshalled_to_gui_thread(qapp):
    client = _AsyncFakeClient(
        [
            {"type": "auth_message", "auth_message_type": "secret",
             "auth_message": "Password:"},
            {"type": "success"},
            {"type": "success"},
        ]
    )
    ctl = _ThreadTrackingController(client=client,
                                   session_cmd=["qdwin-session.target"])
    ctl._current_text = "hunter2"

    ctl.submit()
    # submit() sets busy=True synchronously on the GUI thread.
    assert ctl.busy is True

    ok = _pump_until(qapp, lambda: ("succeeded", ctl._gui_thread)
                     in ctl._mutation_threads)
    assert ok, "succeeded never delivered on the GUI thread"

    # busy must come back down (queued from the worker's finally block).
    assert _pump_until(qapp, lambda: ctl.busy is False), \
        "busy never reset to False"

    # The core invariant: NOTHING ran on the worker thread.
    assert ctl._thread_violations == [], (
        f"cross-thread Qt mutation detected: {ctl._thread_violations!r}"
    )
    # And we actually exercised the worker (sanity: a real round-trip ran).
    assert any(m["type"] == "start_session" for m in client.sent)


def test_failure_results_marshalled_to_gui_thread(qapp):
    client = _AsyncFakeClient(
        [
            {"type": "auth_message", "auth_message_type": "secret",
             "auth_message": "Password:"},
            {"type": "error", "error_type": "auth_error",
             "description": "incorrect password"},
            {"type": "success"},  # cancel_session reply
        ]
    )
    ctl = _ThreadTrackingController(client=client)
    ctl._current_text = "wrong"

    ctl.submit()

    ok = _pump_until(qapp, lambda: ("failed", ctl._gui_thread)
                     in ctl._mutation_threads)
    assert ok, "failed never delivered on the GUI thread"
    assert _pump_until(qapp, lambda: ctl.busy is False)

    # status was set off the GUI thread originally; must now be on it.
    assert ctl._thread_violations == [], (
        f"cross-thread Qt mutation detected: {ctl._thread_violations!r}"
    )
    assert ctl.statusMessage == "incorrect password"


def test_no_lingering_auth_worker_after_completion(qapp):
    """The worker thread must exit; its only remaining job is to hand
    results back to the GUI thread, not to keep touching the QObject."""
    client = _AsyncFakeClient([{"type": "success"}, {"type": "success"}])
    ctl = _ThreadTrackingController(client=client)
    ctl._current_text = ""
    ctl.submit()
    _pump_until(qapp, lambda: ctl.busy is False)

    # Give the daemon worker a beat to unwind, then assert it's gone.
    def _no_worker():
        return not any(t.name == "qdgreeter-auth" and t.is_alive()
                       for t in threading.enumerate())
    assert _pump_until(qapp, _no_worker), "auth worker thread did not exit"
    assert ctl._thread_violations == []
