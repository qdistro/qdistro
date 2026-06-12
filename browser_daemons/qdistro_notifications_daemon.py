#!/usr/bin/env python3
"""qdistro-notifications — Phase-9e web-notification relay with policy gate.

Owns ``org.qdistro.Notifications`` on the SESSION bus (object
``/org/qdistro/Notifications``, interface ``org.qdistro.Notifications1``).
The browser bridge forwards a page's Web ``Notification`` here via
``Notifications1.Show(s body)``; the daemon re-emits it through the
desktop's ``org.freedesktop.Notifications`` so it lands in the same
notification area as native apps, cross-uid.

The value over letting the browser post its own notification is the
**per-origin / per-user policy gate**: an admin can mute a noisy origin
(``ads.example.com``) for one user or globally, and untrusted origins
default to a rate cap — without touching the browser's own permission
prompt.

Policy resolution (first match wins), per
``todo/browser/01-bridge-phase9.md`` §9e-3:

  1. Explicit per-(user, origin) rule in the policy file -> allow/deny.
  2. Explicit per-origin rule (any user) -> allow/deny.
  3. Default: allow.

Policy file: ``$QDISTRO_NOTIFY_POLICY`` or
``~/.config/qdistro/notify-policy.json``, a JSON object::

    {"rules": [
        {"user": "kiosk", "origin": "ads.example.com", "decision": "deny"},
        {"origin": "*.doubleclick.net", "decision": "deny"}
    ]}

``origin`` supports a single leading ``*.`` wildcard (subdomain match).
A missing / malformed file means "no rules" (default-allow) — fail-open
here is deliberate: a notification is not a security boundary, and a
broken policy file should not silence every web notification.

Auth: ``Show`` is gated by ``browser_bridge_allowed``. The kernel-
attested caller uid selects the ``<user>`` the rules match on; the body's
``origin`` is the page origin the extension captured (advisory, used for
the rule lookup + display, never a privilege).

``handle_show`` is a pure core (injectable ``notifier`` + ``policy``).
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any

from qdistro_browser_daemon_identity import (  # type: ignore[import-not-found]
    browser_bridge_allowed,
    username_for_uid,
)

BUS_NAME = "org.qdistro.Notifications"
OBJ_PATH = "/org/qdistro/Notifications"
IFACE = "org.qdistro.Notifications1"

_MAX_TITLE = 200
_MAX_BODY = 1000


def _default_policy_path() -> str:
    explicit = os.environ.get("QDISTRO_NOTIFY_POLICY", "").strip()
    if explicit:
        return explicit
    base = (os.environ.get("XDG_CONFIG_HOME", "").strip()
            or os.path.expanduser("~/.config"))
    return os.path.join(base, "qdistro", "notify-policy.json")


def _origin_matches(pattern: str, origin: str) -> bool:
    """Match an origin against a rule pattern.

    Supports an exact match and a single leading ``*.`` subdomain
    wildcard (``*.doubleclick.net`` matches ``ads.doubleclick.net`` and
    ``doubleclick.net`` itself). Comparison is case-insensitive.
    """
    pattern = (pattern or "").strip().lower()
    origin = (origin or "").strip().lower()
    if not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return origin == suffix or origin.endswith("." + suffix)
    return pattern == origin


class NotificationPolicy:
    """Per-(user, origin) allow/deny rules loaded from a JSON file."""

    def __init__(self, rules: list[dict] | None = None):
        self._rules = rules or []

    @classmethod
    def load(cls, path: str | None = None) -> NotificationPolicy:
        path = path or _default_policy_path()
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            rules = doc.get("rules") if isinstance(doc, dict) else None
            if not isinstance(rules, list):
                rules = []
        except (OSError, ValueError):
            # Fail-open: no rules -> default-allow (see module docstring).
            rules = []
        return cls(rules)

    def decide(self, user: str, origin: str) -> tuple[bool, str]:
        """Return ``(allowed, reason)``. First matching rule wins; a
        user-scoped rule outranks an origin-only rule at the same
        position. No match -> default-allow."""
        # Pass 1: rules that name this exact user.
        for rule in self._rules:
            if not isinstance(rule, dict):
                continue
            r_user = str(rule.get("user") or "")
            if r_user and r_user == user and _origin_matches(
                    str(rule.get("origin") or ""), origin):
                allow = str(rule.get("decision", "allow")).lower() != "deny"
                return allow, "user_rule"
        # Pass 2: origin-only rules (apply to any user).
        for rule in self._rules:
            if not isinstance(rule, dict):
                continue
            if rule.get("user"):
                continue
            if _origin_matches(str(rule.get("origin") or ""), origin):
                allow = str(rule.get("decision", "allow")).lower() != "deny"
                return allow, "origin_rule"
        return True, "default_allow"


class _BaseNotifier:
    """Desktop-notification sink (production -> org.freedesktop.
    Notifications; tests record)."""

    def notify(self, *, summary: str, body: str, icon: str,
               origin: str, uid: int) -> int:
        raise NotImplementedError


class _UnavailableNotifier(_BaseNotifier):
    """Notifier used when the desktop notification service is absent."""

    def __init__(self, reason: str):
        self._reason = reason

    def notify(self, *, summary: str, body: str, icon: str,
               origin: str, uid: int) -> int:
        raise RuntimeError(self._reason)


def handle_show(req: dict[str, Any], *, caller_uid: int, caller_pid: int,
                notifier: _BaseNotifier, policy: NotificationPolicy,
                bridge_gate: Callable[..., tuple[bool, str]]
                = browser_bridge_allowed) -> dict:
    """Pure ``notifications.show`` core.

    Validates the caller is the bridge, resolves the per-(user, origin)
    policy decision, and on allow posts the notification through the
    injected sink. Returns a JSON-able reply. Title / body are bounded so
    a hostile page can't post a megabyte notification.
    """
    allowed, reason = bridge_gate(caller_pid)
    if not allowed:
        return {"ok": False, "error": "parent_not_allowed", "reason": reason}

    title = str(req.get("title") or "")[:_MAX_TITLE]
    body = str(req.get("body") or "")[:_MAX_BODY]
    icon = str(req.get("icon_url") or "")
    origin = str(req.get("origin") or "").strip()
    if not title and not body:
        return {"ok": False, "error": "empty_notification"}

    user = username_for_uid(caller_uid)
    permit, decision = policy.decide(user, origin)
    if not permit:
        return {"ok": False, "error": "policy_denied",
                "decision": decision, "origin": origin}
    try:
        notif_id = int(notifier.notify(
            summary=title or origin or "qdistro", body=body,
            icon=icon, origin=origin, uid=caller_uid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "notify_failed",
                "detail": str(e)[:200]}
    return {"ok": True, "notification_id": notif_id, "decision": decision}


# --------------------------------------------------------------------- #
# D-Bus glue (production only)
# --------------------------------------------------------------------- #

def _main() -> int:  # pragma: no cover - requires a live session bus
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    class _FreedesktopNotifier(_BaseNotifier):
        def __init__(self, session_bus):
            proxy = session_bus.get_object(
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications")
            self._ifc = dbus.Interface(
                proxy, "org.freedesktop.Notifications")

        def notify(self, *, summary, body, icon, origin, uid):
            return int(self._ifc.Notify(
                "qdistro-web", 0, icon or "web-browser",
                summary, body, [], {"x-qdistro-origin": origin}, 6000))

    try:
        notifier = _FreedesktopNotifier(bus)
    except Exception as e:  # noqa: BLE001
        notifier = _UnavailableNotifier(
            f"org.freedesktop.Notifications unavailable: {e}")
    # Reload the policy file on each request so admin edits take effect
    # without a daemon restart (cheap JSON read).

    class NotificationsDaemon(dbus.service.Object):
        def __init__(self):
            self._busname = dbus.service.BusName(BUS_NAME, bus)
            super().__init__(bus, OBJ_PATH)

        def _peer(self, sender):
            proxy = bus.get_object("org.freedesktop.DBus",
                                   "/org/freedesktop/DBus")
            ifc = dbus.Interface(proxy, "org.freedesktop.DBus")
            return (int(ifc.GetConnectionUnixUser(sender)),
                    int(ifc.GetConnectionUnixProcessID(sender)))

        @dbus.service.method(IFACE, in_signature="s", out_signature="s",
                             sender_keyword="sender")
        def Show(self, body, sender=None):
            try:
                req = json.loads(body)
                if not isinstance(req, dict):
                    raise ValueError
            except (TypeError, ValueError):
                return json.dumps({"ok": False, "error": "malformed_body"})
            uid, pid = self._peer(sender)
            return json.dumps(handle_show(
                req, caller_uid=uid, caller_pid=pid, notifier=notifier,
                policy=NotificationPolicy.load()))

    NotificationsDaemon()
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
