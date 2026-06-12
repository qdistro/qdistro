#!/usr/bin/env python3
"""qdistro-compositor — Phase-9e screen-lock inhibit/release daemon.

Owns ``org.qdistro.Compositor`` on the SESSION bus (object
``/org/qdistro/Compositor``, interface ``org.qdistro.Compositor1``). The
browser bridge forwards ``screenlock.inhibit`` / ``screenlock.release``
here via ``Compositor1.ScreenlockInhibit(s body)`` /
``Compositor1.ScreenlockRelease(s body)`` when a tab enters / leaves
fullscreen video or presentation mode.

The daemon translates these into a real idle/lock inhibitor held against
``org.freedesktop.login1`` (logind ``Inhibit("idle", ...)`` returning a
fd that is released by closing it). One inhibitor is held per
(uid, tab_id) source; ScreenlockRelease drops the matching one, and a
disappearing source is swept after :data:`INHIBIT_TTL_S` so a crashed
tab can't pin the screen on forever.

**Policy gate** (this is the privileged op — keeping a multi-user box
awake affects everyone): an inhibit is honoured only when the request's
``reason`` is in the allowlist (``fullscreen_video`` / ``presentation``
by default) AND the per-uid inhibitor count is under the cap. Admins
widen / narrow the allowlist via ``$QDISTRO_COMPOSITOR_INHIBIT_REASONS``.
A denied inhibit returns ``policy_denied`` and holds nothing.

Auth: both methods are gated by ``browser_bridge_allowed``; the kernel-
attested caller uid keys the inhibitor map so user A can't release user
B's inhibitor.

``handle_inhibit`` / ``handle_release`` are pure cores over an injectable
``inhibitor`` sink + an :class:`InhibitState`, unit-testable without a
bus or logind.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Callable

from qdistro_browser_daemon_identity import (  # type: ignore[import-not-found]
    browser_bridge_allowed,
)

BUS_NAME = "org.qdistro.Compositor"
OBJ_PATH = "/org/qdistro/Compositor"
IFACE = "org.qdistro.Compositor1"

# Reasons an inhibit is allowed for. Overridable by the admin.
ALLOWED_REASONS = frozenset(
    p for p in os.environ.get(
        "QDISTRO_COMPOSITOR_INHIBIT_REASONS",
        "fullscreen_video:presentation:playing_audio").split(":") if p)

# Per-uid cap on simultaneously-held inhibitors. A buggy / hostile tab
# loop can't exhaust the inhibitor table.
MAX_INHIBITS_PER_UID = int(
    os.environ.get("QDISTRO_COMPOSITOR_MAX_INHIBITS", "8"))

# A held inhibitor older than this with no refresh is reclaimed.
INHIBIT_TTL_S = float(os.environ.get("QDISTRO_COMPOSITOR_TTL_S", "14400"))


def _source_key(uid: int, tab_id: Any) -> str:
    return f"{int(uid)}:{tab_id if tab_id is not None else '_'}"


class _BaseInhibitor:
    """Idle/lock-inhibitor sink. Production holds a logind ``Inhibit``
    fd; tests record acquire/release. ``acquire`` returns an opaque
    handle the daemon stores and later passes back to ``release``."""

    def acquire(self, *, uid: int, reason: str, who: str) -> Any:
        raise NotImplementedError

    def release(self, handle: Any) -> None:
        raise NotImplementedError


class InhibitState:
    """Tracks held inhibitors keyed by (uid, tab_id). Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        # key -> {"handle": Any, "uid": int, "created": float}
        self._held: dict[str, dict] = {}

    def count_for_uid(self, uid: int) -> int:
        with self._lock:
            return sum(1 for e in self._held.values()
                       if e["uid"] == int(uid))

    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._held.get(key)

    def put(self, key: str, handle: Any, uid: int, now: float) -> None:
        with self._lock:
            self._held[key] = {"handle": handle, "uid": int(uid),
                               "created": now}

    def pop(self, key: str) -> dict | None:
        with self._lock:
            return self._held.pop(key, None)

    def sweep(self, now: float, ttl_s: float) -> list[dict]:
        """Remove entries older than ttl_s; return them so the caller can
        release their handles."""
        with self._lock:
            dead_keys = [k for k, e in self._held.items()
                         if now - e["created"] > ttl_s]
            return [self._held.pop(k) for k in dead_keys]


