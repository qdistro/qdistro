"""qdwin_locker_v1 client.

Pure pywayland — the locker holds its OWN wl_display connection,
separate from Qt's. Earlier drafts tried to share Qt's display so the
Qt-owned wl_surface could be passed to `attach_lock_surface`, but the
PySide6 native-interface bridge for raw `wl_display*` extraction is
brittle (relies on `sip` which is PyQt-only and a nonexistent
`pywayland.WlSurface.from_native`). The pragmatic shape: pywayland
handles all protocol traffic + the lock surface (shm-backed), and the
Qt QML surface is a separate render that is NOT the LOCK-layer
surface. See doc/wayland-bridge.md for the longer-term plan to
unify.

What this module owns:

- The wl_display + wl_registry on the WAYLAND_DISPLAY socket.
- The qdwin_locker_v1 global binding + bind_as_locker call.
- A wl_compositor handle for creating the lock wl_surface.
- The lock surface (currently a 1×1 placeholder, fully transparent;
  the visible UI is rendered by Qt elsewhere). The compositor pins
  this on the LOCK layer and refuses to render anything else while
  set_locked(1) is in effect.
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
from dataclasses import dataclass
from typing import Callable

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
        self._compositor = None
        self._locker = None
        self._lock_surface = None
        self._lock_handle = None
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
        """Connect, bind, attach a placeholder lock surface. Returns
        True on success."""
        from pywayland.client import Display
        from pywayland.protocol.wayland import WlCompositor

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
        if "wl_compositor" not in self._globals:
            log.error("wl_compositor not advertised")
            return False

        c_name, c_ver = self._globals["wl_compositor"]
        self._compositor = self._registry.bind(c_name, WlCompositor, min(c_ver, 4))

        from .protocol.qdwin_locker_v1 import QdwinLockerV1
        l_name, l_ver = self._globals["qdwin_locker_v1"]
        self._locker = self._registry.bind(l_name, QdwinLockerV1, l_ver)
        self._locker.dispatcher["ready"] = self._on_ready
        self._locker.dispatcher["locked_changed"] = self._on_locked_changed
        self._locker.dispatcher["lock_requested"] = self._on_lock_requested
        self._locker.dispatcher["overlay_key"] = self._on_overlay_key
        self._locker.bind_as_locker()
        # Two roundtrips — first to flush bind_as_locker, second to
        # observe the ready event. Without this, attach_lock_surface
        # can fire before the compositor accepts the locker role and
        # races against bind_qdwin_locker's uid filter.
        self._display.roundtrip()
        self._display.roundtrip()
        if not self._bound.is_set():
            log.error("locker bound but ready event never arrived")
            return False

        # Placeholder lock surface — 1×1 transparent. Replace with
        # the rendered UI surface once the Qt bridge is in place.
        self._lock_surface = self._compositor.create_surface()
        self._lock_handle = self._locker.attach_lock_surface(self._lock_surface)
        self._lock_handle.dispatcher["configure"] = self._on_configure
        self._display.roundtrip()
        log.info("locker connected, surface attached")

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

    def _on_configure(self, resource, width: int, height: int, serial: int) -> None:
        log.info("lock-surface configure %dx%d serial=%d", width, height, serial)
        # We're already on the poll thread here; serialize anyway so
        # a main-thread `set_locked` can't sneak in mid-emit.
        with self._display_lock:
            try:
                resource.ack_configure(serial)
                if self._display:
                    self._display.flush()
            except Exception:
                log.exception("ack_configure failed")

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
                        # select() guarantees the fd is readable, so
                        # dispatch(block=True) reads the kernel buffer
                        # and dispatches in one shot. dispatch(block=False)
                        # only drains the already-queued event ring —
                        # it does NOT read the fd, leaving wire bytes
                        # buffered forever and `idled` never delivered.
                        self._display.dispatch(block=True)
            except Exception:
                log.exception("wayland poll loop error")
                break
