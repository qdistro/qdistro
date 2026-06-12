"""ext-idle-notify-v1 subscription.

Implements client-side idle monitoring on the same wl_display as the
locker connection. The idle watcher:

  1. Binds `ext_idle_notifier_v1` and `wl_seat` via the registry.
  2. Calls `get_idle_notification(timeout_ms, seat)` to receive an
     `ext_idle_notification_v1` proxy.
  3. Registers `idled` and `resumed` dispatcher entries.
  4. On `idled`, invokes the user-supplied callback (which should
     re-dispatch to Qt via a `QueuedConnection` signal — the
     callback runs on the pywayland poll thread).
  5. On `resumed`, logs and optionally cancels a pending lock.

All wl_display interactions are serialized via the `LockerClient`'s
`_display_lock` so the main thread's `start()` does not race the poll
thread's `dispatch()`.

Errors during initialization propagate to the caller so misconfiguration
is loud — earlier drafts swallowed every exception which masked a
broken `get_idle_listener` call (the real API is
`get_idle_notification`). If the compositor does not advertise
`ext_idle_notifier_v1` the watcher logs at WARNING and exits cleanly;
all other failures raise.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger("qdlocker.idle")


class IdleWatcher:
    def __init__(self, timeout_ms: int) -> None:
        if timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be > 0, got {timeout_ms}")
        self.timeout_ms = timeout_ms
        self._on_idle: Callable[[], None] | None = None
        self._on_resume: Callable[[], None] | None = None
        self._display = None
        self._display_lock = None
        self._idle_notifier = None
        self._idle_handle = None
        self._registry = None
        self._seat = None
        self._available = False

    def on_idle(self, cb: Callable[[], None]) -> None:
        """Set the callback fired when the idle threshold elapses.

        The callback runs on the pywayland poll thread — implementations
        should re-dispatch onto the Qt main thread via a Signal with
        `Qt.QueuedConnection`. The wrapper here catches and logs any
        exception so a transient bug in the callback cannot kill the
        poll loop and silently disable future idle events.
        """
        self._on_idle = cb

    def on_resume(self, cb: Callable[[], None]) -> None:
        """Set the callback fired when the user resumes activity."""
        self._on_resume = cb

    def start(self, display, display_lock=None) -> bool:
        """Begin watching. Returns True if the ext_idle_notifier_v1
        global was bound and the notification was requested
        successfully; False if the compositor does not advertise the
        protocol. Raises on any other failure.

        `display_lock` must be the `LockerClient._display_lock` if the
        display is shared with a poll thread. When passed, this method
        acquires it for the entire roundtrip + bind sequence so the
        poll thread does not concurrently dispatch on the same fd.
        """
        from pywayland.protocol.ext_idle_notify_v1 import ExtIdleNotifierV1
        from pywayland.protocol.wayland import WlSeat

        self._display = display
        self._display_lock = display_lock

        # Hold the display lock for the entire bind sequence — the
        # poll thread otherwise races with our roundtrip and the
        # default queue corrupts.
        if display_lock is not None:
            display_lock.acquire()
        try:
            registry = display.get_registry()
            self._registry = registry
            globals_dict: dict = {}

            def handle_global(reg, name, interface, version):
                globals_dict[interface] = (name, version)

            registry.dispatcher["global"] = handle_global
            display.roundtrip()

            if "ext_idle_notifier_v1" not in globals_dict:
                log.warning("ext_idle_notifier_v1 not advertised by compositor; "
                            "idle auto-lock unavailable")
                return False
            if "wl_seat" not in globals_dict:
                log.warning("wl_seat not advertised; idle auto-lock unavailable")
                return False

            seat_name, seat_ver = globals_dict["wl_seat"]
            self._seat = registry.bind(seat_name, WlSeat, min(seat_ver, 7))

            notif_name, notif_ver = globals_dict["ext_idle_notifier_v1"]
            self._idle_notifier = registry.bind(
                notif_name, ExtIdleNotifierV1, min(notif_ver, 1)
            )
            self._idle_handle = self._idle_notifier.get_idle_notification(
                self.timeout_ms, self._seat
            )
            self._idle_handle.dispatcher["idled"] = self._handle_idled
            self._idle_handle.dispatcher["resumed"] = self._handle_resumed
            display.roundtrip()

            log.info("idle watcher started; timeout=%dms", self.timeout_ms)
            self._available = True
            return True
        finally:
            if display_lock is not None:
                display_lock.release()

    def stop(self) -> None:
        """Destroy the active idle notification so re-creation produces a
        fresh `idled` cycle. Safe to call multiple times."""
        if self._idle_handle is not None:
            try:
                self._idle_handle.destroy()
            except Exception:
                log.exception("failed to destroy idle notification")
            self._idle_handle = None

    def rearm(self) -> None:
        """After a lock cycle completes, destroy and recreate the
        notification so the next idle period fires `idled` again."""
        if not self._available or self._idle_notifier is None or self._seat is None:
            return
        if self._display_lock is not None:
            self._display_lock.acquire()
        try:
            self.stop()
            self._idle_handle = self._idle_notifier.get_idle_notification(
                self.timeout_ms, self._seat
            )
            self._idle_handle.dispatcher["idled"] = self._handle_idled
            self._idle_handle.dispatcher["resumed"] = self._handle_resumed
            if self._display is not None:
                self._display.flush()
        finally:
            if self._display_lock is not None:
                self._display_lock.release()

    # ----- dispatcher callbacks (run on the pywayland poll thread) -----

    def _handle_idled(self, *_args) -> None:
        log.info("idle threshold reached; triggering lock callback")
        if self._on_idle is None:
            return
        try:
            self._on_idle()
        except Exception:
            # An unhandled exception here would tear down the poll
            # loop and silently disable all future idle events.
            log.exception("idle callback raised")

    def _handle_resumed(self, *_args) -> None:
        log.debug("idle resumed (user activity)")
        if self._on_resume is not None:
            try:
                self._on_resume()
            except Exception:
                log.exception("resume callback raised")
