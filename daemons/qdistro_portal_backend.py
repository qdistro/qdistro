#!/usr/bin/env python3
"""qdistro xdg-desktop-portal backend.

Session-bus D-Bus service registered as
``org.freedesktop.impl.portal.qdistro`` that bridges standard XDG
portal interfaces to the qdistro admin broker.  Unmodified Flatpak /
GTK / Qt apps already speak the portal protocol; this backend routes
those requests through ``qbus-admin`` (CheckPermission / RequestPermission)
instead of the usual same-user approval.

Implements:

- ``org.freedesktop.impl.portal.Access``     -- generic access dialog
- ``org.freedesktop.impl.portal.FileChooser`` -- open / save file
- ``org.freedesktop.impl.portal.Screenshot``  -- screen capture
- ``org.freedesktop.impl.portal.Notification``-- add / remove notifications

Architecture::

    App (Flatpak / GTK / Qt)
      |
      |  portal interface (e.g. org.freedesktop.portal.Screenshot)
      v
    xdg-desktop-portal  (per-user session)
      |
      |  org.freedesktop.impl.portal.*  (routed by qdistro.portal conf)
      v
    qdistro-portal-backend  (this daemon, session bus)
      |
      |  system-bus: org.qdistro.AdminBroker1.CheckPermission / RequestPermission
      v
    qdistro-admin-broker  (system bus)

Backend selection is configured via ``qdistro-portals.conf``::

    [preferred]
    default=qdistro
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

# Portal bus name claimed on the session bus so xdg-desktop-portal
# can activate this backend on demand.
PORTAL_BUS_NAME = "org.freedesktop.impl.portal.qdistro"
PORTAL_OBJ_PATH = "/org/freedesktop/portal/desktop"

# Portal interface names (per xdg-desktop-portal spec).
ACCESS_IFC = "org.freedesktop.impl.portal.Access"
FILECHOOSER_IFC = "org.freedesktop.impl.portal.FileChooser"
SCREENSHOT_IFC = "org.freedesktop.impl.portal.Screenshot"
NOTIFICATION_IFC = "org.freedesktop.impl.portal.Notification"

# Admin broker coordinates on the system bus.
BROKER_BUS = "org.qdistro.AdminBroker1"
BROKER_OBJ = "/org/qdistro/AdminBroker1"

# Portal response codes (xdg-desktop-portal spec).
RESP_SUCCESS = 0
RESP_CANCELLED = 1
RESP_OTHER = 2

# File-picker helper. Uses zenity by default; override with
# QDISTRO_FILE_PICKER for testing or alternate UIs.
_FILE_PICKER = os.environ.get("QDISTRO_FILE_PICKER", "zenity")


def _broker_iface(sys_bus: dbus.Bus) -> dbus.Interface:
    proxy = sys_bus.get_object(BROKER_BUS, BROKER_OBJ)
    return dbus.Interface(proxy, BROKER_BUS)


def _check_permission(sys_bus: dbus.Bus, action: str,
                       details: dict[str, str]) -> str:
    """Ask the broker for a fast-path verdict.

    Returns "allow", "deny", or "unknown".  On D-Bus errors (broker
    not running, timeout) returns "deny" -- fail closed.
    """
    try:
        iface = _broker_iface(sys_bus)
        return str(iface.CheckPermission(action, details))
    except dbus.DBusException as exc:
        print(f"[qdistro-portal] CheckPermission({action!r}) failed: "
              f"{exc.get_dbus_name()}: {exc.get_dbus_message()}",
              file=sys.stderr, flush=True)
        return "deny"


def _request_permission(sys_bus: dbus.Bus, action: str,
                         details: dict[str, str]) -> None:
    """Fire-and-forget RequestPermission so admin sees the prompt."""
    try:
        iface = _broker_iface(sys_bus)
        iface.RequestPermission(action, details)
    except dbus.DBusException as exc:
        print(f"[qdistro-portal] RequestPermission({action!r}) failed: "
              f"{exc.get_dbus_name()}: {exc.get_dbus_message()}",
              file=sys.stderr, flush=True)


def _run_file_picker(*, title: str = "Open File",
                     save: bool = False,
                     multiple: bool = False,
                     directory: bool = False,
                     current_folder: str = "") -> list[str]:
    """Run an external file-picker and return selected path(s).

    Returns an empty list on cancel or error.
    """
    cmd: list[str] = [_FILE_PICKER]
    if save:
        cmd.append("--file-selection")
        cmd.append("--save")
    else:
        cmd.append("--file-selection")
    if multiple:
        cmd.append("--multiple")
    if directory:
        cmd.append("--directory")
    cmd.extend(["--title", title])
    if current_folder:
        cmd.extend(["--filename", current_folder + "/"])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return []
        raw = result.stdout.strip()
        if not raw:
            return []
        # zenity --multiple separates with "|"
        return [p for p in raw.split("|") if p]
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[qdistro-portal] file picker failed: {exc}",
              file=sys.stderr, flush=True)
        return []


class QdistroPortalBackend(dbus.service.Object):
    """XDG desktop portal backend routing requests through the qdistro
    admin broker for policy enforcement."""

    def __init__(self, bus: dbus.Bus):
        super().__init__(bus, PORTAL_OBJ_PATH)
        self._sys_bus = dbus.SystemBus()

    # -----------------------------------------------------------------
    # org.freedesktop.impl.portal.Access
    # -----------------------------------------------------------------

    @dbus.service.method(
        ACCESS_IFC,
        in_signature="osssssa{sv}",
        out_signature="ua{sv}",
    )
    def AccessDialog(self, handle, app_id, parent_window,
                     title, subtitle, body, options):
        """Generic access dialog.

        Checks ``portal.access`` via the broker.  If the broker says
        "allow" the dialog is auto-approved; "deny" is auto-rejected;
        "unknown" triggers a fire-and-forget RequestPermission and
        returns cancelled (the next attempt after admin approval will
        succeed via the cache).
        """
        app_id_s = str(app_id or "")
        title_s = str(title or "")
        action = "portal.access"
        details = {"app_id": app_id_s, "title": title_s}
        verdict = _check_permission(self._sys_bus, action, details)
        if verdict == "allow":
            return (dbus.UInt32(RESP_SUCCESS),
                    dbus.Dictionary({}, signature="sv"))
        if verdict == "deny":
            return (dbus.UInt32(RESP_CANCELLED),
                    dbus.Dictionary({}, signature="sv"))
        # unknown -- fire-and-forget so admin sees it, return cancelled
        _request_permission(self._sys_bus, action, details)
        return (dbus.UInt32(RESP_CANCELLED),
                dbus.Dictionary({}, signature="sv"))

    # -----------------------------------------------------------------
    # org.freedesktop.impl.portal.FileChooser
    # -----------------------------------------------------------------

    @dbus.service.method(
        FILECHOOSER_IFC,
        in_signature="ossa{sv}",
        out_signature="ua{sv}",
    )
    def OpenFile(self, handle, app_id, parent_window, options):
        """Open-file portal: broker gate then file picker."""
        return self._file_chooser(handle, app_id, parent_window,
                                  options, save=False)

    @dbus.service.method(
        FILECHOOSER_IFC,
        in_signature="ossa{sv}",
        out_signature="ua{sv}",
    )
    def SaveFile(self, handle, app_id, parent_window, options):
        """Save-file portal: broker gate then file picker."""
        return self._file_chooser(handle, app_id, parent_window,
                                  options, save=True)

    def _file_chooser(self, handle, app_id, parent_window,
                      options, *, save: bool):
        app_id_s = str(app_id or "")
        action = "com.qdistro.fs.open"
        details = {"app_id": app_id_s}
        verdict = _check_permission(self._sys_bus, action, details)
        if verdict == "deny":
            return (dbus.UInt32(RESP_CANCELLED),
                    dbus.Dictionary({}, signature="sv"))
        if verdict == "unknown":
            _request_permission(self._sys_bus, action, details)
            return (dbus.UInt32(RESP_CANCELLED),
                    dbus.Dictionary({}, signature="sv"))
        # verdict == "allow" -- show the picker
        opts = dict(options) if options else {}
        title = str(opts.get("title", "Save File" if save else "Open File"))
        multiple = bool(opts.get("multiple", False)) and not save
        directory = bool(opts.get("directory", False))
        current_folder = ""
        if "current_folder" in opts:
            try:
                raw = bytes(opts["current_folder"])
                current_folder = raw.rstrip(b"\x00").decode("utf-8", "replace")
            except Exception:
                pass
        paths = _run_file_picker(
            title=title, save=save, multiple=multiple,
            directory=directory, current_folder=current_folder)
        if not paths:
            return (dbus.UInt32(RESP_CANCELLED),
                    dbus.Dictionary({}, signature="sv"))
        # Build the xdg-desktop-portal FileChooser result.
        # "uris" is an array of strings (as URI).
        from urllib.parse import quote
        uris = dbus.Array(
            [dbus.String("file://" + quote(p, safe="/")) for p in paths],
            signature="s")
        results: dict[str, Any] = {"uris": uris}
        return (dbus.UInt32(RESP_SUCCESS),
                dbus.Dictionary(results, signature="sv"))

    # -----------------------------------------------------------------
    # org.freedesktop.impl.portal.Screenshot
    # -----------------------------------------------------------------

    @dbus.service.method(
        SCREENSHOT_IFC,
        in_signature="ossa{sv}",
        out_signature="ua{sv}",
    )
    def Screenshot(self, handle, app_id, parent_window, options):
        """Screenshot portal: broker gate on screen capture.

        The actual screencopy is deferred to the compositor (qdshell);
        this backend only handles the policy gate. A real implementation
        would invoke qdshell's screencopy interface after approval.
        """
        app_id_s = str(app_id or "")
        action = "com.qdistro.screen.capture"
        details = {"app_id": app_id_s}
        verdict = _check_permission(self._sys_bus, action, details)
        if verdict == "allow":
            # In a full implementation, invoke qdshell screencopy here
            # and return the URI of the captured image.  For now, return
            # success with an empty URI -- the compositor integration is
            # a separate piece.
            results: dict[str, Any] = {"uri": dbus.String("")}
            return (dbus.UInt32(RESP_SUCCESS),
                    dbus.Dictionary(results, signature="sv"))
        if verdict == "deny":
            return (dbus.UInt32(RESP_CANCELLED),
                    dbus.Dictionary({}, signature="sv"))
        _request_permission(self._sys_bus, action, details)
        return (dbus.UInt32(RESP_CANCELLED),
                dbus.Dictionary({}, signature="sv"))

    # -----------------------------------------------------------------
    # org.freedesktop.impl.portal.Notification
    # -----------------------------------------------------------------

    @dbus.service.method(
        NOTIFICATION_IFC,
        in_signature="ssa{sv}",
        out_signature="",
    )
    def AddNotification(self, app_id, id_, notification):
        """Add a notification.

        Checks ``portal.notification`` via the broker.  If denied or
        unknown the notification is silently dropped (no error to the
        caller -- the portal spec says AddNotification has no reply
        content).
        """
        app_id_s = str(app_id or "")
        action = "portal.notification"
        details = {"app_id": app_id_s, "notification_id": str(id_ or "")}
        verdict = _check_permission(self._sys_bus, action, details)
        if verdict == "allow":
            # Deliver the notification via the host notification daemon.
            # For now, log it -- actual delivery is a future integration
            # with the qdistro notification surface.
            print(f"[qdistro-portal] notification allowed: "
                  f"app={app_id_s} id={id_}", flush=True)
        elif verdict == "unknown":
            _request_permission(self._sys_bus, action, details)

    @dbus.service.method(
        NOTIFICATION_IFC,
        in_signature="ss",
        out_signature="",
    )
    def RemoveNotification(self, app_id, id_):
        """Remove a notification.

        Policy-checked the same way as AddNotification so a denied app
        cannot remove notifications it did not create.
        """
        app_id_s = str(app_id or "")
        action = "portal.notification"
        details = {"app_id": app_id_s, "notification_id": str(id_ or "")}
        verdict = _check_permission(self._sys_bus, action, details)
        if verdict == "allow":
            print(f"[qdistro-portal] notification removed: "
                  f"app={app_id_s} id={id_}", flush=True)


# -- Mainloop ----------------------------------------------------------

_LOOP: GLib.MainLoop | None = None


def _on_term(*_args):
    print("[qdistro-portal] caught signal, shutting down", flush=True)
    if _LOOP is not None:
        _LOOP.quit()
    return False


def main() -> int:
    global _LOOP
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName(  # noqa: F841
        PORTAL_BUS_NAME, bus, do_not_queue=True)
    backend = QdistroPortalBackend(bus)  # noqa: F841
    print(f"[qdistro-portal] listening on {PORTAL_BUS_NAME} "
          f"{PORTAL_OBJ_PATH}", flush=True)
    _LOOP = GLib.MainLoop()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _on_term, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _on_term, None)
    _LOOP.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
