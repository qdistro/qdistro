#!/usr/bin/env python3
"""qdistro-downloads — Phase-9e browser download-notification daemon.

Owns ``org.qdistro.Downloads`` on the SESSION bus (object
``/org/qdistro/Downloads``, interface ``org.qdistro.Downloads1``). The
browser bridge forwards ``downloads.notify`` here via
``Downloads1.Notify(s body)`` on every
``chrome.downloads.onChanged`` transition (start / progress / complete /
interrupted).

On a *completed* download the daemon raises a desktop notification
("Download finished — <file>"); clicking it opens the file's containing
folder in the **originating user's** file manager. Cross-uid is the
point: the admin watching a multi-user box sees a download that finished
in user B's browser and can jump straight to it.

Two trust subtleties:

  * The notifying caller must be the qdistro browser bridge
    (``browser_bridge_allowed`` — executed-script + allowlisted parent
    browser exe). A random same-uid process can't spoof a download.
  * "Open file location" runs the file manager as the **kernel-attested
    caller uid** (SO_PEERCRED), never as a uid named in the body, and
    only after re-validating that the path lives under that user's home
    / XDG download dir. A compromised bridge can't turn the click into
    "open /etc as root".

``handle_notify`` is a pure core (injectable ``notifier`` sink) so the
dispatch + path-policy unit-test without a bus. ``open_location`` is a
separate pure function gated by :func:`path_allowed_for_uid`.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from qdistro_browser_daemon_identity import (  # type: ignore[import-not-found]
    browser_bridge_allowed, username_for_uid,
)

BUS_NAME = "org.qdistro.Downloads"
OBJ_PATH = "/org/qdistro/Downloads"
IFACE = "org.qdistro.Downloads1"

# chrome.downloads state strings we treat as terminal-complete.
_COMPLETE_STATES = frozenset({"complete", "completed"})


class _BaseNotifier:
    """Desktop-notification sink. Production posts via
    ``org.freedesktop.Notifications``; tests record the call. The
    ``action_key`` lets the daemon wire the "Open folder" button — when
    the user activates it the production notifier calls back into
    :func:`open_location`.
    """

    def notify(self, *, summary: str, body: str, download_id: int,
               path: str, uid: int) -> int:
        raise NotImplementedError


def _home_for_uid(uid: int) -> str:
    import pwd as _pwd
    try:
        return _pwd.getpwuid(int(uid)).pw_dir or ""
    except (KeyError, ValueError):
        return ""


def path_allowed_for_uid(path: str, uid: int,
                         *, home_fn: Callable[[int], str] = _home_for_uid
                         ) -> bool:
    """Decide whether ``path`` is one the originating user may have a file
    manager opened on.

    Policy: the realpath must live under the caller's home directory. The
    browser only ever writes downloads under the user's XDG download dir
    (a child of $HOME), so this both rejects path-traversal
    (``../../etc/shadow``) and a hostile bridge naming another user's
    files. Fails closed when the home dir is unknown.
    """
    if not isinstance(path, str) or not path:
        return False
    home = home_fn(uid)
    if not home:
        return False
    real = os.path.realpath(path)
    home_real = os.path.realpath(home)
    # Anchor on a trailing separator so /home/bob-evil isn't matched by
    # /home/bob.
    return real == home_real or real.startswith(home_real + os.sep)


def handle_notify(req: dict[str, Any], *, caller_uid: int, caller_pid: int,
                  notifier: _BaseNotifier,
                  bridge_gate: Callable[..., tuple[bool, str]]
                  = browser_bridge_allowed) -> dict:
    """Pure ``downloads.notify`` core.

    Only *completed* downloads raise a notification; in-progress / sparse
    deltas are acknowledged without surfacing UI (the admin widget polls
    progress via ``DownloadsList`` on the adapter side). Returns a
    JSON-able reply. Fails closed when the caller is not the bridge.
    """
    allowed, reason = bridge_gate(caller_pid)
    if not allowed:
        return {"ok": False, "error": "parent_not_allowed", "reason": reason}

    state = str(req.get("state") or "").strip().lower()
    try:
        download_id = int(req.get("download_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_download_id"}
    filename = str(req.get("filename") or "")

    if state not in _COMPLETE_STATES:
        # Acknowledge non-terminal updates without UI.
        return {"ok": True, "notified": False, "state": state}

    user = username_for_uid(caller_uid)
    base = os.path.basename(filename) or "download"
    summary = "Download finished"
    body = f"{base} — {user}"
    try:
        notif_id = int(notifier.notify(
            summary=summary, body=body, download_id=download_id,
            path=filename, uid=caller_uid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "notify_failed",
                "detail": str(e)[:200]}
    return {"ok": True, "notified": True, "notification_id": notif_id}


# File-manager binaries we are willing to exec, in preference order.
# Each is invoked with a single file/dir argument; none interprets a
# shell. ``--`` separators are added by the launcher.
_FILE_MANAGERS = (
    "/usr/bin/pcmanfm-qt",
    "/usr/bin/pcmanfm",
    "/usr/bin/dolphin",
    "/usr/bin/nautilus",
    "/usr/bin/xdg-open",
)


def resolve_file_manager(
        candidates: tuple[str, ...] = _FILE_MANAGERS,
        *, exists: Callable[[str], bool] = os.path.exists) -> str:
    """Return the first installed file-manager binary, or "" if none."""
    for fm in candidates:
        if exists(fm):
            return fm
    return ""


def open_location(path: str, uid: int, gid: int,
                  *, runner: Callable[..., int] | None = None,
                  fm_resolver: Callable[[], str] = resolve_file_manager,
                  home_fn: Callable[[int], str] = _home_for_uid) -> dict:
    """Open ``path``'s containing folder in the originating user's file
    manager, dropping to ``uid``/``gid``.

    Re-validates :func:`path_allowed_for_uid` immediately before exec — a
    notification can sit unclicked for minutes, so this is the load-
    bearing gate (defense in depth with the notify-time check). The file
    manager is launched as the caller's uid via ``runner`` (production
    binds a ``subprocess.run(..., user=, group=)`` thunk; tests inject a
    recorder). No shell, tokenised argv.
    """
    if not path_allowed_for_uid(path, uid, home_fn=home_fn):
        return {"ok": False, "error": "path_not_allowed"}
    target = os.path.realpath(path)
    folder = target if os.path.isdir(target) else os.path.dirname(target)
    if not folder:
        return {"ok": False, "error": "no_folder"}
    fm = fm_resolver()
    if not fm:
        return {"ok": False, "error": "no_file_manager"}
    argv = [fm, "--", folder] if fm.endswith("xdg-open") else [fm, folder]
    if runner is None:  # pragma: no cover - production exec path
        runner = _default_runner
    try:
        rc = int(runner(argv, uid, gid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "launch_failed",
                "detail": str(e)[:200]}
    return {"ok": rc == 0, "folder": folder, "file_manager": fm, "rc": rc}


def _default_runner(argv: list[str], uid: int,
                    gid: int) -> int:  # pragma: no cover
    import subprocess
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": _home_for_uid(uid) or "/",
        "DISPLAY": os.environ.get("DISPLAY", ""),
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", ""),
        "XDG_RUNTIME_DIR": f"/run/user/{int(uid)}",
    }
    proc = subprocess.Popen(
        argv, user=uid, group=gid, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True)
    # Fire-and-forget: the file manager detaches; report launch success.
    return 0 if proc.pid else 1


# --------------------------------------------------------------------- #
# D-Bus glue (production only)
# --------------------------------------------------------------------- #

def _main() -> int:  # pragma: no cover - requires a live session bus
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    class _FreedesktopNotifier(_BaseNotifier):
        """Posts via org.freedesktop.Notifications and wires the
        default-action / "Open folder" button back to open_location."""

        def __init__(self, session_bus):
            self._bus = session_bus
            proxy = session_bus.get_object(
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications")
            self._ifc = dbus.Interface(
                proxy, "org.freedesktop.Notifications")
            # download_id keyed map of (path, uid) for the action handler.
            self._pending: dict[int, tuple[str, int]] = {}
            self._ifc.connect_to_signal("ActionInvoked", self._on_action)

        def notify(self, *, summary, body, download_id, path, uid):
            nid = int(self._ifc.Notify(
                "qdistro-downloads", 0, "folder-download",
                summary, body,
                ["open", "Open folder"], {}, 8000))
            self._pending[nid] = (path, uid)
            return nid

        def _on_action(self, notif_id, action_key):
            entry = self._pending.pop(int(notif_id), None)
            if entry is None or action_key not in ("open", "default"):
                return
            path, uid = entry
            try:
                import pwd as _pwd
                gid = _pwd.getpwuid(int(uid)).pw_gid
            except (KeyError, ValueError):
                return
            open_location(path, int(uid), int(gid))

    notifier = _FreedesktopNotifier(bus)

    class DownloadsDaemon(dbus.service.Object):
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
        def Notify(self, body, sender=None):
            try:
                req = json.loads(body)
                if not isinstance(req, dict):
                    raise ValueError
            except (TypeError, ValueError):
                return json.dumps({"ok": False, "error": "malformed_body"})
            uid, pid = self._peer(sender)
            return json.dumps(handle_notify(
                req, caller_uid=uid, caller_pid=pid, notifier=notifier))

    DownloadsDaemon()
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
