"""Auth backend — fprintd (D-Bus) + PAM fallback.

Mirrors the LockContext.qml flow: fingerprint runs in parallel with
password entry; either path can succeed. fprintd is reached over the
system bus at `net.reactivated.Fprint`; PAM uses python-pam.

Per qdistro/doc/sessions.md:39-40 the locker uses fprintd over D-Bus
directly (no PAM on the fingerprint path) and PAM only as the password
fallback.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import threading

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("qdlocker.auth")


class AuthOutcome(enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"


class AuthBackend(QObject):
    """Coordinates fprintd + PAM. Owned by the main thread; the bus and
    PAM calls run on a worker thread / asyncio loop to keep the UI
    responsive."""

    ready = Signal()  # PAM service path detected
    message = Signal(str, bool, bool)  # text, is_error, response_required
    outcome = Signal(object)  # AuthOutcome

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pam_service = os.environ.get("QDLOCKER_PAM_SERVICE")
        self._pam_user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not self._pam_user:
            # An empty username silently makes every PAM auth fail
            # with a confusing 'failed' outcome. Hard-fail loudly so
            # operators can see the config bug.
            raise RuntimeError(
                "qdlocker: cannot determine admin username "
                "(neither $USER nor $LOGNAME set)"
            )
        # _state_lock guards _pam_pending_password and _fprintd_busy
        # against the worker/main thread interleaving.
        self._state_lock = threading.Lock()
        self._fprintd_busy = False
        self._pam_thread: threading.Thread | None = None
        self._pam_pending_password: str | None = None
        # _pam_abort.set() makes the worker thread bail out of its
        # conversation poll loop on the next 50ms tick. Used by
        # abort_pam() and by the fprintd-match path (which beats PAM
        # to a successful auth).
        self._pam_abort = threading.Event()
        
        # Track fprintd attempts and manage timeout
        self._fprintd_attempts = 0
        self._fprintd_failures = 0
        self._max_fprintd_failures = 3  # Maximum allowed fprintd failures before fallback
        self._fprintd_timeout = 10.0   # Timeout in seconds for fprintd verification
        self._fprintd_timer = None

    def probe_pam(self) -> None:
        """Pick the PAM service file (env override → login → system-auth
        → common-auth). Synchronous, fast; emits `ready` when done."""
        if self._pam_service:
            log.info("PAM service from env: %s", self._pam_service)
            self.ready.emit()
            return
        for candidate in ("login", "system-auth", "common-auth"):
            if os.path.exists(f"/etc/pam.d/{candidate}"):
                self._pam_service = candidate
                log.info("PAM service detected: %s", candidate)
                break
        else:
            self._pam_service = "login"
            log.warning("no PAM service file found; defaulting to 'login'")
        self.ready.emit()

    def start_pam(self) -> None:
        """Begin a PAM auth attempt for the admin user. Runs on a worker
        thread because PAM's API is blocking."""
        if self._pam_thread and self._pam_thread.is_alive():
            log.debug("PAM already in flight; ignoring duplicate start")
            return
        self._pam_abort.clear()
        self._pam_thread = threading.Thread(
            target=self._pam_worker, name="qdlocker-pam", daemon=True
        )
        self._pam_thread.start()

    def respond_pam(self, password: str) -> None:
        """Provide the password requested by the most recent PAM
        challenge. Guarded by _state_lock because the conversation
        callback reads `_pam_pending_password` on the worker thread."""
        with self._state_lock:
            self._pam_pending_password = password

    def abort_pam(self) -> None:
        """Cancel an in-flight PAM attempt (e.g. user kept typing)."""
        self._pam_abort.set()

    def occupy_fingerprint_sensor(self, on: bool) -> None:
        """While the user is typing a password, run a parallel fprintd
        verify so a fingerprint also unlocks. Guarded by _state_lock
        because the check-and-set on `_fprintd_busy` is otherwise a
        race window where two rapid `currentText` flips can launch
        two workers."""
        with self._state_lock:
            if on and not self._fprintd_busy:
                self._fprintd_busy = True
                start = True
            else:
                start = False
        if start:
            threading.Thread(
                target=self._fprint_worker, name="qdlocker-fprintd", daemon=True
            ).start()
        # `on=False` is best-effort; the worker self-completes within
        # ~30s once VerifyStart is queued, and the cancellation API
        # (VerifyStop) is documented as racy with in-flight matches.

    def _pam_worker(self) -> None:
        try:
            import pam  # python-pam
        except ImportError:
            log.error("python-pam not installed; PAM auth unavailable")
            self.outcome.emit(AuthOutcome.FAILED)
            return

        auth = pam.pam()

        def conversation(messages):
            replies = []
            for style, msg in messages:
                # style: 1=PROMPT_ECHO_OFF (password), 2=PROMPT_ECHO_ON,
                # 3=ERROR_MSG, 4=TEXT_INFO
                is_error = style == 3
                response_required = style in (1, 2)
                self.message.emit(msg, is_error, response_required)
                if response_required:
                    pw = None
                    while pw is None:
                        if self._pam_abort.wait(timeout=0.05):
                            return [(None, 0) for _ in messages]
                        with self._state_lock:
                            pw = self._pam_pending_password
                            if pw is not None:
                                self._pam_pending_password = None
                    replies.append((pw, 0))
                else:
                    replies.append(("", 0))
            return replies

        try:
            ok = auth.authenticate(
                self._pam_user,
                None,
                service=self._pam_service or "login",
                call_end=True,
                conv=conversation,
            )
        except Exception:
            log.exception("PAM authentication raised")
            self.outcome.emit(AuthOutcome.FAILED)
            return

        if self._pam_abort.is_set():
            self.outcome.emit(AuthOutcome.ABORTED)
            return
        self.outcome.emit(AuthOutcome.SUCCESS if ok else AuthOutcome.FAILED)

    def _fprint_worker(self) -> None:
        """Verify against admin's enrolled prints via fprintd D-Bus.

        Flow (per sessions.md:63-64):
          1. net.reactivated.Fprint.Manager.GetDefaultDevice
          2. Device.Claim(username)
          3. Device.VerifyStart("any")
          4. wait for VerifyStatus signal with result="verify-match"
          5. Device.VerifyStop + Device.Release
        """
        try:
            asyncio.run(self._fprint_async())
        except Exception:
            log.exception("fprintd verify raised")
        finally:
            with self._state_lock:
                self._fprintd_busy = False

    async def _fprint_async(self) -> None:
        try:
            from dbus_next.aio import MessageBus
            from dbus_next import BusType
        except ImportError:
            log.warning("dbus-next not installed; fingerprint disabled")
            # Fallback to PAM after incrementing failure count
            with self._state_lock:
                self._fprintd_failures += 1
                should_fallback = self._fprintd_failures >= self._max_fprintd_failures
            if should_fallback:
                self.start_pam()
            return

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            mgr_intro = await bus.introspect(
                "net.reactivated.Fprint", "/net/reactivated/Fprint/Manager"
            )
        except Exception:
            log.info("fprintd not available on system bus; skipping")
            with self._state_lock:
                self._fprintd_failures += 1
                should_fallback = self._fprintd_failures >= self._max_fprintd_failures
            if should_fallback:
                self.start_pam()
            await bus.disconnect()
            return

        mgr_obj = bus.get_proxy_object(
            "net.reactivated.Fprint", "/net/reactivated/Fprint/Manager", mgr_intro
        )
        mgr = mgr_obj.get_interface("net.reactivated.Fprint.Manager")
        dev_path = await mgr.call_get_default_device()  # type: ignore[attr-defined]

        dev_intro = await bus.introspect("net.reactivated.Fprint", dev_path)
        dev_obj = bus.get_proxy_object("net.reactivated.Fprint", dev_path, dev_intro)
        dev = dev_obj.get_interface("net.reactivated.Fprint.Device")

        result_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def on_status(result: str, done: bool) -> None:
            log.info("fprintd VerifyStatus: %s done=%s", result, done)
            if result_future.done():
                return
            if result == "verify-match":
                result_future.set_result(True)
            elif done:
                result_future.set_result(False)

        # Register the listener BEFORE VerifyStart. If we register
        # after, dbus-next may have already processed the first
        # verify-match signal and the future is never resolved.
        try:
            dev.on_verify_status(on_status)  # type: ignore[attr-defined]
        except Exception:
            log.exception("could not register VerifyStatus listener")
            with self._state_lock:
                self._fprintd_failures += 1
                should_fallback = self._fprintd_failures >= self._max_fprintd_failures
            if should_fallback:
                self.start_pam()
            await bus.disconnect()
            return

        matched = False
        try:
            await dev.call_claim(self._pam_user)  # type: ignore[attr-defined]
            await dev.call_verify_start("any")  # type: ignore[attr-defined]
            # Use the configured timeout instead of 30s
            matched = await asyncio.wait_for(result_future, timeout=self._fprintd_timeout)
        except asyncio.TimeoutError:
            log.info("fprintd verify timeout")
            with self._state_lock:
                self._fprintd_failures += 1
                should_fallback = self._fprintd_failures >= self._max_fprintd_failures
            if should_fallback:
                self.start_pam()
        except Exception:
            log.exception("fprintd verify failed")
            with self._state_lock:
                self._fprintd_failures += 1
                should_fallback = self._fprintd_failures >= self._max_fprintd_failures
            if should_fallback:
                self.start_pam()
        finally:
            try:
                await dev.call_verify_stop()  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                await dev.call_release()  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                dev.off_verify_status(on_status)  # type: ignore[attr-defined]
            except Exception:
                pass
            await bus.disconnect()

        if matched:
            # Reset failure counter on success
            with self._state_lock:
                self._fprintd_failures = 0
            # Abort any in-flight PAM attempt — otherwise the worker
            # thread parks forever waiting for `_pam_pending_password`,
            # and the next `start_pam` is suppressed by the
            # `is_alive()` guard for the rest of the process lifetime.
            self._pam_abort.set()
            self.outcome.emit(AuthOutcome.SUCCESS)
