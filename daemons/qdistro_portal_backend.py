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
import re
import signal
import stat
import subprocess
import sys
from typing import Any

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

# Shared /proc identity readers (anti-PID-reuse starttime anchor, exe
# resolution), used to attest the *real* D-Bus sender (the XDG portal
# frontend) rather than trusting an arbitrary same-session caller.
#
# No sys.path manipulation: the module resolves from this file's own
# directory in BOTH layouts. Installed, install-portal-backend-for-vm.sh
# copies broker/qdistro_proc_identity.py alongside this daemon into
# /usr/lib/qdistro/daemons/ and systemd execs this file directly, so it
# is sys.path[0]. In the checkout, pyproject.toml puts broker/ on
# pytest's pythonpath.
#
# This used to prepend __file__/../../broker, which installed resolved to
# /usr/lib/qdistro/broker — a directory nothing creates. The insert was
# dead but read as the working wiring, hiding where resolution actually
# comes from; the same shape was a LIVE break in user_relay (F4).
try:
    import qdistro_proc_identity as _pi  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 — readers are best-effort; absence = DENY
    _pi = None  # type: ignore[assignment]

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

# Canonical ABSOLUTE paths of the trusted XDG portal *frontend* binary. The
# D-Bus caller of ``org.freedesktop.impl.portal.*`` is always
# xdg-desktop-portal (the frontend), never the app itself — the app talks to
# the frontend, the frontend authenticates it and re-issues the impl call
# carrying the app's identity in the ``app_id`` argument. We therefore verify
# the *sender* is the expected frontend exe and, having done so, trust the
# forwarded ``app_id`` for broker scoping.
#
# We match the FULL resolved ``/proc/<pid>/exe`` path against this allowlist —
# NOT the basename. /proc/<pid>/exe is set by the kernel at execve() and is
# not writable by the process, so a full-path match cannot be forged: a
# same-uid attacker who runs a self-authored binary named "xdg-desktop-portal"
# out of /tmp has exe=/tmp/xdg-desktop-portal, which is not on the allowlist
# (the earlier basename check accepted it — finding #8). We additionally
# require the file to be root-owned and not group/other-writable so a path on
# the allowlist can only ever be a system-managed binary. Override the
# accepted paths for testing / alternate frontends via
# QDISTRO_PORTAL_FRONTEND_EXE (comma-separated absolute paths).
_FRONTEND_EXE_PATHS = frozenset(
    p.strip() for p in os.environ.get(
        "QDISTRO_PORTAL_FRONTEND_EXE",
        "/usr/libexec/xdg-desktop-portal:"
        "/usr/lib/xdg-desktop-portal:"
        "/usr/lib64/xdg-desktop-portal:"
        "/usr/local/libexec/xdg-desktop-portal").split(":") if p.strip()
)


def _is_trusted_root_file(path: str, allowed: frozenset) -> bool:
    """True iff ``path`` resolves (realpath) to an absolute path on the
    ``allowed`` allowlist AND the resolved file is a regular file owned by
    root and not writable by group/other.

    This is the shared "the process is executing a canonical, system-managed
    file" predicate. /proc/<pid>/exe is non-forgeable; an argv-derived script
    path is forgeable in argv memory but the realpath+root-owned+non-writable
    requirement still defeats the trivial "name a file in /tmp" attack and
    bounds the residual to a process that has actually arranged for a
    root-owned canonical file to appear as its identity. Fail closed on any
    error."""
    if not path:
        return False
    try:
        real = os.path.realpath(path)
        if real not in allowed:
            return False
        st = os.stat(real)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid != 0:
        return False
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return True


def _sanitize_app_id(app_id: str) -> str:
    """Reduce an app id to the broker-detail charset."""
    app = str(app_id or "unknown")[:128]
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", app).strip("._-") or "unknown"


def _portal_action(base: str, app_id: str) -> str:
    """Scope portal approvals to the requesting portal app_id."""
    return f"{base}:{_sanitize_app_id(app_id)}"


