"""Freedesktop / qdshell notification helper.

qdshell (the Quickshell-based sibling shell at
``../../qdshell``) consumes ``org.freedesktop.Notifications`` via
``NotificationServer``. So posting via the standard D-Bus interface
is the integration — there's no separate qdshell-specific channel
to talk to, and any other notification daemon (mako, dunst, plasma)
will display the same payload.

We talk D-Bus through :mod:`PyQt6.QtDBus`, which ships with PyQt6,
so no new runtime dependency is added.

The :func:`notify` function returns the daemon-assigned notification
ID. Pass that ID back as ``replaces_id`` on a follow-up call to
update the same notification in-place — that's how we deliver "live"
file-operation progress without spamming the history.

If the session bus is unavailable (headless CI, no daemon) every
call becomes a no-op that returns ``0``.

Per the spec, the ``hints`` dict may include a ``value`` entry
(int 0-100); the well-known progress hint key. qdshell currently
ignores it for visual progress, but mako/dunst/plasma honour it,
and qdshell silently keeps it in the notification history.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from PyQt6.QtCore import QVariant
from PyQt6.QtDBus import QDBusConnection, QDBusInterface

log = logging.getLogger(__name__)


_BUS_SERVICE = "org.freedesktop.Notifications"
_BUS_PATH = "/org/freedesktop/Notifications"
_BUS_IFACE = "org.freedesktop.Notifications"


_iface: QDBusInterface | None = None


def _connect() -> QDBusInterface | None:
    """Return a session-bus interface, or ``None`` if unavailable."""
    global _iface
    if _iface is not None:
        return _iface if _iface.isValid() else None
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        log.debug("session bus not connected; qdshell notifications disabled")
        return None
    iface = QDBusInterface(_BUS_SERVICE, _BUS_PATH, _BUS_IFACE, bus)
    if not iface.isValid():
        log.debug("Notifications interface not valid; daemon not running?")
        return None
    _iface = iface
    return _iface


def notify(summary: str, body: str = "", *,
           replaces_id: int = 0,
           icon: str = "",
           app_name: str = "QFileMan",
           urgency: int = 1,
           value: int | None = None,
           timeout_ms: int = -1,
           actions: Iterable[str] = ()) -> int:
    """Post a desktop notification. Returns the daemon-assigned ID.

    Pass the returned ID as ``replaces_id`` to update the same notification.
    If the session bus or daemon isn't available the call is a no-op
    that returns ``0`` — callers can use the returned ID unconditionally
    (replacing 0 just creates a new notification next time).

    ``value`` (0-100) is sent as the standard ``value`` progress hint.
    ``urgency`` is 0 (low), 1 (normal), 2 (critical).
    """
    iface = _connect()
    if iface is None:
        return 0

    hints: dict = {
        "urgency": QVariant(urgency).toByteArray()[:1]
        if False else urgency,  # urgency wants a byte; QtDBus marshals int fine
    }
    if value is not None:
        # Clamp; spec is open-ended but daemons treat 0-100 as percentage.
        hints["value"] = max(0, min(100, int(value)))

    reply = iface.call(
        "Notify",
        app_name,
        int(replaces_id),
        icon,
        summary,
        body,
        list(actions),
        hints,
        int(timeout_ms),
    )
    args = reply.arguments() if reply is not None else []
    if not args:
        return 0
    try:
        return int(args[0])
    except (TypeError, ValueError):
        return 0


def close(notification_id: int) -> None:
    """Ask the daemon to dismiss ``notification_id`` early."""
    if not notification_id:
        return
    iface = _connect()
    if iface is None:
        return
    iface.call("CloseNotification", int(notification_id))


def format_started(title: str) -> tuple[str, str]:
    """Return ``(summary, body)`` for a 'job started' notification."""
    return (title, "Started…")


def format_finished(title: str, exit_code: int | None) -> tuple[str, str, int]:
    """Return ``(summary, body, urgency)`` for a finished job.

    ``exit_code`` of ``None`` means the process didn't reach a finish
    handler (e.g. the user closed the dialog mid-run, or QProcess
    couldn't even spawn the program).
    """
    if exit_code == 0:
        return (title, "Done.", 1)
    if exit_code is None:
        return (title, "Cancelled or failed to start.", 1)
    return (title, f"Failed (exit {exit_code}).", 2)
