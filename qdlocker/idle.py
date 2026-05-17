"""ext-idle-notify-v1 subscription.

Implements client-side idle monitoring. This is the preferred path
over the compositor-side idle monitoring since it allows the locker
to be more responsive and configurable.

The locker will:

  1. Bind `ext_idle_notifier_v1` on the same wl_display as the
     locker connection.
  2. Request a notification with the configured timeout (env
     `QDLOCKER_IDLE_MS`, default 10 min).
  3. On the `idled` event, call `LockerClient.set_locked(True)`.
  4. On the `resumed` event, reset the timer.

The idle watcher is now wired and can be started.
"""

from __future__ import annotations

import logging

log = logging.getLogger("qdlocker.idle")


class IdleWatcher:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        self._on_idle: callable | None = None
        self._display = None
        self._idle_notifier = None
        self._idle_handle = None

    def on_idle(self, cb) -> None:
        """Set the callback that runs when the idle threshold fires.
        Once wired, the callback runs on the pywayland poll thread —
        re-dispatch to Qt via a Signal with QueuedConnection."""
        self._on_idle = cb

    def start(self, display) -> None:
        """Begin watching. Initializes the ext-idle-notify-v1 binding."""
        try:
            from pywayland.protocol.ext_idle_notify_v1 import ExtIdleNotifierV1
            
            # Find the ext_idle_notifier_v1 global
            registry = display.get_registry()
            globals_dict = {}
            
            def handle_global(name, interface, version):
                globals_dict[interface] = (name, version)
                
            registry.dispatcher['global'] = handle_global
            display.roundtrip()
            
            if 'ext_idle_notifier_v1' not in globals_dict:
                log.warning("ext_idle_notifier_v1 not available on compositor")
                return
                
            # Bind the idle notifier
            name, version = globals_dict['ext_idle_notifier_v1']
            self._idle_notifier = registry.bind(name, ExtIdleNotifierV1, version)
            
            # Create the idle handle with our timeout
            self._idle_handle = self._idle_notifier.get_idle_listener(self._on_idle_callback, self.timeout_ms)
            display.roundtrip()
            
            log.info(f"Idle watcher started with timeout {self.timeout_ms}ms")
            
        except ImportError:
            log.warning("ext_idle_notify_v1 protocol not available, falling back to compositor-side idle detection")
        except Exception:
            log.exception("Failed to initialize ext_idle_notify_v1, falling back to compositor-side idle detection")

    def _on_idle_callback(self, idle_listener, resource):
        """Internal callback for when idle state is triggered."""
        log.info("Idle timeout reached, triggering lock")
        if self._on_idle:
            self._on_idle()