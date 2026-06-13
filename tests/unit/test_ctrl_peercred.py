"""Unit tests for the ctrl-socket peer-credential policy.

The `/run/user/<uid>/qdlocker.sock` control socket exposes live locker
state, a length-revealing masked prompt buffer (a keystroke
timing/length side channel) and a synthetic lock injection. It must
serve ONLY the session owner: connections are gated on SO_PEERCRED and
accepted only when the peer uid matches our own. The gate fails closed —
unreadable credentials are refused.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from qdlocker import ctrl as ctrl_mod
from qdlocker.controller import LockController
from qdlocker.ctrl import CtrlSocket, peer_uid


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


@pytest.fixture
def ctrl_socket(qapp, auth, tmp_path):
    controller = LockController(auth)
    bridge = StubBridge()
    # introspection=True so the peercred tests can probe with `status`; this
    # fixture exercises the SO_PEERCRED gate, not the finding-02 command gating
    # (which test_ctrl_introspection.py covers).
    sock = CtrlSocket(controller, bridge, path=tmp_path / "qdlocker.sock",
                      introspection=True)
    yield sock, bridge
    sock.close()


def _request(path, command: str, timeout: float = 5.0) -> str | None:
    """Connect, send one command, return the reply, or None if the
    server closed the connection without replying (i.e. refused)."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout)
    try:
        c.connect(str(path))
        try:
            c.sendall((command + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = c.recv(1024)
                if not chunk:
                    break
                data += chunk
        except (ConnectionResetError, BrokenPipeError):
            # Server refused us and dropped the connection — treated the
            # same as a clean empty read: no reply was served.
            return None
        return data.decode().strip() if data else None
    finally:
        c.close()


# --- peer_uid helper -------------------------------------------------------


def test_peer_uid_reads_own_uid(ctrl_socket):
    sock, _bridge = ctrl_socket
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(5.0)
    c.connect(str(sock._path))
    try:
        # The server side sees us; but we can also read our own peer's
        # uid from the client side (symmetric for a same-uid pair).
        assert peer_uid(c) == os.getuid()
    finally:
        c.close()


def test_peer_uid_fails_closed_when_unreadable(monkeypatch):
    """If SO_PEERCRED can't be read, peer_uid returns None (fail closed)."""

    class FakeConn:
        def getsockopt(self, *a, **k):
            raise OSError("nope")

    assert peer_uid(FakeConn()) is None


def test_peer_uid_short_read_is_none(monkeypatch):
    class FakeConn:
        def getsockopt(self, *a, **k):
            return b"\x00"  # too short for struct ucred

    assert peer_uid(FakeConn()) is None


# --- _authorize_peer gate --------------------------------------------------


def test_authorize_accepts_own_uid(ctrl_socket, monkeypatch):
    sock, _bridge = ctrl_socket
    monkeypatch.setattr(ctrl_mod, "peer_uid", lambda conn: os.getuid())
    assert sock._authorize_peer(object()) is True


def test_authorize_rejects_foreign_uid(ctrl_socket, monkeypatch):
    sock, _bridge = ctrl_socket
    foreign = os.getuid() + 12345
    monkeypatch.setattr(ctrl_mod, "peer_uid", lambda conn: foreign)
    assert sock._authorize_peer(object()) is False


def test_authorize_rejects_unreadable_creds(ctrl_socket, monkeypatch):
    sock, _bridge = ctrl_socket
    monkeypatch.setattr(ctrl_mod, "peer_uid", lambda conn: None)
    assert sock._authorize_peer(object()) is False


# --- end-to-end over the real socket --------------------------------------


def test_same_uid_peer_is_served(ctrl_socket):
    """The legitimate same-uid caller (the GUI harness) still works."""
    sock, _bridge = ctrl_socket
    reply = _request(sock._path, "status")
    assert reply is not None
    assert reply.startswith("locked=")


@pytest.mark.cheat_aware(
    protects="qdlocker ctrl-socket serves only the session owner; a "
    "cross-uid peer is refused before any reply (no lock-state or "
    "prompt-length side-channel leaks to another user)",
    severity="critical",
    cheats=[
        "make peer_uid return os.getuid() so the foreign peer is accepted",
        "assert on the connection succeeding instead of reply is None",
        "loosen the uid match to a range/group instead of exact equality",
        "let the gate fail OPEN when SO_PEERCRED is unreadable",
    ],
    consequence="another local user reads live locker state and the "
    "length-revealing masked prompt buffer (a keystroke timing/length "
    "side channel) off the screen locker's control socket",
)
def test_foreign_uid_peer_is_rejected(ctrl_socket, monkeypatch):
    """A simulated cross-uid peer is refused: the server closes the
    connection without serving any reply."""
    sock, _bridge = ctrl_socket
    foreign = os.getuid() + 12345
    monkeypatch.setattr(ctrl_mod, "peer_uid", lambda conn: foreign)
    reply = _request(sock._path, "status", timeout=3.0)
    assert reply is None  # refused before any data was served


@pytest.mark.cheat_aware(
    protects="a peer refused by the SO_PEERCRED gate cannot drive the "
    "locker via the synthetic lock-injection command",
    severity="critical",
    cheats=[
        "process the command before the authorize gate runs",
        "drop or shorten the time.sleep so the unhandled inject is missed",
        "assert len(bridge.injected) >= 0 instead of == []",
        "monkeypatch peer_uid to our own uid so the peer is authorized",
    ],
    consequence="a foreign local process forces lock-state transitions on "
    "the locker control surface, e.g. to grief or to probe the prompt "
    "side channel",
)
def test_foreign_uid_cannot_inject_lock(ctrl_socket, monkeypatch):
    """A refused peer can't drive the locker via the synthetic lock."""
    sock, bridge = ctrl_socket
    foreign = os.getuid() + 12345
    monkeypatch.setattr(ctrl_mod, "peer_uid", lambda conn: foreign)
    _request(sock._path, "lock", timeout=3.0)
    # Give the serve thread a moment to (not) handle it.
    time.sleep(0.2)
    assert bridge.injected == []
