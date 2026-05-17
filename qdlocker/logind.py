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

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception("logind watcher loop crashed")

    async def _main(self) -> None:
        try:
            from dbus_next.aio import MessageBus
            from dbus_next import BusType
        except ImportError:
            log.warning("dbus-next not installed; logind lid/suspend lock unavailable")
            return

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
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

            try:
                session_path = await mgr.call_get_session_by_pid(  # type: ignore[attr-defined]
                    os.getpid()
                )
            except Exception:
                log.exception("logind GetSessionByPID failed; lid lock unavailable")
                return

            log.info("logind session=%s", session_path)

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

            # Subscribe to PrepareForSleep(start) so we lock BEFORE suspend.
            def _on_prepare_for_sleep(start: bool) -> None:
                if not start:
                    return
                log.info("logind PrepareForSleep(start=True) -> reason=suspend")
                try:
                    self._on_lock(REASON_SUSPEND)
                except Exception:
                    log.exception("on_lock callback raised")

            try:
                mgr.on_prepare_for_sleep(_on_prepare_for_sleep)  # type: ignore[attr-defined]
            except Exception:
                log.exception("could not subscribe to PrepareForSleep")

            log.info("logind watcher ready")
            await self._stop_event.wait()
        finally:
            try:
                await bus.disconnect()
            except Exception:
                pass
