"""Unit tests for the ctrl-socket introspection gate (finding 02).

The control socket keeps the `lock` command available in production (qdshell's
lock button / session menu / IPC depend on it; it can only raise the lock state
and leaks nothing). The introspection commands — `status`, `unlock-result` and
`prompt-text` (a password-LENGTH side channel) — are served ONLY when
introspection is explicitly enabled (constructor `introspection=True`, which
app.py authorizes only via a root-owned marker for the GUI test harness). In
production they are unavailable.
"""

from __future__ import annotations

import os
import socket
import sys
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from qdlocker.controller import LockController
from qdlocker.ctrl import CtrlSocket


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    yield app


@pytest.fixture
def auth():
    class StubAuth(QObject):
        ready = pyqtSignal()
        message = pyqtSignal(str, bool, bool)
        outcome = pyqtSignal(object)

        def __init__(self):
            super().__init__()
            self._gen = 0
            self.probe_pam = MagicMock(side_effect=lambda: self.ready.emit())
            self.start_pam = MagicMock()
            self.abort_pam = MagicMock()
            self.respond_pam = MagicMock()
            self.occupy_fingerprint_sensor = MagicMock()

        def reset_session(self):
            self._gen += 1

        def _current_generation(self):
            return self._gen

    return StubAuth()


class StubBridge(QObject):
    lockedChangedForCtrl = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.locked = False
        self.injected = []

    def inject_lock_requested(self, reason: int) -> None:
        self.injected.append(reason)


def _request(path, command: str, timeout: float = 5.0) -> str | None:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    try:
        c.connect(str(path))
        c.sendall((command + "\n").encode())
        return c.recv(4096).decode().strip()
    finally:
        c.close()


def _make(qapp, auth, tmp_path, *, introspection):
    controller = LockController(auth)
    bridge = StubBridge()
    sock = CtrlSocket(controller, bridge, path=tmp_path / "qdlocker.sock",
                      introspection=introspection)
    return sock, bridge


@pytest.mark.cheat_aware(
    protects="the ctrl socket's introspection commands (status, unlock-result, "
    "prompt-text — the password-length side channel — and indicators, "
    "which discloses whether a capture is live) are NOT served in "
    "production; only the leak-free `lock` command is",
    severity="low-medium",
    cheats=[
        "default introspection=True so production exposes prompt-text",
        "let `prompt-text`/`status` fall through to the live readout when the "
        "flag is off",
        "gate `lock` behind the flag too (would break qdshell's lock button)",
    ],
    consequence="a same-uid process reads the live password length / lock "
    "state from a production locker",
)
def test_production_socket_is_lock_only(qapp, auth, tmp_path):
    sock, bridge = _make(qapp, auth, tmp_path, introspection=False)
    try:
        # `lock` works and actually injects a manual (reason=3) lock.
        assert _request(sock._path, "lock") == "ok"
        assert bridge.injected == [3]
        # Introspection commands are refused — no live state, no length channel.
        for cmd in ("status", "unlock-result", "prompt-text", "indicators"):
            assert _request(sock._path, cmd) == "error: command unavailable", (
                f"{cmd} must be unavailable without introspection"
            )
    finally:
        sock.close()


def test_introspection_socket_serves_diagnostics(qapp, auth, tmp_path):
    sock, bridge = _make(qapp, auth, tmp_path, introspection=True)
    try:
        assert _request(sock._path, "lock") == "ok"
        status = _request(sock._path, "status")
        assert status is not None and "locked=" in status
        assert _request(sock._path, "unlock-result").startswith("last=")
        # prompt-text returns the masked buffer form, never plaintext.
        assert _request(sock._path, "prompt-text") is not None
        # J28: with no observer attached the snapshot must read FAILED, not
        # empty — a harness reading it early must not see something that
        # looks like a healthy quiet machine.
        ind = _request(sock._path, "indicators")
        assert ind is not None and "capture_observer=failed" in ind
    finally:
        sock.close()