def _caller_pid(sender: str) -> int:
    """Kernel-attested pid of the D-Bus ``sender`` connection.

    Resolves ``org.freedesktop.DBus.GetConnectionUnixProcessID`` on the
    session bus. Returns ``0`` when the sender is missing/unknown (the
    connection went away, or the caller is the bus itself).
    """
    if not sender:
        return 0
    try:
        bus = dbus.SessionBus()
        proxy = bus.get_object("org.freedesktop.DBus",
                               "/org/freedesktop/DBus")
        ifc = dbus.Interface(proxy, "org.freedesktop.DBus")
        return int(ifc.GetConnectionUnixProcessID(sender))
    except dbus.DBusException as exc:
        print(f"[qdistro-portal] GetConnectionUnixProcessID({sender!r}) "
              f"failed: {exc.get_dbus_name()}", file=sys.stderr, flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[qdistro-portal] pid attestation failed: {exc!r}",
              file=sys.stderr, flush=True)
        return 0


def verify_frontend(sender: str) -> bool:
    """Verify the D-Bus ``sender`` IS the trusted XDG portal frontend.

    In the XDG portal architecture the caller of
    ``org.freedesktop.impl.portal.*`` (the BACKEND/impl side this daemon
    implements) is **xdg-desktop-portal** — the trusted frontend that has
    already authenticated the real application and forwards its identity
    in the ``app_id`` *argument*. The app never calls us directly.

    So the right thing to attest about the *sender* is not "which app",
    but "is this really the frontend". We resolve the sender's real pid
    via the bus, require a non-zero starttime (anti-PID-reuse), read the
    binary behind ``/proc/<pid>/exe``, and require its FULL RESOLVED PATH
    to be a canonical, root-owned, non-writable frontend binary (see
    :data:`_FRONTEND_EXE_PATHS` / :func:`_is_trusted_root_file`).

    Matching the full exe path — not the basename — is what makes this
    non-forgeable: /proc/<pid>/exe is fixed by the kernel at execve() and
    cannot be rewritten by the process, so a same-uid attacker who runs a
    self-authored ``/tmp/xdg-desktop-portal`` is rejected (its exe path is
    not on the allowlist), whereas the old basename check accepted it
    (finding #8). Returns ``True`` only when the sender is verified as the
    frontend; any failure to attest — missing sender, no proc-identity
    readers, pid gone, exe unreadable, zero starttime, or an exe that is
    not a canonical root-owned frontend binary — returns ``False`` so
    callers fail closed (DENY).
    """
    if _pi is None:
        return False
    pid = _caller_pid(sender)
    if pid <= 0:
        return False
    starttime = _pi.read_starttime(pid)
    if not starttime:  # 0 -> gone / unreadable / malformed: fail closed
        return False
    exe = _pi.read_exe(pid)
    if not exe or exe == "?":
        return False
    return _is_trusted_root_file(exe, _FRONTEND_EXE_PATHS)


