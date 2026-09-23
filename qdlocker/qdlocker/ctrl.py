"""Test ctrl-socket — `/run/user/<uid>/qdlocker.sock`.

Line-protocol UNIX socket for the VM GUI harness. Always returns
LIVE state, never bootstrap-time defaults: the `locked` field reads
`bridge.locked` which is updated on every `locked_changed` event
from the compositor.

Commands (one per connection, newline-terminated):
  lock                forces lock; equivalent to a `lock_requested`
                       arriving from the compositor. ALWAYS available —
                       it can only raise the lock state and leaks nothing,
                       so production (qdshell's lock button / session menu /
                       IPC) relies on it.
  status              `locked=<bool> prompt-len=<n> pam-ready=<bool>
                       unlock-in-progress=<bool>`
  unlock-result       `last=<success|failed|none>`
  prompt-text         masked prompt buffer (`*` per char + length);
                       never returns plaintext.
  indicators          one-line live state of the lock-surface capture /
                       egress indicators (J28), as seen by the RUNNING
                       observer — so the GUI gate can assert its lock-edge
                       rescan, timeout and freshness behaviour instead of
                       re-deriving from a separate process.

Finding 02: `status`, `unlock-result`, `prompt-text` and `indicators` are introspection
commands — they expose live lock state and, via prompt-text/prompt-len, a
password-LENGTH side channel; `indicators` additionally discloses whether a
capture is live. They exist only for the GUI test harness and are
served ONLY when introspection is enabled (constructor `introspection=True`).
app.py authorizes that solely via a root-owned marker
(`/etc/qdistro/locker-ctrl-introspection`) so a same-uid process cannot forge
it. In production they return `error: command unavailable`.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSlot

from .auth import AuthOutcome
from .controller import LockController

log = logging.getLogger("qdlocker.ctrl")

# How many bytes we'll read per command. The protocol is one line of
# ASCII; anything longer is malformed.
MAX_COMMAND_LEN = 1024

# Linux `struct ucred` = three native ints (pid, uid, gid). This is the
# payload returned by SO_PEERCRED on an AF_UNIX SOCK_STREAM socket.
_UCRED_FMT = "iii"
_UCRED_SIZE = struct.calcsize(_UCRED_FMT)


def peer_uid(conn: socket.socket) -> int | None:
    """Return the connecting peer's effective uid via SO_PEERCRED.

    Returns ``None`` if the credentials can't be read (no SO_PEERCRED
    support, short read, or any OSError) so callers can fail closed.
    """
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, so_peercred, _UCRED_SIZE)
    except OSError:
        return None
    if len(raw) != _UCRED_SIZE:
        return None
    _pid, uid, _gid = struct.unpack(_UCRED_FMT, raw)
    return uid


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "qdlocker.sock"


class CtrlState:
    def __init__(self, controller: LockController, locked: bool) -> None:
        self._lock = threading.Lock()
        self._locked = locked
        self._prompt_len = len(controller.currentText)
        self._pam_ready = controller.pamReady
        self._unlock_in_progress = controller.unlockInProgress
        self._last_outcome: AuthOutcome | None = None
        # J28 indicator snapshot, refreshed from the observer's `changed`
        # signal. Defaults to a FAILED reading, not an empty one: a harness
        # that reads this before the observer has published anything must not
        # see something that looks like a healthy quiet machine.
        self._indicators = "capture_observer=failed egress_observer=failed"

    def set_locked(self, value: bool) -> None:
        with self._lock:
            self._locked = value

    def set_prompt_len(self, value: int) -> None:
        with self._lock:
            self._prompt_len = value

    def set_pam_ready(self, value: bool) -> None:
        with self._lock:
            self._pam_ready = value

    def set_unlock_in_progress(self, value: bool) -> None:
        with self._lock:
            self._unlock_in_progress = value

    def set_last_outcome(self, value: AuthOutcome) -> None:
        with self._lock:
            self._last_outcome = value

    def set_indicators(self, value: str) -> None:
        with self._lock:
            self._indicators = value

    def indicators(self) -> str:
        with self._lock:
            return self._indicators

    def status(self) -> str:
        with self._lock:
            return (
                f"locked={self._locked} "
                f"prompt-len={self._prompt_len} "
                f"pam-ready={self._pam_ready} "
                f"unlock-in-progress={self._unlock_in_progress}"
            )

    def unlock_result(self) -> str:
        with self._lock:
            last = self._last_outcome.value if self._last_outcome else "none"
        return f"last={last}"

    def prompt_text(self) -> str:
        with self._lock:
            n = self._prompt_len
        return f"masked={'*' * n} len={n}"


class CtrlSocket(QObject):
    def __init__(
        self,
        controller: LockController,
        bridge,
        path: Path | None = None,
        parent: QObject | None = None,
        introspection: bool = False,
        indicators=None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._bridge = bridge
        # Finding 02: introspection commands (status, unlock-result,
        # prompt-text) expose live lock state and a password-LENGTH side
        # channel (prompt-text). They exist only for the GUI test harness and
        # are gated OFF by default; production keeps only the `lock` command,
        # which can merely raise the lock state and leaks nothing.
        self._introspection = introspection
        self._path = path or default_socket_path()
        self._state = CtrlState(controller, bridge.locked)
        self._stop = threading.Event()
        controller.unlocked.connect(self._on_unlocked)
        controller.failed.connect(self._on_failed)
        controller._currentTextChanged.connect(self._on_current_text_changed)
        controller._pamReadyChanged.connect(self._on_pam_ready_changed)
        controller._unlockInProgressChanged.connect(self._on_unlock_in_progress_changed)
        if hasattr(bridge, "lockedChangedForCtrl"):
            bridge.lockedChangedForCtrl.connect(self._on_locked_changed)
        self._indicators = indicators
        if indicators is not None:
            indicators.changed.connect(self._on_indicators_changed)
            self._on_indicators_changed()

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
        self._thread = threading.Thread(
            target=self._serve, name="qdlocker-ctrl", daemon=True
        )
        self._thread.start()
        log.info("ctrl socket at %s", self._path)

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("could not unlink %s", self._path)

    @pyqtSlot()
    def _on_unlocked(self) -> None:
        self._state.set_last_outcome(AuthOutcome.SUCCESS)

    @pyqtSlot()
    def _on_failed(self) -> None:
        self._state.set_last_outcome(AuthOutcome.FAILED)

    @pyqtSlot()
    def _on_current_text_changed(self) -> None:
        self._state.set_prompt_len(len(self._controller.currentText))

    @pyqtSlot()
    def _on_pam_ready_changed(self) -> None:
        self._state.set_pam_ready(self._controller.pamReady)

    @pyqtSlot()
    def _on_unlock_in_progress_changed(self) -> None:
        self._state.set_unlock_in_progress(self._controller.unlockInProgress)

    @pyqtSlot(bool)
    def _on_locked_changed(self, locked: bool) -> None:
        self._state.set_locked(locked)

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stop.is_set():
                    log.exception("accept failed")
                return
            if not self._authorize_peer(conn):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            self._handle_connection(conn)

    def _authorize_peer(self, conn: socket.socket) -> bool:
        """Peer-credential policy: serve ONLY the session owner.

        The ctrl socket exposes live locker state, a length-revealing
        masked prompt buffer (keystroke timing/length side channel) and
        a synthetic lock injection. The socket already lives in the
        0o700 XDG_RUNTIME_DIR and is itself 0o600, but those only bound
        access to the same uid as a directory/file ACL. We additionally
        verify the connecting peer's uid via SO_PEERCRED and accept only
        connections whose uid matches our own (the session owner). Fail
        closed: if the credentials can't be read at all, refuse.
        """
        uid = peer_uid(conn)
        if uid is None:
            log.warning("ctrl: refusing connection with unreadable peer credentials")
            return False
        own = os.getuid()
        if uid != own:
            log.warning(
                "ctrl: refusing connection from foreign uid %d (expected %d)",
                uid,
                own,
            )
            return False
        return True

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

    def _on_indicators_changed(self) -> None:
        if self._indicators is None:
            return
        try:
            self._state.set_indicators(self._indicators.snapshot_line())
        except Exception:  # never let an introspection readout kill the locker
            log.exception("indicator snapshot failed")

    def _handle(self, line: str) -> str:
        if not line:
            return "error: empty command"
        cmd, _, _rest = line.partition(" ")
        if cmd == "lock":
            # 3 = manual per qdwin-locker-v1.xml. Routed through the
            # bridge so it goes through the same QueuedConnection
            # path as a real compositor event. Always available — it can
            # only raise the lock state and discloses nothing (finding 02).
            self._bridge.inject_lock_requested(3)
            return "ok"
        # Finding 02: the remaining commands are introspection/diagnostics and
        # are only served when explicitly enabled (introspection=True, which
        # app.py authorizes via a root-owned marker for the GUI test harness).
        # In production they are unavailable, so the prompt-length side channel
        # and live-state readout do not exist.
        if cmd in ("status", "unlock-result", "prompt-text", "indicators"):
            if not self._introspection:
                return "error: command unavailable"
            if cmd == "status":
                return self._state.status()
            if cmd == "unlock-result":
                return self._state.unlock_result()
            if cmd == "indicators":
                return self._state.indicators()
            # prompt-text: never return plaintext — only a length-revealing
            # mask. Scenario 05 asserts on this exact form.
            return self._state.prompt_text()
        return f"error: unknown command '{cmd}'"
