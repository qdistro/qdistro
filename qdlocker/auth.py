"""Auth backend — fprintd (D-Bus) + PAM fallback.

Mirrors the LockContext.qml flow: fingerprint runs in parallel with
password entry; either path can succeed. fprintd is reached over the
system bus at `net.reactivated.Fprint`; PAM uses python-pam.

The fprintd failure counter is reset at the start of each lock session
(via `reset_session()` called from the controller on lock_requested).
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
    """Coordinates fprintd + PAM."""

    ready = Signal()
    message = Signal(str, bool, bool)
    outcome = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        max_fprintd_failures: int = 3,
        fprintd_timeout_s: float = 10.0,
        fprintd_enabled: bool = True,
    ) -> None:
        super().__init__(parent)
        self._pam_service = os.environ.get("QDLOCKER_PAM_SERVICE")
        self._pam_user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not self._pam_user:
            raise RuntimeError(
                "qdlocker: cannot determine admin username "
                "(neither $USER nor $LOGNAME set)"
            )
        self._state_lock = threading.Lock()
        self._fprintd_busy = False
        self._pam_thread: threading.Thread | None = None
        self._pam_pending_password: str | None = None
        self._pam_abort = threading.Event()

        # Configuration (overridable via constructor kwargs).
        self._max_fprintd_failures = max(1, int(max_fprintd_failures))
        self._fprintd_timeout = float(fprintd_timeout_s)
        self._fprintd_enabled = bool(fprintd_enabled)

        # Per-lock-session state. Reset by `reset_session()` whenever the
        # locker transitions from unlocked → locked.
        self._fprintd_failures = 0
        # _fprintd_unavailable: sticky once fprintd is determined to be
        # environmentally absent (no dbus-next, fprintd off the bus,
        # listener API missing). Avoids re-paying the discovery cost on
        # every keystroke and immediately routes to PAM.
        self._fprintd_unavailable = not fprintd_enabled
        # Suppress duplicate fallback dispatch when multiple workers
        # cross the threshold simultaneously.
        self._falling_back_to_pam = False

    # ---- session-boundary hooks (called by LockController) ----

    def reset_session(self) -> None:
        """Reset per-lock-session counters. Call when a new lock begins."""
        with self._state_lock:
            self._fprintd_failures = 0
            self._falling_back_to_pam = False
        log.debug("auth session reset")

    def probe_pam(self) -> None:
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
        if self._pam_thread and self._pam_thread.is_alive():
            log.warning(
                "PAM start suppressed: previous worker is still alive. "
                "If the user has waited >10s on a stuck conversation this "
                "is the cause."
            )
            return
        self._pam_abort.clear()
        self._pam_thread = threading.Thread(
            target=self._pam_worker, name="qdlocker-pam", daemon=True
        )
        self._pam_thread.start()

    def respond_pam(self, password: str) -> None:
        with self._state_lock:
            self._pam_pending_password = password

    def abort_pam(self) -> None:
        self._pam_abort.set()

    def occupy_fingerprint_sensor(self, on: bool) -> None:
        if not self._fprintd_enabled:
            return
        if self._fprintd_unavailable:
            # Hardware/env permanently absent; start PAM directly on first ask.
            if on:
                self.start_pam()
            return
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

    # ---- failure accounting helpers ----

    def _record_fprintd_failure(self, *, environmental: bool) -> None:
        """Increment the failure counter and start PAM fallback when
        the threshold is crossed. Holds _state_lock around both the
        increment and the should-fallback dispatch decision so two
        concurrent workers can't both fire start_pam after both cross
        the threshold."""
        with self._state_lock:
            if environmental:
                self._fprintd_unavailable = True
            self._fprintd_failures += 1
            count = self._fprintd_failures
            crossed = (count >= self._max_fprintd_failures
                       and not self._falling_back_to_pam)
            if crossed:
                self._falling_back_to_pam = True
        log.info("fprintd failure recorded (count=%d, threshold=%d, env=%s)",
                 count, self._max_fprintd_failures, environmental)
        if crossed:
            log.info("fprintd threshold reached; starting PAM fallback")
            self.start_pam()

    def _pam_worker(self) -> None:
        try:
            import pam
        except ImportError:
            log.error("python-pam not installed; PAM auth unavailable")
            self.outcome.emit(AuthOutcome.FAILED)
            return

        auth = pam.pam()

        def conversation(messages):
            replies = []
            for style, msg in messages:
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
            self._record_fprintd_failure(environmental=True)
            return

        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception:
            log.exception("could not connect to system bus")
            self._record_fprintd_failure(environmental=True)
            return

        try:
            try:
                mgr_intro = await bus.introspect(
                    "net.reactivated.Fprint", "/net/reactivated/Fprint/Manager"
                )
            except Exception:
                log.info("fprintd not available on system bus; skipping")
                self._record_fprintd_failure(environmental=True)
                return

            mgr_obj = bus.get_proxy_object(
                "net.reactivated.Fprint", "/net/reactivated/Fprint/Manager",
                mgr_intro,
            )
            mgr = mgr_obj.get_interface("net.reactivated.Fprint.Manager")
            dev_path = await mgr.call_get_default_device()  # type: ignore[attr-defined]

            dev_intro = await bus.introspect("net.reactivated.Fprint", dev_path)
            dev_obj = bus.get_proxy_object(
                "net.reactivated.Fprint", dev_path, dev_intro
            )
            dev = dev_obj.get_interface("net.reactivated.Fprint.Device")

            loop = asyncio.get_running_loop()
            result_future: asyncio.Future[bool] = loop.create_future()

            def on_status(result: str, done: bool) -> None:
                log.info("fprintd VerifyStatus: %s done=%s", result, done)
                if result_future.done():
                    return
                if result == "verify-match":
                    result_future.set_result(True)
                elif done:
                    result_future.set_result(False)

            try:
                dev.on_verify_status(on_status)  # type: ignore[attr-defined]
            except Exception:
                log.exception("could not register VerifyStatus listener")
                self._record_fprintd_failure(environmental=True)
                return

            matched = False
            try:
                await dev.call_claim(self._pam_user)  # type: ignore[attr-defined]
                await dev.call_verify_start("any")  # type: ignore[attr-defined]
                matched = await asyncio.wait_for(
                    result_future, timeout=self._fprintd_timeout
                )
            except asyncio.TimeoutError:
                log.info("fprintd verify timeout")
                self._record_fprintd_failure(environmental=False)
            except Exception:
                log.exception("fprintd verify failed")
                self._record_fprintd_failure(environmental=False)
            finally:
                for cleanup in (
                    lambda: dev.call_verify_stop(),  # type: ignore[attr-defined]
                    lambda: dev.call_release(),  # type: ignore[attr-defined]
                ):
                    try:
                        await cleanup()
                    except Exception:
                        pass
                try:
                    dev.off_verify_status(on_status)  # type: ignore[attr-defined]
                except Exception:
                    pass

            if matched:
                # Per-session reset of failure tally happens on the next
                # lock; for now just zero so a later parallel attempt
                # doesn't trip the fallback inappropriately.
                with self._state_lock:
                    self._fprintd_failures = 0
                self._pam_abort.set()
                self.outcome.emit(AuthOutcome.SUCCESS)
            else:
                # Real verify-no-match path — the fprintd state machine
                # told us "wrong finger" via the `done=True` signal.
                # Count this toward the 3-strike threshold so the PAM
                # fallback eventually fires for a determined attacker.
                self._record_fprintd_failure(environmental=False)
        finally:
            try:
                await bus.disconnect()
            except Exception:
                pass
