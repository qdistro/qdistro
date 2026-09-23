"""qdwin_locker_v1 client.

Pure pywayland — the locker holds its OWN wl_display connection,
separate from Qt's. pywayland owns the private control protocol
(`set_locked`, `overlay_key`, lock triggers); qdwin identifies the
same process's Qt-owned xdg_toplevel and promotes that real visible
surface to the compositor LOCK layer while locked.

What this module owns:

- The wl_display + wl_registry on the WAYLAND_DISPLAY socket.
- The qdwin_locker_v1 global binding + bind_as_locker call.
- The real visible lock surface is the Qt/QML window, not a
  pywayland-created placeholder. qdwin promotes the Qt toplevel based
  on the authenticated locker process identity.
- The pywayland fd poll loop, run on a worker thread so Qt's event
  loop is free for the QML.

Cross-thread signalling: callbacks fire on the pywayland worker
thread and post into Qt via `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`
so the controller stays single-threaded.
"""

from __future__ import annotations

import logging
import select
import threading
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("qdlocker.wayland")


@dataclass
class LockerEvents:
    """Callbacks invoked on the worker thread. Each callback is
    responsible for re-dispatching to the main thread (typically via
    a `Qt.QueuedConnection` signal)."""

    on_ready: Callable[[bool], None]
    on_locked_changed: Callable[[bool], None]
    on_lock_requested: Callable[[int], None]
    on_overlay_key: Callable[[int, str], None]


class LockerClient:
    def __init__(self, events: LockerEvents) -> None:
        self._events = events
        self._display = None
        self._registry = None
        self._locker = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._globals: dict[str, tuple[int, int]] = {}
        self._bound = threading.Event()  # set after `ready` arrives
        # libwayland's default queue is not safe for concurrent access:
        # main-thread `set_locked` (which writes a request + flush)
        # racing with the poll thread's `dispatch` corrupts the
        # connection state. _display_lock serializes every touch of
        # `self._display` and the bound proxies (`self._locker`,
        # `self._lock_handle`). The lock is fine-grained — only held
        # for the duration of a single request emit + flush.
        self._display_lock = threading.Lock()

    def connect(self) -> bool:
        """Connect and bind qdwin_locker_v1. Returns True on success."""
        from pywayland.client import Display

        try:
            self._display = Display()
            self._display.connect()
        except Exception:
            log.exception("could not connect to WAYLAND_DISPLAY")
            return False

        self._registry = self._display.get_registry()
        self._registry.dispatcher["global"] = self._on_global
        self._display.roundtrip()

        if "qdwin_locker_v1" not in self._globals:
            log.error("qdwin_locker_v1 not advertised by compositor")
            return False

        from .protocol.qdwin_locker_v1 import QdwinLockerV1
        l_name, l_ver = self._globals["qdwin_locker_v1"]
        self._locker = self._registry.bind(l_name, QdwinLockerV1, l_ver)
        self._locker.dispatcher["ready"] = self._on_ready
        self._locker.dispatcher["locked_changed"] = self._on_locked_changed
        self._locker.dispatcher["lock_requested"] = self._on_lock_requested
        self._locker.dispatcher["overlay_key"] = self._on_overlay_key
        self._locker.bind_as_locker()
        # Two roundtrips — first to flush bind_as_locker, second to
        # observe the ready event before the Qt side may request a lock.
        self._display.roundtrip()
        self._display.roundtrip()
        if not self._bound.is_set():
            log.error("locker bound but ready event never arrived")
            return False

        log.info("locker connected")

        self._thread = threading.Thread(
            target=self._poll_loop, name="qdlocker-wayland", daemon=True
        )
        self._thread.start()
        return True

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        # Even if the join timed out, take the lock so we don't
        # close the display fd while the worker is mid-dispatch.
        with self._display_lock:
            if self._display:
                try:
                    self._display.disconnect()
                except Exception:
                    pass
                self._display = None

    def set_locked(self, locked: bool) -> None:
        with self._display_lock:
            if not self._locker:
                log.error("set_locked called before connect")
                return
            self._locker.set_locked(1 if locked else 0)
            if self._display:
                self._display.flush()

    def lock_acknowledged(self, reason: int) -> None:
        with self._display_lock:
            if not self._locker:
                return
            self._locker.lock_acknowledged(reason)
            if self._display:
                self._display.flush()

    # ----- dispatcher callbacks (called on poll thread) -----

    def _on_global(self, _registry, name: int, interface: str, version: int) -> None:
        self._globals[interface] = (name, version)

    def _on_ready(self, _resource, initially_locked: int) -> None:
        self._bound.set()
        self._events.on_ready(bool(initially_locked))

    def _on_locked_changed(self, _resource, locked: int) -> None:
        self._events.on_locked_changed(bool(locked))

    def _on_lock_requested(self, _resource, reason: int) -> None:
        self._events.on_lock_requested(reason)

    def _on_overlay_key(self, _resource, sym: int, utf8: str) -> None:
        self._events.on_overlay_key(sym, utf8)

    # ----- poll loop -----

    def _poll_loop(self) -> None:
        # Capture fd outside the lock — `get_fd` returns a stable int.
        fd = self._display.get_fd()
        while not self._stop.is_set():
            try:
                # Flush under the lock so a main-thread request +
                # flush can't interleave half a wire message with
                # ours. Then drop the lock during the select so we
                # don't block other threads on i/o.
                with self._display_lock:
                    if self._display is None:
                        return
                    self._display.flush()
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    with self._display_lock:
                        if self._display is None:
                            return
                        # Read the fd and drain pending events without
                        # wl_display_dispatch()'s blocking path. Holding
                        # _display_lock while dispatch(block=True) can
                        # starve main-thread requests such as set_locked(1):
                        # after a restart the locker would update its local
                        # state but never flush the request to qdwin.
                        self._display.read()
                        self._display.dispatch(block=False)
            except Exception:
                log.exception("wayland poll loop error")
                break
