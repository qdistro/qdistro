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
import pwd
import threading

from PyQt6.QtCore import QObject, pyqtSignal

log = logging.getLogger("qdlocker.auth")


class AuthOutcome(enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"


class AuthBackend(QObject):
    """Coordinates fprintd + PAM."""

    ready = pyqtSignal()
    message = pyqtSignal(str, bool, bool)
    outcome = pyqtSignal(object)

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
        # Identity comes from the kernel — the real uid of the locker process
        # — not from $USER/$LOGNAME. As a TCB process the locker must
        # authenticate whoever actually owns it, never an attacker-mutable env
        # var (the "identity-from-kernel" envelope of the threat model): a
        # tampered $USER/$LOGNAME must not redirect PAM/fprintd auth to a
        # different account. Fail closed if NSS/passwd can't resolve the uid.
        try:
            self._pam_user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError as exc:
            raise RuntimeError(
                "qdlocker: cannot resolve the unlock account from the running uid"
            ) from exc
        self._state_lock = threading.Lock()
        # Monotonically increasing session generation. Bumped on every
        # reset_session() (i.e. each fresh lock). Every emitted outcome
        # carries the generation that was current when its auth attempt
        # ran, so the controller can drop a stale outcome that a slow PAM
        # or fprintd worker delivers after the session already advanced
        # (e.g. a laggy PAM FAILED racing in after fprintd already
        # unlocked, or any worker finishing after the next lock began).
        self._session_generation = 0
        self._fprintd_busy = False
        # Generation of the worker currently holding `_fprintd_busy` (valid
        # only while busy). Lets a same-generation duplicate arm (e.g. a
        # keystroke while the lock-start worker is still in flight) be a true
        # no-op, and a re-arm be scheduled ONLY when the busy worker belongs
        # to a superseded lock.
        self._fprintd_busy_generation = 0
        # Set when an arm request (occupy_fingerprint_sensor(True)) arrives
        # while a worker from a PRIOR generation is still busy (e.g. the old
        # worker is in its D-Bus disconnect/cleanup after a success when the
        # screen relocks). The busy flag suppresses an immediate start, so we
        # remember that the current generation still wants the sensor armed
        # and start a fresh worker from the old worker's `finally` once it
        # clears `_fprintd_busy`. Without this, a relock that races an
        # in-flight worker would leave the fresh lock with no fprintd verify
        # (and no PAM until a keystroke) — the very gap this whole change
        # closes. Holds the generation that requested the re-arm so a worker
        # finishing into a *superseded* session doesn't re-arm a dead one.
        self._fprintd_rearm_generation: int | None = None
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
        # _fprintd_unavailable: set once fprintd is determined to be
        # environmentally absent for THIS lock session (no dbus-next,
        # fprintd off the bus, listener API missing, or a D-Bus
        # timeout/hang). Sticky within a session — it avoids re-paying the
        # discovery cost on every keystroke and immediately routes to PAM —
        # but cleared by reset_session() so a transient wedge is retried on
        # the next lock.
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
            # Clear the per-session unavailable latch so a TRANSIENT wedge
            # (a one-off claim/verify-start/connect timeout) is retried on
            # the next lock instead of permanently disabling fingerprint
            # for the rest of the process. If fprintd is genuinely absent
            # the next discovery just re-fails cheaply and re-latches.
            # When fprintd was disabled at construction it stays disabled.
            if self._fprintd_enabled:
                self._fprintd_unavailable = False
            self._session_generation += 1
            gen = self._session_generation
        log.debug("auth session reset (generation=%d)", gen)

    def _current_generation(self) -> int:
        with self._state_lock:
            return self._session_generation

    def _emit_outcome(self, outcome: AuthOutcome, generation: int) -> None:
        """Emit an outcome tagged with the session generation it belongs
        to, so the controller can drop it if the session has since moved
        on (stale-outcome race guard)."""
        self.outcome.emit((outcome, generation))

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
            # Expected: a new worker is suppressed while the single in-flight
            # one is parked waiting for the password. This fires once per
            # keystroke (each re-arms the fingerprint sensor → start_pam) and
            # is NOT evidence of a wedged retry loop, so keep it at debug.
            log.debug(
                "PAM start suppressed: previous worker is still alive "
                "(parked waiting for password; normal per-keystroke re-arm)."
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
            if not on:
                # An explicit disarm cancels any pending re-arm.
                self._fprintd_rearm_generation = None
                return
            if not self._fprintd_busy:
                self._fprintd_busy = True
                self._fprintd_busy_generation = self._session_generation
                self._fprintd_rearm_generation = None
                start = True
            elif self._fprintd_busy_generation == self._session_generation:
                # The in-flight worker belongs to the CURRENT lock — the
                # sensor is already armed for this session, so a duplicate
                # arm (e.g. a keystroke while the lock-start worker is still
                # running) is a true no-op. No redundant re-arm scheduled.
                start = False
            else:
                # The in-flight worker belongs to an OLDER lock that is still
                # cleaning up. Remember that this fresh generation wants the
                # sensor so the worker's `finally` starts a new one once it
                # clears `_fprintd_busy`.
                start = False
                self._fprintd_rearm_generation = self._session_generation
        if start:
            self._spawn_fprint_worker()

    def _spawn_fprint_worker(self) -> None:
        threading.Thread(
            target=self._fprint_worker, name="qdlocker-fprintd", daemon=True
        ).start()

    # ---- failure accounting helpers ----

    def _record_fprintd_failure(
        self, *, environmental: bool, generation: int | None = None
    ) -> None:
        """Increment the failure counter and start PAM fallback.

        An *environmental* failure (no dbus-next, fprintd off the bus, a
        D-Bus timeout/hang, listener API missing) means the fingerprint
        path is dead for this session, so we fail CLOSED immediately:
        mark it unavailable and start PAM/password right away rather than
        waiting out the 3-strike threshold (which would otherwise leave
        the user with no prompt until the next keystroke). A *real*
        verify-no-match still counts toward the strike threshold so a
        determined attacker eventually trips the PAM fallback.

        Holds _state_lock around both the increment and the
        should-fallback dispatch decision so two concurrent workers can't
        both fire start_pam after both cross the threshold.

        If `generation` is supplied and no longer matches the current
        session, the call is a stale worker from a superseded lock: drop
        its side effects entirely so it can't poison the fresh session's
        failure counter or spuriously start PAM."""
        with self._state_lock:
            if generation is not None and generation != self._session_generation:
                log.info(
                    "dropping stale fprintd failure (gen=%s, current=%s)",
                    generation, self._session_generation,
                )
                return
            if environmental:
                self._fprintd_unavailable = True
            self._fprintd_failures += 1
            count = self._fprintd_failures
            crossed = (
                (environmental or count >= self._max_fprintd_failures)
                and not self._falling_back_to_pam
            )
            if crossed:
                self._falling_back_to_pam = True
        log.info("fprintd failure recorded (count=%d, threshold=%d, env=%s)",
                 count, self._max_fprintd_failures, environmental)
        if crossed:
            log.info("fprintd unavailable/threshold reached; starting PAM fallback")
            self.start_pam()

    def _pam_worker(self) -> None:
        # Capture the generation this attempt belongs to. If a later lock
        # bumps it (or fprintd unlocks first), the controller drops any
        # outcome we emit below as stale.
        generation = self._current_generation()
        try:
            import pam
        except ImportError:
            log.error("python-pam not installed; PAM auth unavailable")
            self._emit_outcome(AuthOutcome.FAILED, generation)
            return

        auth = pam.pam()

        self.message.emit("Password", False, True)
        password = None
        while password is None:
            if self._pam_abort.wait(timeout=0.05):
                self._emit_outcome(AuthOutcome.ABORTED, generation)
                return
            with self._state_lock:
                password = self._pam_pending_password
                if password is not None:
                    self._pam_pending_password = None

        try:
            ok = auth.authenticate(
                self._pam_user,
                password,
                service=self._pam_service or "login",
                call_end=True,
            )
        except Exception:
            log.exception("PAM authentication raised")
            self._emit_outcome(AuthOutcome.FAILED, generation)
            return

        if self._pam_abort.is_set():
            self._emit_outcome(AuthOutcome.ABORTED, generation)
            return
        self._emit_outcome(
            AuthOutcome.SUCCESS if ok else AuthOutcome.FAILED, generation
        )

    def _fprint_worker(self) -> None:
        try:
            asyncio.run(self._fprint_async())
        except Exception:
            log.exception("fprintd verify raised")
        finally:
            # Clear busy and decide — under the same lock — whether a fresh
            # worker is owed. A re-arm is honored only if a newer lock
            # requested the sensor (rearm_generation set) AND that generation
            # is still the current one AND fprintd is still viable for it
            # (not env-unavailable). This closes the relock-races-cleanup gap
            # without ever running two workers at once (we re-set busy before
            # releasing the lock).
            with self._state_lock:
                self._fprintd_busy = False
                rearm = (
                    self._fprintd_rearm_generation is not None
                    and self._fprintd_rearm_generation == self._session_generation
                    and self._fprintd_enabled
                    and not self._fprintd_unavailable
                )
                if rearm:
                    self._fprintd_busy = True
                    self._fprintd_busy_generation = self._session_generation
                self._fprintd_rearm_generation = None
            if rearm:
                log.info("re-arming fprintd for the current lock session")
                self._spawn_fprint_worker()

    async def _fprint_async(self) -> None:
        # Generation this fingerprint attempt belongs to; tags the SUCCESS
        # emit so a stale match delivered after the next lock is dropped.
        generation = self._current_generation()
        try:
            from dbus_next import BusType
            from dbus_next.aio import MessageBus
        except ImportError:
            log.warning("dbus-next not installed; fingerprint disabled")
            self._record_fprintd_failure(environmental=True, generation=generation)
            return

        # Bound the connect itself: a wedged system bus can otherwise hang
        # the whole fingerprint path indefinitely, before the verify timeout
        # below ever has a chance to arm. Fail CLOSED (env-unavailable → PAM).
        try:
            bus = await asyncio.wait_for(
                MessageBus(bus_type=BusType.SYSTEM).connect(),
                timeout=self._fprintd_timeout,
            )
        except TimeoutError:
            log.warning("fprintd system-bus connect timed out; falling back")
            self._record_fprintd_failure(environmental=True, generation=generation)
            return
        except Exception:
            log.exception("could not connect to system bus")
            self._record_fprintd_failure(environmental=True, generation=generation)
            return

        try:
            # Each of introspect / get_default_device / claim / verify-start
            # is a round-trip to fprintd over the system bus; any of them can
            # wedge if fprintd is stuck. Cap every one with the same timeout
            # so a hang anywhere in the discovery/claim phase fails CLOSED to
            # PAM instead of leaving the locker stuck on the fingerprint path.
            try:
                mgr_intro = await asyncio.wait_for(
                    bus.introspect(
                        "net.reactivated.Fprint",
                        "/net/reactivated/Fprint/Manager",
                    ),
                    timeout=self._fprintd_timeout,
                )
            except TimeoutError:
                log.warning("fprintd Manager introspect timed out; falling back")
                self._record_fprintd_failure(environmental=True, generation=generation)
                return
            except Exception:
                log.info("fprintd not available on system bus; skipping")
                self._record_fprintd_failure(environmental=True, generation=generation)
                return

            mgr_obj = bus.get_proxy_object(
                "net.reactivated.Fprint", "/net/reactivated/Fprint/Manager",
                mgr_intro,
            )
            mgr = mgr_obj.get_interface("net.reactivated.Fprint.Manager")
            try:
                dev_path = await asyncio.wait_for(
                    mgr.call_get_default_device(),  # type: ignore[attr-defined]
                    timeout=self._fprintd_timeout,
                )
            except TimeoutError:
                log.warning("fprintd GetDefaultDevice timed out; falling back")
                self._record_fprintd_failure(environmental=True, generation=generation)
                return
            except Exception:
                # A machine with no enrolled reader makes fprintd answer
                # GetDefaultDevice with net.reactivated.Fprint.Error.NoSuchDevice
                # ("No devices available"). That's a DBusError, not a timeout —
                # it MUST fail CLOSED to PAM, not propagate out of the worker
                # uncaught (which would swallow it via _fprint_worker's generic
                # except and leave the user with no prompt). This matters now
                # that the sensor is armed on every fresh lock, even on
                # deviceless hosts.
                log.info("fprintd has no default device; falling back")
                self._record_fprintd_failure(environmental=True, generation=generation)
                return

            try:
                dev_intro = await asyncio.wait_for(
                    bus.introspect("net.reactivated.Fprint", dev_path),
                    timeout=self._fprintd_timeout,
                )
            except TimeoutError:
                log.warning("fprintd Device introspect timed out; falling back")
                self._record_fprintd_failure(environmental=True, generation=generation)
                return
            except Exception:
                log.info("fprintd device introspect failed; falling back")
                self._record_fprintd_failure(environmental=True, generation=generation)
                return
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
                self._record_fprintd_failure(environmental=True, generation=generation)
                return

            # Auth accounting is decided exactly once below via `result`,
            # one of: "match", "no_match", "env_fail", "verify_fail".
            # Doing it inline used to double-count (e.g. a verify timeout
            # recorded a failure in `except` AND again in the `else`).
            result = "no_match"
            try:
                # Claim and VerifyStart are blocking round-trips that can
                # hang before the verify-result wait below arms. A hang
                # here means fprintd itself is wedged — that's an
                # ENVIRONMENTAL failure, so it must fail CLOSED to PAM
                # immediately rather than burning a single non-environmental
                # strike. Handle their timeout separately from the
                # verify-RESULT timeout (which legitimately just means "no
                # finger presented in N seconds" and stays non-environmental).
                try:
                    await asyncio.wait_for(
                        dev.call_claim(self._pam_user),  # type: ignore[attr-defined]
                        timeout=self._fprintd_timeout,
                    )
                    await asyncio.wait_for(
                        dev.call_verify_start("any"),  # type: ignore[attr-defined]
                        timeout=self._fprintd_timeout,
                    )
                except TimeoutError:
                    log.warning("fprintd claim/verify-start timed out; falling back")
                    result = "env_fail"

                if result != "env_fail":
                    matched = await asyncio.wait_for(
                        result_future, timeout=self._fprintd_timeout
                    )
                    result = "match" if matched else "no_match"
            except TimeoutError:
                # Verify-result timeout: no finger seen in time. Real
                # (non-environmental) — counts toward the strike threshold.
                log.info("fprintd verify timeout")
                result = "no_match"
            except Exception:
                log.exception("fprintd verify failed")
                result = "verify_fail"
            finally:
                # Bound cleanup too: if fprintd is wedged, verify-stop /
                # release can hang just like the claim, which would defeat
                # the whole fail-closed cap. Best-effort with a timeout.
                for cleanup in (
                    lambda: dev.call_verify_stop(),  # type: ignore[attr-defined]
                    lambda: dev.call_release(),  # type: ignore[attr-defined]
                ):
                    try:
                        await asyncio.wait_for(
                            cleanup(), timeout=self._fprintd_timeout
                        )
                    except Exception:
                        pass
                try:
                    dev.off_verify_status(on_status)  # type: ignore[attr-defined]
                except Exception:
                    pass

            if result == "match":
                # Guard the success side effects against a stale worker:
                # if a newer lock already advanced the generation, this
                # match belongs to a dead session. Hold _state_lock across
                # the stale check AND `_pam_abort.set()` so a concurrent
                # reset_session()/new PAM worker can't interleave between
                # them and get its fresh worker aborted by this stale one.
                with self._state_lock:
                    stale = generation != self._session_generation
                    if stale:
                        log.info(
                            "dropping stale fprintd match (gen=%s, current=%s)",
                            generation, self._session_generation,
                        )
                    else:
                        # Per-session reset of failure tally happens on the
                        # next lock; for now just zero so a later parallel
                        # attempt doesn't trip the fallback inappropriately.
                        self._fprintd_failures = 0
                        # Abort the in-flight PAM worker for THIS session
                        # while still holding the lock so it can't be a
                        # freshly-started one from a newer generation.
                        self._pam_abort.set()
                if not stale:
                    self._emit_outcome(AuthOutcome.SUCCESS, generation)
            elif result == "env_fail":
                self._record_fprintd_failure(
                    environmental=True, generation=generation
                )
            else:
                # "no_match" (wrong finger or verify-result timeout) and
                # "verify_fail" (unexpected verify error) both count toward
                # the strike threshold so the PAM fallback eventually fires.
                self._record_fprintd_failure(
                    environmental=False, generation=generation
                )
        finally:
            # Bound disconnect too. dbus-next's disconnect closes the
            # local transport rather than round-tripping, so it should
            # never block on a wedged peer — but cap it anyway so the
            # worker (and `_fprintd_busy`) can never get pinned here.
            try:
                await asyncio.wait_for(
                    bus.disconnect(), timeout=self._fprintd_timeout
                )
            except Exception:
                pass
