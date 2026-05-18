"""Test ctrl-socket — `/run/user/<uid>/qdlocker.sock`.

Line-protocol UNIX socket for the VM GUI harness. Always returns
LIVE state, never bootstrap-time defaults: the `locked` field reads
`bridge.locked` which is updated on every `locked_changed` event
from the compositor.

Commands (one per connection, newline-terminated):
  status              `locked=<bool> prompt-len=<n> pam-ready=<bool>
                       unlock-in-progress=<bool>`
  lock                forces lock; equivalent to a `lock_requested`
                       arriving from the compositor.
  unlock-result       `last=<success|failed|none>`
  prompt-text         masked prompt buffer (`*` per char + length);
                       never returns plaintext.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

from PySide6.QtCore import QObject, QSocketNotifier, Slot

from .auth import AuthOutcome
from .controller import LockController

log = logging.getLogger("qdlocker.ctrl")

# How many bytes we'll read per command. The protocol is one line of
# ASCII; anything longer is malformed.
MAX_COMMAND_LEN = 1024


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "qdlocker.sock"


class CtrlSocket(QObject):
    def __init__(
        self,
        controller: LockController,
        bridge,
        path: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._bridge = bridge
        self._path = path or default_socket_path()
        self._last_outcome: AuthOutcome | None = None
        controller.unlocked.connect(self._on_unlocked)
        controller.failed.connect(self._on_failed)

        # Tighten umask so the bind creates the socket with 0o600
        # regardless of the inherited umask. The chmod afterwards is
        # belt-and-braces.
        old_umask = os.umask(0o077)
        try:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.bind(str(self._path))
            self._sock.listen(8)
            self._sock.setblocking(False)
        finally:
            os.umask(old_umask)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            log.warning("could not chmod %s", self._path)
        self._notifier = QSocketNotifier(
            self._sock.fileno(), QSocketNotifier.Type.Read, self
        )
        self._notifier.activated.connect(self._on_accept)
        log.info("ctrl socket at %s", self._path)

    def close(self) -> None:
        try:
            self._notifier.setEnabled(False)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("could not unlink %s", self._path)

    @Slot()
    def _on_unlocked(self) -> None:
        self._last_outcome = AuthOutcome.SUCCESS

    @Slot()
    def _on_failed(self) -> None:
        self._last_outcome = AuthOutcome.FAILED

    @Slot()
    def _on_accept(self) -> None:
        # Loop to drain the accept backlog — QSocketNotifier is
        # level-triggered but we'd rather avoid relying on a second
        # wake when two clients arrive simultaneously.
        while True:
            try:
                conn, _ = self._sock.accept()
            except BlockingIOError:
                return
            except OSError:
                log.exception("accept failed")
                return
            self._handle_connection(conn)

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.setblocking(False)
            data = b""
            # Read up to MAX_COMMAND_LEN or a newline. Non-blocking
            # so a misbehaving client can't hang the GUI thread, but
            # we wait briefly between recv attempts via select() so a
            # well-behaved client that pipes "<cmd>\n" through socat
            # has time to land its bytes after connect()-but-before-
            # first-recv. The previous tight 64-iteration spin closed
            # the socket before socat's payload arrived ~3% of runs,
            # producing the s103-locker-idle Test 1 "broken pipe on
            # write" symptom. Total ceiling: 64 * 50ms = 3.2 s.
            import select as _select
            for _ in range(64):
                try:
                    chunk = conn.recv(MAX_COMMAND_LEN - len(data))
                except BlockingIOError:
                    chunk = None
                except OSError:
                    return
                if chunk:
                    data += chunk
                    if b"\n" in data or len(data) >= MAX_COMMAND_LEN:
                        break
                    continue
                if chunk == b"":
                    break  # peer closed cleanly
                # BlockingIOError: wait up to 50ms for the client to
                # push its first/next chunk. If nothing arrives across
                # the full 64-iteration budget the client is wedged
                # and we bail with whatever we have (likely "").
                r, _, _ = _select.select([conn], [], [], 0.05)
                if not r and data:
                    break
            line = data.decode("utf-8", errors="replace").strip()
            reply = self._handle(line)
            try:
                conn.sendall((reply + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, line: str) -> str:
        if not line:
            return "error: empty command"
        cmd, _, _rest = line.partition(" ")
        if cmd == "status":
            return (
                f"locked={self._bridge.locked} "
                f"prompt-len={len(self._controller.currentText)} "
                f"pam-ready={self._controller.pamReady} "
                f"unlock-in-progress={self._controller.unlockInProgress}"
            )
        if cmd == "lock":
            # 3 = manual per qdwin-locker-v1.xml. Routed through the
            # bridge so it goes through the same QueuedConnection
            # path as a real compositor event.
            self._bridge.inject_lock_requested(3)
            return "ok"
        if cmd == "unlock-result":
            last = self._last_outcome.value if self._last_outcome else "none"
            return f"last={last}"
        if cmd == "prompt-text":
            # Never return plaintext — only a length-revealing mask.
            # Scenario 05 asserts on this exact form.
            n = len(self._controller.currentText)
            return f"masked={'*' * n} len={n}"
        return f"error: unknown command '{cmd}'"