def handle_inhibit(req: dict[str, Any], *, caller_uid: int, caller_pid: int,
                   inhibitor: _BaseInhibitor, state: InhibitState,
                   now_fn: Callable[[], float] = time.time,
                   bridge_gate: Callable[..., tuple[bool, str]]
                   = browser_bridge_allowed) -> dict:
    """Pure ``screenlock.inhibit`` core.

    Policy: reason must be allowlisted and the per-uid cap must not be
    exceeded. Re-inhibiting an already-held (uid, tab_id) source is
    idempotent (refreshes the timestamp, keeps one handle). Returns a
    JSON-able reply. Fails closed when the caller is not the bridge.
    """
    allowed, reason_tag = bridge_gate(caller_pid)
    if not allowed:
        return {"ok": False, "error": "parent_not_allowed",
                "reason": reason_tag}

    reason = str(req.get("reason") or "").strip()
    if reason not in ALLOWED_REASONS:
        return {"ok": False, "error": "policy_denied", "reason": reason}

    key = _source_key(caller_uid, req.get("tab_id"))
    existing = state.get(key)
    if existing is not None:
        # Idempotent refresh — keep the single held inhibitor.
        state.put(key, existing["handle"], caller_uid, now_fn())
        return {"ok": True, "inhibited": True, "refreshed": True}

    if state.count_for_uid(caller_uid) >= MAX_INHIBITS_PER_UID:
        return {"ok": False, "error": "inhibit_cap_exceeded"}

    who = f"qdistro-browser:{reason}"
    try:
        handle = inhibitor.acquire(uid=caller_uid, reason=reason, who=who)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "inhibit_failed",
                "detail": str(e)[:200]}
    state.put(key, handle, caller_uid, now_fn())
    return {"ok": True, "inhibited": True, "refreshed": False}


def handle_release(req: dict[str, Any], *, caller_uid: int, caller_pid: int,
                   inhibitor: _BaseInhibitor, state: InhibitState,
                   bridge_gate: Callable[..., tuple[bool, str]]
                   = browser_bridge_allowed) -> dict:
    """Pure ``screenlock.release`` core.

    Drops the inhibitor for the caller's (uid, tab_id) source. A release
    with no matching held inhibitor is a no-op success (idempotent). The
    (uid, tab_id) key means user A can never release user B's inhibitor —
    the uid is kernel-attested, not from the body.
    """
    allowed, reason_tag = bridge_gate(caller_pid)
    if not allowed:
        return {"ok": False, "error": "parent_not_allowed",
                "reason": reason_tag}
    key = _source_key(caller_uid, req.get("tab_id"))
    entry = state.pop(key)
    if entry is None:
        return {"ok": True, "released": False}
    try:
        inhibitor.release(entry["handle"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "release_failed",
                "detail": str(e)[:200]}
    return {"ok": True, "released": True}


# --------------------------------------------------------------------- #
# D-Bus glue (production only)
# --------------------------------------------------------------------- #

def _main() -> int:  # pragma: no cover - requires a live session bus + logind
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    session_bus = dbus.SessionBus()
    system_bus = dbus.SystemBus()

    class _LogindInhibitor(_BaseInhibitor):
        """Holds an ``idle`` inhibitor against logind. The returned fd
        must stay open for the inhibit to hold; we stash it in the
        handle and close it on release."""

        def __init__(self, sys_bus):
            proxy = sys_bus.get_object(
                "org.freedesktop.login1", "/org/freedesktop/login1")
            self._mgr = dbus.Interface(
                proxy, "org.freedesktop.login1.Manager")

        def acquire(self, *, uid, reason, who):
            fd = self._mgr.Inhibit("idle", who, reason, "block")
            # fd is a dbus UnixFd; take() yields an int we own.
            return fd.take() if hasattr(fd, "take") else int(fd)

        def release(self, handle):
            try:
                os.close(int(handle))
            except OSError:
                pass

    inhibitor = _LogindInhibitor(system_bus)
    state = InhibitState()

    def _sweep_tick():
        for entry in state.sweep(time.time(), INHIBIT_TTL_S):
            try:
                inhibitor.release(entry["handle"])
            except Exception:  # noqa: BLE001
                pass
        return True

    GLib.timeout_add_seconds(60, _sweep_tick)

    class CompositorDaemon(dbus.service.Object):
        def __init__(self):
            self._busname = dbus.service.BusName(BUS_NAME, session_bus)
            super().__init__(session_bus, OBJ_PATH)

        def _peer(self, sender):
            proxy = session_bus.get_object(
                "org.freedesktop.DBus", "/org/freedesktop/DBus")
            ifc = dbus.Interface(proxy, "org.freedesktop.DBus")
            return (int(ifc.GetConnectionUnixUser(sender)),
                    int(ifc.GetConnectionUnixProcessID(sender)))

        def _parse(self, body, sender):
            try:
                req = json.loads(body)
                if not isinstance(req, dict):
                    raise ValueError
            except (TypeError, ValueError):
                return None, None, None
            uid, pid = self._peer(sender)
            return req, uid, pid

        @dbus.service.method(IFACE, in_signature="s", out_signature="s",
                             sender_keyword="sender")
        def ScreenlockInhibit(self, body, sender=None):
            req, uid, pid = self._parse(body, sender)
            if req is None:
                return json.dumps({"ok": False, "error": "malformed_body"})
            return json.dumps(handle_inhibit(
                req, caller_uid=uid, caller_pid=pid,
                inhibitor=inhibitor, state=state))

        @dbus.service.method(IFACE, in_signature="s", out_signature="s",
                             sender_keyword="sender")
        def ScreenlockRelease(self, body, sender=None):
            req, uid, pid = self._parse(body, sender)
            if req is None:
                return json.dumps({"ok": False, "error": "malformed_body"})
            return json.dumps(handle_release(
                req, caller_uid=uid, caller_pid=pid,
                inhibitor=inhibitor, state=state))

    CompositorDaemon()
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
