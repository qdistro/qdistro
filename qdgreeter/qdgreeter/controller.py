"""GreetController — boot greeter state.

Smaller surface than the locker's controller because greetd handles PAM
and session creation; we only drive the round-trip, surface auth_messages,
and forward errors to the UI.

Auth flow per `greetd-ipc(7)`:

  create_session(admin)
  loop:
    reply is success            → start_session(cmd) → success → succeeded
    reply is auth_message+secret → post_auth(password)
    reply is auth_message+info  → post_auth(None)              (display, continue)
    reply is error+auth_error   → cancel_session → failed (retryable)
    reply is error+error        → cancel_session → failed (fatal)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import stat
import subprocess
import threading

from PyQt6.QtCore import (
    Q_ARG,
    QMetaObject,
    QObject,
    Qt,
    QThread,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)

from .greetd import GreetdClient, GreetdError

log = logging.getLogger("qdgreeter.controller")

# Finding 05: QDGREETER_USER and QDGREETER_SESSION_CMD decide WHO authenticates
# and WHAT runs as that user post-auth (the latter is passed to greetd
# start_session). In production greetd supplies a clean root-owned environment
# and does not set these, so the hardcoded defaults below apply. But a polluted
# environment (a misconfigured greetd, a debug run, an inherited-env mistake)
# could redirect the authenticated identity or the post-auth command. So the env
# overrides are honored ONLY when a ROOT-OWNED marker authorizes them — a
# same-uid/unprivileged process cannot forge it. Mirrors the qdlocker-02 gate.
_FALLBACK_USER = "admin"
_FALLBACK_SESSION_CMD = ["/usr/local/bin/qdwin-session-launcher"]
_ENV_OVERRIDE_MARKER = "/etc/qdistro/greeter-env-override"


def _env_overrides_authorized() -> bool:
    """True only when the root-owned override marker is present and
    trustworthy: a regular file owned by root with no group/world write bits,
    resolved without following a symlink (lstat). Same trust rule the locker
    applies to its system config."""
    try:
        st = os.lstat(_ENV_OVERRIDE_MARKER)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        log.warning("env-override marker %s is not a regular file; refusing",
                    _ENV_OVERRIDE_MARKER)
        return False
    if st.st_uid != 0:
        log.warning("env-override marker %s not owned by root (uid=%d); refusing",
                    _ENV_OVERRIDE_MARKER, st.st_uid)
        return False
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        log.warning("env-override marker %s is group/world-writable; refusing",
                    _ENV_OVERRIDE_MARKER)
        return False
    return True


def _resolve_user() -> str:
    if _env_overrides_authorized():
        # Even an authorized (root-set) override must be non-empty; a blank
        # QDGREETER_USER would otherwise hand greetd an empty username.
        user = os.environ.get("QDGREETER_USER", "").strip()
        if user:
            return user
    return _FALLBACK_USER


def _resolve_session_cmd() -> list[str]:
    # greetd execs this as the authenticated user; its lifetime IS the session
    # lifetime. A bare `systemctl --user start qdwin-session.target` returns 0
    # as soon as the job is enqueued — greetd would see a clean session end
    # milliseconds after auth and recycle back to the greeter. The launcher
    # (deploy/qdwin-session-launcher.sh) does the right `--wait` semantics and
    # surfaces non-zero exits.
    if _env_overrides_authorized():
        raw = os.environ.get("QDGREETER_SESSION_CMD", "")
        try:
            cmd = shlex.split(raw)
        except ValueError:
            # Malformed quoting must not exit the only interactive login at
            # import time; fall back to the safe default.
            log.warning("invalid QDGREETER_SESSION_CMD; using fallback",
                        exc_info=True)
            cmd = []
        if cmd:
            return cmd
    return list(_FALLBACK_SESSION_CMD)


# Resolved once at import: the marker does not change during a single pre-auth
# greeter lifetime. Without the marker (production) these are the safe defaults.
DEFAULT_USER = _resolve_user()
DEFAULT_SESSION_CMD = _resolve_session_cmd()

# Shown when a PAM stack issues a second `secret` prompt within one
# auth exchange (e.g. password + a separate OTP/second-factor
# challenge). The greeter collects a single secret per submit(), so it
# has no answer for the additional challenge. Replaying the first
# secret would (a) almost always fail the second factor and (b) feed
# the typed password into a prompt it was never entered for — a secret
# is replayed into a different challenge. We fail closed instead.
MULTI_SECRET_UNSUPPORTED_MSG = (
    "This account needs more than one secret to log in, which this "
    "greeter can't collect — use a console login (Ctrl+Alt+F2)."
)


class GreetController(QObject):
    succeeded = pyqtSignal()
    failed = pyqtSignal()
    currentTextChanged = pyqtSignal()
    statusMessageChanged = pyqtSignal()
    busyChanged = pyqtSignal()
    usernameChanged = pyqtSignal()

    def __init__(
        self,
        parent: QObject | None = None,
        client: GreetdClient | None = None,
        session_cmd: list[str] | None = None,
        username: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._username = username or DEFAULT_USER
        self._session_cmd = list(session_cmd) if session_cmd else list(DEFAULT_SESSION_CMD)
        self._current_text = ""
        self._status_message = ""
        self._busy = False
        self._client = client or GreetdClient()

    @pyqtProperty(str, notify=usernameChanged)
    def username(self) -> str:
        return self._username

    @pyqtProperty(str, notify=currentTextChanged)
    def currentText(self) -> str:
        return self._current_text

    @currentText.setter  # type: ignore[no-redef]
    def currentText(self, value: str) -> None:
        if value == self._current_text:
            return
        self._current_text = value
        self.currentTextChanged.emit()

    @pyqtProperty(str, notify=statusMessageChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @pyqtProperty(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    # ------------------------------------------------------------------
    # GUI-thread-only state mutators.
    #
    # These are the ONLY places that touch bound Qt property storage or
    # emit the controller's signals. They are pyqtSlots so the auth
    # worker thread can hand work back to the GUI thread via
    # QMetaObject.invokeMethod(..., Qt.QueuedConnection) — see the
    # _post_* marshaling helpers below. Calling them directly is only
    # legal from the thread the controller lives on (the GUI thread).
    # ------------------------------------------------------------------
    @pyqtSlot(str)
    def _set_status(self, msg: str) -> None:
        if msg == self._status_message:
            return
        self._status_message = msg
        self.statusMessageChanged.emit()

    @pyqtSlot(bool)
    def _set_busy(self, value: bool) -> None:
        if value == self._busy:
            return
        self._busy = value
        self.busyChanged.emit()

    @pyqtSlot()
    def _clear_text(self) -> None:
        self.currentText = ""

    @pyqtSlot()
    def _emit_succeeded(self) -> None:
        self.succeeded.emit()

    @pyqtSlot()
    def _emit_failed(self) -> None:
        self.failed.emit()

    # ------------------------------------------------------------------
    # Cross-thread marshaling.
    #
    # The auth worker thread calls these instead of the slots above. If
    # we're already on the controller's (GUI) thread they run inline so
    # synchronous test drivers and the QML auto-connection path keep
    # their existing semantics; otherwise the call is queued onto the
    # GUI thread's event loop so the actual QObject mutation / signal
    # emission happens there, never on the worker thread.
    # ------------------------------------------------------------------
    def _on_gui_thread(self) -> bool:
        return self.thread() is QThread.currentThread()

    def _post_status(self, msg: str) -> None:
        if self._on_gui_thread():
            self._set_status(msg)
        else:
            QMetaObject.invokeMethod(
                self, "_set_status", Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, msg),
            )

    def _post_busy(self, value: bool) -> None:
        if self._on_gui_thread():
            self._set_busy(value)
        else:
            QMetaObject.invokeMethod(
                self, "_set_busy", Qt.ConnectionType.QueuedConnection,
                Q_ARG(bool, value),
            )

    def _post_clear_text(self) -> None:
        if self._on_gui_thread():
            self._clear_text()
        else:
            QMetaObject.invokeMethod(
                self, "_clear_text", Qt.ConnectionType.QueuedConnection,
            )

    def _post_succeeded(self) -> None:
        if self._on_gui_thread():
            self._emit_succeeded()
        else:
            QMetaObject.invokeMethod(
                self, "_emit_succeeded", Qt.ConnectionType.QueuedConnection,
            )

    def _post_failed(self) -> None:
        if self._on_gui_thread():
            self._emit_failed()
        else:
            QMetaObject.invokeMethod(
                self, "_emit_failed", Qt.ConnectionType.QueuedConnection,
            )

    @pyqtSlot()
    def submit(self) -> None:
        """Drive a full greetd round-trip on the current password.

        Runs the asyncio flow in a dedicated worker thread so the Qt
        event loop keeps spinning — otherwise `run_until_complete`
        would block QML signal delivery and any auth_message status
        updates would never paint.
        """
        if self._busy:
            return
        password = self._current_text
        # We're on the GUI thread here, so these mutate directly.
        self._post_busy(True)
        self._post_status("")

        def _run() -> None:
            nonlocal password
            try:
                asyncio.run(self._auth_flow(password))
            finally:
                # Runs on the worker thread: marshal the state resets back
                # onto the GUI thread rather than touching the bound Qt
                # property from here. Clear the typed password on every
                # failure/exception path (the success path quits the app, so
                # a lingering secret never matters there) — this single
                # choke point covers all paths uniformly. Clear the text
                # BEFORE dropping busy so the brief idle state can't paint
                # the stale masked text. Drop the local too, to shorten the
                # plaintext password's lifetime on the worker thread.
                self._post_clear_text()
                self._post_busy(False)
                del password

        threading.Thread(target=_run, name="qdgreeter-auth", daemon=True).start()

    @pyqtSlot(str)
    def appendText(self, text: str) -> None:
        if text:
            self.currentText = self._current_text + text

    @pyqtSlot()
    def backspace(self) -> None:
        self.currentText = self._current_text[:-1]

    @pyqtSlot()
    def clearText(self) -> None:
        self.currentText = ""

    @pyqtSlot(int, result=bool)
    def switchToTty(self, tty: int) -> bool:
        """Switch away from the EGLFS greeter when Ctrl+Alt+Fx is pressed."""
        if tty < 1 or tty > 12:
            log.warning("ignoring invalid tty switch request: tty%s", tty)
            return False
        try:
            result = subprocess.run(
                ["/usr/bin/chvt", str(tty)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            log.exception("failed to execute chvt for tty%s", tty)
            return False
        if result.returncode != 0:
            log.warning("chvt tty%s failed with rc=%s", tty, result.returncode)
            return False
        return True

    async def _auth_flow(self, password: str) -> None:
        try:
            await self._client.connect()
            reply = await self._client.create_session(self._username)
            reply = await self._consume_auth_messages(reply, password)
            if reply.get("type") == "success":
                start = await self._client.start_session(self._session_cmd)
                if start.get("type") == "success":
                    self._post_succeeded()
                    return
                await self._handle_error_reply(start)
            else:
                await self._handle_error_reply(reply)
        except GreetdError as exc:
            self._post_status(exc.description)
            await self._cancel_quiet()
            self._post_failed()
        except TimeoutError:
            log.exception("greetd IPC timed out")
            self._post_status("greetd timed out; retry login")
            self._post_failed()
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            BrokenPipeError,
        ):
            # The socket died mid-flow (typical when greetd closes after
            # an error reply, or restarts under load). The password may
            # well have been correct — labeling this "Authentication
            # failed" misleads the operator. Don't chase the closed
            # socket with cancel_session; just surface the disconnect.
            log.exception("greetd disconnected mid-flow")
            self._post_status("greetd disconnected; retry login")
            self._post_failed()
        except Exception:  # noqa: BLE001
            # Don't include the password or any payload in the log;
            # `exc` is from socket / json layer, password isn't in it.
            # Don't echo `str(exc)` into the UI — it can carry socket
            # paths or reply bytes; keep details in the journal.
            log.exception("greetd flow raised")
            self._post_status("Authentication failed — see journalctl")
            await self._cancel_quiet()
            self._post_failed()
        finally:
            await self._client.close()

    async def _consume_auth_messages(
        self, reply: dict, password: str
    ) -> dict:
        """Walk the auth_message ↔ post_auth_message_response loop.

        The greeter collects exactly one secret per submit(). We answer
        the FIRST `secret` prompt with the user-entered password. If the
        PAM stack issues a SECOND `secret` prompt within the same
        exchange (a multi-prompt stack: password + a separate
        second-factor challenge), we have no answer for it. Replaying the
        first secret would feed the typed password into a challenge it
        was never meant for and would almost always fail the second
        factor anyway, so we fail CLOSED: cancel the session and surface
        a clear error instead of replaying. Returning a synthetic `error`
        reply routes through `_handle_error_reply`, which cancels and
        emits `failed` exactly like a real greetd error.

        For `info` / `error` (non-secret) types we acknowledge with a
        null response so greetd can advance to the next stage; we NEVER
        replay the password into a non-secret prompt.
        """
        secret_answered = False
        while reply.get("type") == "auth_message":
            kind = reply.get("auth_message_type", "secret")
            message = reply.get("auth_message", "")
            if kind == "secret":
                if secret_answered:
                    # Second secret prompt: we have no answer. Fail closed
                    # rather than replaying the first password.
                    log.warning(
                        "PAM issued a second secret prompt; greeter "
                        "collects one secret per attempt — failing closed"
                    )
                    return {
                        "type": "error",
                        "error_type": "error",
                        "description": MULTI_SECRET_UNSUPPORTED_MSG,
                    }
                secret_answered = True
                reply = await self._client.post_auth(password)
            else:
                # visible / info / error: per greetd-ipc(7), `visible`
                # is a non-secret prompt whose response many PAM modules
                # log in plaintext. NEVER replay the password into one.
                # MVP-safe: display the message and ack with null so the
                # stack advances; the user can retry if they need a real
                # response collected.
                if message:
                    self._post_status(message)
                reply = await self._client.post_auth(None)
        return reply

    async def _handle_error_reply(self, reply: dict) -> None:
        if reply.get("type") == "error":
            description = reply.get("description", "Authentication failed")
            self._post_status(description)
        else:
            self._post_status(reply.get("description", "Authentication failed"))
        # Cancel the session so greetd returns to a clean state and the
        # next submit() can retry from create_session. Per greetd-ipc(7),
        # an `error` reply (auth_error or error) leaves the session in
        # an aborted state on greetd's side, but the client must still
        # send cancel_session for the slot to be reclaimable before the
        # next create_session. This matches the docstring contract at
        # the top of this module ("error+auth_error → cancel_session").
        # _cancel_quiet swallows any post-error socket errors.
        await self._cancel_quiet()
        self._post_failed()

    async def _cancel_quiet(self) -> None:
        try:
            if self._client.connected:
                await self._client.cancel_session()
        except Exception:  # noqa: BLE001
            pass
