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

If dbus-next is unavailable the watcher reports degraded automatic locking.
Transient bus/logind failures reconnect with bounded backoff; each connection
owns its signal subscriptions, pending tasks and sleep inhibitor.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable

log = logging.getLogger("qdlocker.logind")

REASON_LID = 1
REASON_SUSPEND = 2

# How long to hold the suspend transition waiting for the compositor to
# confirm the lock surface is committed. Kept comfortably under logind's
# default InhibitDelayMaxSec (5s) so we release the fd cooperatively
# rather than letting logind time us out.
_LOCK_CONFIRM_TIMEOUT_S = 4.0
_CONNECT_TIMEOUT_S = 5.0
_RECONNECT_MIN_S = 1.0
_RECONNECT_MAX_S = 30.0


class LogindWatcher:
    def __init__(
        self,
        on_lock: Callable[[int], None],
        on_unlock: Callable[[], None] | None = None,
        *,
        lock_on_lid: bool = True,
    ) -> None:
        """on_lock(reason) is invoked from the asyncio thread when
        logind signals Session.Lock or PrepareForSleep(start=True).
        Use a thread-safe callback (e.g. emit a Qt signal connected
        with QueuedConnection)."""
        self._on_lock = on_lock
        self._on_unlock = on_unlock
        self._lock_on_lid = lock_on_lid
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        # Set (on the asyncio loop) when the compositor confirms it has
        # entered the locked state. Created inside the loop thread.
        self._lock_confirmed: asyncio.Event | None = None
        # Held delay-inhibitor fd (an int). None when not held.
        self._inhibit_fd: int | None = None
        self._inhibit_lock = asyncio.Lock()
        # The Manager interface proxy, used to (re)acquire the inhibitor.
        self._mgr = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()

    def start(self) -> bool:
        """Spawn the asyncio thread. Returns True if the thread was
        started (the actual D-Bus connection happens inside the thread
        and is logged but does not propagate failures here)."""
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._run, name="qdlocker-logind", daemon=True
        )
        self._thread.start()
        return True

    @property
    def automatic_lock_ready(self) -> bool:
        """Whether sleep subscription and its delay inhibitor are established."""
        return self._ready.is_set()

    def stop(self) -> None:
        self._stop_requested.set()
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
        """Serialize resume and retry acquisition so only one fd is owned."""
        async with self._inhibit_lock:
            await self._acquire_inhibitor_locked()

    async def _acquire_inhibitor_locked(self) -> None:
        """Take a sleep delay inhibitor; retain sleep signals on failure."""
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
        self._ready.set()
        log.info("logind sleep delay-inhibitor acquired (fd=%s)",
                 self._inhibit_fd)

    def _release_inhibitor(self) -> None:
        """Close (release) the held delay-inhibitor fd, if any. Closing
        the fd is what tells logind we are done delaying."""
        self._ready.clear()
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
        except TimeoutError:
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
            from dbus_next import BusType
            from dbus_next.aio import MessageBus
        except ImportError:
            log.warning("dbus-next not installed; logind lid/suspend lock unavailable")
            return

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._lock_confirmed = asyncio.Event()
        self._inhibit_lock = asyncio.Lock()
        if self._stop_requested.is_set():
            self._stop_event.set()
        stop = asyncio.create_task(self._stop_event.wait())
        connection = None
        delay = _RECONNECT_MIN_S
        try:
            while not self._stop_event.is_set():
                connection = asyncio.create_task(self._watch_connection(
                    lambda: MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True)
                ))
                done, _ = await asyncio.wait(
                    (connection, stop), return_when=asyncio.FIRST_COMPLETED
                )
                if stop in done:
                    break
                try:
                    await connection
                    delay = _RECONNECT_MIN_S
                except Exception:
                    log.exception("logind unavailable; automatic locking degraded; "
                                  "reconnecting in %.1fs", delay)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, _RECONNECT_MAX_S)
        finally:
            if connection is not None:
                connection.cancel()
            stop.cancel()
            await asyncio.gather(*(t for t in (connection, stop) if t is not None),
                                 return_exceptions=True)
            self._ready.clear()
            self._mgr = None
            self._loop = None

    async def _watch_connection(self, bus_factory) -> None:
        """One generation: discard all callbacks/tasks before reconnecting."""
        bus = bus_factory()
        changed = asyncio.Event()
        tasks: set[asyncio.Task] = set()
        subscriptions = []
        active = True
        session_path = None
        sleep_task = None
        sleeping = False

        def completed(task) -> None:
            tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.error("logind sleep callback failed", exc_info=task.exception())

        def sleep_changed(start: bool) -> None:
            nonlocal sleep_task, sleeping
            if not active:
                return
            sleeping = start
            previous = sleep_task

            async def transition() -> None:
                # A resume can arrive while the previous lock confirmation is
                # pending. Finish its cleanup before acquiring the next fd.
                if previous is not None:
                    previous.cancel()
                    await asyncio.gather(previous, return_exceptions=True)
                if active:
                    if start:
                        await self._on_prepare_for_sleep_async()
                    else:
                        await self._on_resume_async()

            sleep_task = asyncio.create_task(transition())
            tasks.add(sleep_task)
            sleep_task.add_done_callback(completed)

        def owner_changed(name: str, old_owner: str, new_owner: str) -> None:
            if active and name == "org.freedesktop.login1" and old_owner != new_owner:
                changed.set()

        def session_new(session_id: str, path: str) -> None:
            if active and session_path is None:
                changed.set()

        def session_removed(session_id: str, path: str) -> None:
            if active and path == session_path:
                changed.set()

        def subscribe(proxy, signal, callback) -> None:
            getattr(proxy, "on_" + signal)(callback)
            subscriptions.append((proxy, signal, callback))

        async def setup() -> None:
            nonlocal session_path
            await bus.connect()
            # Register owner changes before introspecting logind so a restart
            # during setup also invalidates this connection generation.
            intro = await bus.introspect("org.freedesktop.DBus", "/org/freedesktop/DBus")
            obj = bus.get_proxy_object("org.freedesktop.DBus", "/org/freedesktop/DBus", intro)
            subscribe(obj.get_interface("org.freedesktop.DBus"),
                      "name_owner_changed", owner_changed)
            intro = await bus.introspect("org.freedesktop.login1", "/org/freedesktop/login1")
            obj = bus.get_proxy_object("org.freedesktop.login1", "/org/freedesktop/login1", intro)
            mgr = obj.get_interface("org.freedesktop.login1.Manager")
            self._mgr = mgr
            subscribe(mgr, "prepare_for_sleep", sleep_changed)
            subscribe(mgr, "session_new", session_new)
            subscribe(mgr, "session_removed", session_removed)
            await self._acquire_inhibitor()
            try:
                session_path = await self._subscribe_session(
                    bus, mgr, subscriptions, lambda: active
                )
            except Exception:
                log.exception("session lock subscription unavailable; suspend protection remains active")
            if self._inhibit_fd is not None:
                self._ready.set()
                log.info("logind automatic sleep locking ready; session=%s", session_path)
            else:
                log.warning("logind automatic locking degraded: no sleep delay inhibitor")

        async def retry_inhibitor() -> None:
            # Keep the manager sleep signal live even if Inhibit is temporarily
            # unavailable. Retry without reacquiring during an active suspend.
            while True:
                await asyncio.sleep(_RECONNECT_MAX_S)
                if not sleeping:
                    await self._acquire_inhibitor()

        waiters = []
        try:
            await asyncio.wait_for(setup(), _CONNECT_TIMEOUT_S)
            retry_task = asyncio.create_task(retry_inhibitor())
            tasks.add(retry_task)
            waiters = [asyncio.create_task(bus.wait_for_disconnect()),
                       asyncio.create_task(changed.wait())]
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            active = False
            self._ready.clear()
            for proxy, signal, callback in reversed(subscriptions):
                try:
                    getattr(proxy, "off_" + signal)(callback)
                except Exception:
                    log.debug("could not remove logind %s subscription", signal, exc_info=True)
            for task in (*tasks, *waiters):
                task.cancel()
            await asyncio.gather(*tasks, *waiters, return_exceptions=True)
            self._release_inhibitor()
            self._mgr = None
            try:
                bus.disconnect()
            except Exception:
                pass
            if self._stop_event is None or not self._stop_event.is_set():
                log.warning("logind connection ended; automatic locking unavailable until reconnected")

    async def _subscribe_session(self, bus, mgr, subscriptions=None,
                                 is_active=lambda: True) -> str | None:
        try:
            session_path = await mgr.call_get_session_by_pid(os.getpid())
        except Exception:
            # systemd user services live outside the session scope. The
            # launcher imports XDG_SESSION_ID; resolve it through logind and
            # verify ownership before subscribing to its signals.
            session_id = os.environ.get("XDG_SESSION_ID")
            session_path = None
            if session_id:
                try:
                    session_path = await mgr.call_get_session(session_id)
                except Exception:
                    log.warning("XDG_SESSION_ID=%s no longer resolves; finding active session",
                                session_id)
            if session_path is None:
                # A long-lived user service retains an old environment after
                # login-session replacement. Choose only one active, local
                # Wayland seat session owned by this uid; ambiguity degrades
                # lid locking instead of binding an arbitrary session.
                candidates = []
                for _, uid, _, seat, path in await mgr.call_list_sessions():
                    if uid != os.getuid() or not seat:
                        continue
                    intro = await bus.introspect("org.freedesktop.login1", path)
                    obj = bus.get_proxy_object("org.freedesktop.login1", path, intro)
                    candidate = obj.get_interface("org.freedesktop.login1.Session")
                    actual_uid, _ = await candidate.get_user()
                    if (actual_uid == os.getuid() and await candidate.get_active()
                            and await candidate.get_type() == "wayland"
                            and not await candidate.get_remote()):
                        candidates.append(path)
                if len(candidates) != 1:
                    log.warning("cannot identify one owned active Wayland session (%d candidates); "
                                "session lock unavailable", len(candidates))
                    return None
                session_path = candidates[0]
        intro = await bus.introspect("org.freedesktop.login1", session_path)
        obj = bus.get_proxy_object("org.freedesktop.login1", session_path, intro)
        sess = obj.get_interface("org.freedesktop.login1.Session")
        uid, _ = await sess.get_user()
        if uid != os.getuid():
            log.error("refusing logind session owned by uid=%s", uid)
            return

        def on_lock() -> None:
            if is_active() and self._lock_on_lid:
                try:
                    self._on_lock(REASON_LID)
                except Exception:
                    log.exception("on_lock callback raised")

        def on_unlock() -> None:
            if is_active() and self._on_unlock is not None:
                try:
                    self._on_unlock()
                except Exception:
                    log.exception("on_unlock callback raised")

        sess.on_lock(on_lock)
        if subscriptions is not None:
            subscriptions.append((sess, "lock", on_lock))
        if self._on_unlock is not None:
            sess.on_unlock(on_unlock)
            if subscriptions is not None:
                subscriptions.append((sess, "unlock", on_unlock))
        log.info("logind session=%s lid_lock=%s", session_path, self._lock_on_lid)
        return session_path
