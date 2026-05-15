"""ext-idle-notify-v1 subscription.

**Status: not wired.** This module is a placeholder. Idle-driven lock
fires today only via the compositor-side path (qdwin watches
ext-idle-notify itself and fans `lock_requested(reason=0=idle)` to
the locker over `qdwin_locker_v1`). Direct client-side subscription
will move here when the bridge to share `LockerClient._display` with
a second pywayland proxy is in place.

When this is wired, the locker will:

  1. Bind `ext_idle_notifier_v1` on the same wl_display as the
     locker connection.
  2. Request a notification with the configured timeout (env
     `QDLOCKER_IDLE_MS`, default 10 min).
  3. On the `idled` event, call `LockerClient.set_locked(True)`.
  4. On the `resumed` event, reset the timer.

For now, importers must NOT call `start()` — the method raises
`NotImplementedError` to keep the half-wired state from silently
no-op'ing.
"""

from __future__ import annotations

import logging

log = logging.getLogger("qdlocker.idle")


class IdleWatcher:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        self._on_idle: callable | None = None

    def on_idle(self, cb) -> None:
        """Set the callback that runs when the idle threshold fires.
        Once wired, the callback runs on the pywayland poll thread —
        re-dispatch to Qt via a Signal with QueuedConnection."""
        self._on_idle = cb

    def start(self, display) -> None:
        """Begin watching. Raises until the implementation lands so
        the half-wired state can't silently no-op."""
        raise NotImplementedError(
            "qdlocker idle watcher is not implemented yet; "
            "the compositor's lock_requested(reason=0=idle) path is the "
            "current way to get idle-driven locks. See qdlocker/idle.py."
        )