def resolve_app_id(app_id: str, sender: str) -> tuple[str, bool]:
    """Resolve the app identity to scope a broker decision to.

    The sender must first be verified as the trusted portal frontend
    (:func:`verify_frontend`). Once that holds, the frontend-forwarded
    ``app_id`` *argument* is the authoritative app identity — it was set
    by the frontend after authenticating the real app, NOT by the app
    itself, so it is not the spoofable value the original finding #8 was
    about (that finding concerned a *direct* caller forging ``app_id``;
    verifying the sender is the frontend is what closes that).

    Returns ``(scoped_app_id, ok)``. ``ok`` is ``False`` (DENY) when the
    sender cannot be verified as the frontend. A frontend call with an
    empty forwarded ``app_id`` (unsandboxed / unidentified app) scopes to
    the sanitized ``"unknown"`` bucket rather than being denied — the
    frontend has still vouched for the channel.
    """
    if not verify_frontend(sender):
        return ("", False)
    return (_sanitize_app_id(app_id), True)


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
        sender_keyword="sender",
    )
    def AccessDialog(self, handle, app_id, parent_window,
                     title, subtitle, body, options, sender=None):
        """Generic access dialog.

        Checks ``portal.access`` via the broker.  If the broker says
        "allow" the dialog is auto-approved; "deny" is auto-rejected;
        "unknown" triggers a fire-and-forget RequestPermission and
        returns cancelled (the next attempt after admin approval will
        succeed via the cache).

        The D-Bus sender must be verified as the trusted XDG portal
        frontend; the broker action is then scoped to the *frontend-
        forwarded* ``app_id`` (which the frontend set after authenticating
        the real app). A sender that is NOT the verified frontend is
        DENIED — that blocks a direct same-session caller forging app_id.
        """
        app_id_s, ok = resolve_app_id(app_id, sender)
        if not ok:
            print(f"[qdistro-portal] AccessDialog DENY: sender is not the "
                  f"verified portal frontend sender={sender!r}",
                  file=sys.stderr, flush=True)
            return (dbus.UInt32(RESP_CANCELLED),
                    dbus.Dictionary({}, signature="sv"))
        title_s = str(title or "")
        action = _portal_action("portal.access", app_id_s)
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
        in_signature="osssa{sv}",
        out_signature="ua{sv}",
        sender_keyword="sender",
    )
    def OpenFile(self, handle, app_id, parent_window, title, options,
                 sender=None):
        """Open-file portal: broker gate then file picker."""
        return self._file_chooser(handle, app_id, parent_window,
                                  title, options, save=False, sender=sender)

    @dbus.service.method(
        FILECHOOSER_IFC,
        in_signature="osssa{sv}",
        out_signature="ua{sv}",
        sender_keyword="sender",
    )
    def SaveFile(self, handle, app_id, parent_window, title, options,
                 sender=None):
        """Save-file portal: broker gate then file picker."""
        return self._file_chooser(handle, app_id, parent_window,
                                  title, options, save=True, sender=sender)

    def _file_chooser(self, handle, app_id, parent_window,
                      title, options, *, save: bool, sender=None):
        app_id_s, ok = resolve_app_id(app_id, sender)
        if not ok:
            print(f"[qdistro-portal] {'SaveFile' if save else 'OpenFile'} "
                  f"DENY: sender is not the verified portal frontend "
                  f"sender={sender!r}", file=sys.stderr, flush=True)
            return (dbus.UInt32(RESP_CANCELLED),
                    dbus.Dictionary({}, signature="sv"))
        action = _portal_action("com.qdistro.fs.open", app_id_s)
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
        title_s = str(title or "")
        picker_title = title_s or str(opts.get(
            "title", "Save File" if save else "Open File"))
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
            title=picker_title, save=save, multiple=multiple,
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
        sender_keyword="sender",
    )
    def Screenshot(self, handle, app_id, parent_window, options,
                   sender=None):
        """Screenshot portal: broker gate on screen capture.

        The actual screencopy is deferred to the compositor (qdshell);
        this backend only handles the policy gate. A real implementation
        would invoke qdshell's screencopy interface after approval.
        """
        app_id_s, ok = resolve_app_id(app_id, sender)
        if not ok:
            print(f"[qdistro-portal] Screenshot DENY: sender is not the "
                  f"verified portal frontend sender={sender!r}",
                  file=sys.stderr, flush=True)
            return (dbus.UInt32(RESP_CANCELLED),
                    dbus.Dictionary({}, signature="sv"))
        action = _portal_action("com.qdistro.screen.capture", app_id_s)
        details = {"app_id": app_id_s}
        verdict = _check_permission(self._sys_bus, action, details)
        if verdict == "allow":
            # Do not advertise success until compositor screencopy is
            # wired and a real file:// URI can be returned.
            return (dbus.UInt32(RESP_OTHER),
                    dbus.Dictionary({}, signature="sv"))
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
        sender_keyword="sender",
    )
    def AddNotification(self, app_id, id_, notification, sender=None):
        """Add a notification.

        Checks ``portal.notification`` via the broker.  If denied or
        unknown the notification is silently dropped (no error to the
        caller -- the portal spec says AddNotification has no reply
        content). An unattestable caller is dropped.
        """
        app_id_s, ok = resolve_app_id(app_id, sender)
        if not ok:
            print(f"[qdistro-portal] AddNotification DENY: sender is not "
                  f"the verified portal frontend sender={sender!r}",
                  file=sys.stderr, flush=True)
            return
        action = _portal_action("portal.notification", app_id_s)
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
        sender_keyword="sender",
    )
    def RemoveNotification(self, app_id, id_, sender=None):
        """Remove a notification.

        Policy-checked the same way as AddNotification so a denied app
        cannot remove notifications it did not create. An unattestable
        caller is dropped.
        """
        app_id_s, ok = resolve_app_id(app_id, sender)
        if not ok:
            print(f"[qdistro-portal] RemoveNotification DENY: sender is not "
                  f"the verified portal frontend sender={sender!r}",
                  file=sys.stderr, flush=True)
            return
        action = _portal_action("portal.notification", app_id_s)
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
