"""systemd-logind session-bus subscription.

When `HandleLidSwitch=lock` is configured in
`/etc/systemd/logind.conf.d/`, logind emits the
`org.freedesktop.login1.Session.Lock` D-Bus signal on the session
object (not via any shell script). This module subscribes to that
signal on the system bus and routes it into the locker's
`lock_requested` queue with `reason=1` (lid).

It also subscribes to `org.freedesktop.login1.Manager.PrepareForSleep`
so the locker locks BEFORE the system suspends (reason=2). Locking
after wake leaves a window where keystrokes can reach a not-yet-locked
screen.

Sleep delay-inhibitor
---------------------
A bare `PrepareForSleep(start=True)` subscription is not enough on its
own: the lock chain (asyncio thread -> Qt main loop -> compositor flush
-> first LOCK-layer frame) runs concurrently with the suspend
transition, so the machine can suspend before the lock surface is
painted and resume showing pre-lock content.

To close that race we take a logind *delay* inhibitor
(`Inhibit("sleep", who, why, "delay")`) at startup. logind blocks the
sleep transition while at least one delay inhibitor fd is held (up to
`InhibitDelayMaxSec`, default 5s). On `PrepareForSleep(start=True)` we
trigger the lock and then release the inhibitor fd ONLY after the
compositor confirms it has entered the locked state (the
`qdwin_locker_v1.locked_changed(1)` event, surfaced to us via
`notify_lock_confirmed()`). On resume (`start=False`) we re-acquire the
fd for the next cycle.

The inhibitor is best-effort and fail-open with respect to *suspend*:
if confirmation does not arrive within a bounded timeout, or anything
in the inhibitor path fails, we release the fd anyway so we never wedge
the suspend transition (logind would force it through at
`InhibitDelayMaxSec` regardless). The lock request itself has already
been issued in every path, so the system never suspends *less* locked
than it would without the inhibitor.

The whole module is dbus-next-async and runs on its own asyncio loop in
a daemon thread; it posts events back via the user-supplied callback
which must itself be thread-safe (typically `bridge.inject_lock_requested`
which goes through a Qt QueuedConnection signal).

If dbus-next is unavailable, or logind cannot be reached, the watcher
logs at WARNING and exits cleanly — the lid-close path is degraded but
the rest of the locker still works.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Callable, Optional

log = logging.getLogger("qdlocker.logind")

REASON_LID = 1
REASON_SUSPEND = 2

# How long to hold the suspend transition waiting for the compositor to
# confirm the lock surface is committed. Kept comfortably under logind's
# default InhibitDelayMaxSec (5s) so we release the fd cooperatively
# rather than letting logind time us out.
_LOCK_CONFIRM_TIMEOUT_S = 4.0


class LogindWatcher:
    def __init__(
        self,
        on_lock: Callable[[int], None],
        on_unlock: Optional[Callable[[], None]] = None,
    ) -> None:
        """on_lock(reason) is invoked from the asyncio thread when
        logind signals Session.Lock or PrepareForSleep(start=True).
        Use a thread-safe callback (e.g. emit a Qt signal connected
        with QueuedConnection)."""
        self._on_lock = on_lock
        self._on_unlock = on_unlock
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Set (on the asyncio loop) when the compositor confirms it has
        # entered the locked state. Created inside the loop thread.
        self._lock_confirmed: Optional[asyncio.Event] = None
        # Held delay-inhibitor fd (an int). None when not held.
        self._inhibit_fd: Optional[int] = None
        # The Manager interface proxy, used to (re)acquire the inhibitor.
        self._mgr = None

    def start(self) -> bool:
        """Spawn the asyncio thread. Returns True if the thread was
        started (the actual D-Bus connection happens inside the thread
        and is logged but does not propagate failures here)."""
        if self._thread is not None and self._thread.is_alive():
            return True
        self._thread = threading.Thread(
            target=self._run, name="qdlocker-logind", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._loop is None or self._stop_event is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def notify_lock_confirmed(self) -> None:
        """Thread-safe hook: call when the compositor confirms it has
        entered the locked state (qdwin_locker_v1.locked_changed(1)).

        Wakes the suspend-path waiter so it can release the sleep delay
        inhibitor and let the system suspend with the lock already
        painted. Safe to call from any thread, at any time, including
        before the asyncio loop is up (a no-op until then)."""
        loop = self._loop
        ev = self._lock_confirmed
        if loop is None or ev is None:
            return
        try:
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:
            # Loop already closed; nothing to wake.
            pass

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("logind watcher loop crashed")

    # ----- delay-inhibitor helpers (run on the asyncio thread) -----

    async def _acquire_inhibitor(self) -> None:
        """Take a logind sleep *delay* inhibitor. Best-effort: on any
        failure the fd stays None and suspend proceeds uninhibited."""
        if self._inhibit_fd is not None:
            return
        if self._mgr is None:
            return
        try:
            fd = await self._mgr.call_inhibit(  # type: ignore[attr-defined]
                "sleep",
                "qdlocker",
                "Lock the screen before the system sleeps",
                "delay",
            )
        except Exception:
            log.exception("logind Inhibit(sleep, delay) failed; "
                          "suspend will not wait for the lock surface")
            self._inhibit_fd = None
            return
        # With negotiate_unix_fd=True, dbus-next hands back the UnixFD as a
        # plain int we own and must close. If it ever comes back as
        # something we can't turn into an int, treat it as a failed
        # acquire (fail-open) rather than stashing a value we can't own or
        # close — a fileno() borrowed from a wrapper we don't keep alive
        # could be closed out from under us.
        try:
            self._inhibit_fd = int(fd)
        except (TypeError, ValueError):
            log.error("Inhibit returned a non-fd value %r; treating as "
                      "no inhibitor (suspend will not wait)", fd)
            self._inhibit_fd = None
            return
        log.info("logind sleep delay-inhibitor acquired (fd=%s)",
                 self._inhibit_fd)

    def _release_inhibitor(self) -> None:
        """Close (release) the held delay-inhibitor fd, if any. Closing
        the fd is what tells logind we are done delaying."""
        fd = self._inhibit_fd
        self._inhibit_fd = None
        if fd is None:
            return
        try:
            os.close(fd)
            log.info("logind sleep delay-inhibitor released (fd=%s)", fd)
        except OSError:
            log.exception("closing inhibitor fd %s failed", fd)

    async def _on_prepare_for_sleep_async(self) -> None:
        """Suspend-path coroutine: trigger the lock, wait (bounded) for
        the compositor to confirm it is locked, then release the delay
        inhibitor so the system may sleep."""
        log.info("logind PrepareForSleep(start=True) -> reason=suspend")
        # Arm the confirmation gate BEFORE requesting the lock so a fast
        # locked_changed=1 can't slip in between request and wait.
        if self._lock_confirmed is not None:
            self._lock_confirmed.clear()
        try:
            self._on_lock(REASON_SUSPEND)
        except Exception:
            log.exception("on_lock callback raised")

        if self._inhibit_fd is None or self._lock_confirmed is None:
            # No inhibitor held (acquire failed / unsupported): we cannot
            # delay the transition. The lock was still requested above.
            log.warning("no sleep delay inhibitor held; suspending without "
                        "waiting for lock confirmation")
            return
        try:
            await asyncio.wait_for(
                self._lock_confirmed.wait(), _LOCK_CONFIRM_TIMEOUT_S
            )
            log.info("lock confirmed before suspend; releasing inhibitor")
        except asyncio.TimeoutError:
            log.warning(
                "lock not confirmed within %.1fs; releasing inhibitor "
                "so suspend can proceed (lock was still requested)",
                _LOCK_CONFIRM_TIMEOUT_S,
            )
        finally:
            # Always release: never wedge the suspend transition.
            self._release_inhibitor()

    async def _on_resume_async(self) -> None:
        """Resume-path coroutine: re-acquire the delay inhibitor for the
        next sleep cycle."""
        log.info("logind PrepareForSleep(start=False) -> resume; "
                 "re-acquiring sleep delay inhibitor")
        await self._acquire_inhibitor()

    async def _main(self) -> None:
        try:
            from dbus_next.aio import MessageBus
            from dbus_next import BusType
        except ImportError:
            log.warning("dbus-next not installed; logind lid/suspend lock unavailable")
            return

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._lock_confirmed = asyncio.Event()

        try:
            # negotiate_unix_fd=True is REQUIRED: Manager.Inhibit replies
            # with a UnixFD (the delay-inhibitor handle). Without fd
            # negotiation, dbus-next cannot unmarshal that reply and the
            # connection drops with EOFError. (The lid/suspend signal
            # subscriptions work either way; the inhibitor needs this.)
            bus = await MessageBus(
                bus_type=BusType.SYSTEM, negotiate_unix_fd=True
            ).connect()
        except Exception:
            log.exception("could not connect to system bus; lid-close lock unavailable")
            return

        try:
            # Find this user's session path via Manager.GetSessionByPID($PID).
            mgr_intro = await bus.introspect(
                "org.freedesktop.login1", "/org/freedesktop/login1"
            )
            mgr_obj = bus.get_proxy_object(
                "org.freedesktop.login1", "/org/freedesktop/login1", mgr_intro
            )
            mgr = mgr_obj.get_interface("org.freedesktop.login1.Manager")
            self._mgr = mgr

            try:
                session_path = await mgr.call_get_session_by_pid(  # type: ignore[attr-defined]
                    os.getpid()
                )
            except Exception:
                log.exception("logind GetSessionByPID failed; lid lock unavailable")
                return

            log.info("logind session=%s", session_path)

            # Take the sleep delay inhibitor up front so the very first
            # suspend after startup is already guarded.
            await self._acquire_inhibitor()

            # Subscribe to Session.Lock on the per-session object.
            sess_intro = await bus.introspect(
                "org.freedesktop.login1", session_path
            )
            sess_obj = bus.get_proxy_object(
                "org.freedesktop.login1", session_path, sess_intro
            )
            sess = sess_obj.get_interface("org.freedesktop.login1.Session")

            def _on_lock_signal() -> None:
                log.info("logind Session.Lock received -> reason=lid")
                try:
                    self._on_lock(REASON_LID)
                except Exception:
                    log.exception("on_lock callback raised")

            def _on_unlock_signal() -> None:
                log.info("logind Session.Unlock received")
                if self._on_unlock is not None:
                    try:
                        self._on_unlock()
                    except Exception:
                        log.exception("on_unlock callback raised")

            try:
                sess.on_lock(_on_lock_signal)  # type: ignore[attr-defined]
            except Exception:
                log.exception("could not subscribe to Session.Lock")

            try:
                sess.on_unlock(_on_unlock_signal)  # type: ignore[attr-defined]
            except Exception:
                # Not all logind versions emit Unlock; log at debug.
                log.debug("Session.Unlock subscription unavailable")

            # Subscribe to PrepareForSleep(start). start=True is the
            # pre-suspend phase where the delay inhibitor still holds the
            # transition; start=False is post-resume. The signal handler
            # is sync (dbus-next calls it on the loop), so we schedule the
            # async work as a task on the running loop.
            def _on_prepare_for_sleep(start: bool) -> None:
                if start:
                    self._loop.create_task(self._on_prepare_for_sleep_async())
                else:
                    self._loop.create_task(self._on_resume_async())

            try:
                mgr.on_prepare_for_sleep(_on_prepare_for_sleep)  # type: ignore[attr-defined]
            except Exception:
                log.exception("could not subscribe to PrepareForSleep")

            log.info("logind watcher ready")
            await self._stop_event.wait()
        finally:
            # Release the inhibitor before tearing the bus down so we
            # don't leak a held fd / block a sleep that races shutdown.
            self._release_inhibitor()
            try:
                await bus.disconnect()
            except Exception:
                pass
