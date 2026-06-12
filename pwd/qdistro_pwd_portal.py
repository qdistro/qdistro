#!/usr/bin/env python3
"""qdistro XDG portal Secret backend (spec/13 Phase-8.3).

Implements `org.freedesktop.impl.portal.Secret.RetrieveSecret` so
unmodified Flatpak / sandboxed apps that use libsecret-portal see a
working backend. Bridges to the system-bus pwd daemon's
`GetPortalKey(app_id) -> ay` method.

## Architecture

```
  Flatpak app
     │
     │ libsecret-portal: org.freedesktop.portal.Secret.RetrieveSecret(fd, app_id)
     ▼
  xdg-desktop-portal (per-user)
     │
     │ org.freedesktop.impl.portal.Secret.RetrieveSecret(handle, app_id, fd, options)
     ▼
  qdistro-pwd-portal (this daemon, per-user session bus)
     │
     │ system-bus: org.qdistro.Pwd1.GetPortalKey(app_id) -> ay
     ▼
  qdistro-pwd (system bus)
     │
     │ /var/lib/qdistro/vaults/portal-keys.vault — auto-provisions
     │ a 32-byte random key per app_id under tag portal/<app_id>.
     ▼
  Disk
```

## Backend registration

xdg-desktop-portal discovers backends via `*.portal` files in
`/usr/share/xdg-desktop-portal/portals/`. The accompanying
`org.qdistro.PortalSecret.portal` declares this daemon as the
Secret backend.

Apps don't see qdistro-pwd-portal directly — they hit
xdg-desktop-portal which proxies to us.

## Constraints

- Caller uid is captured but the daemon trusts the app_id passed by
  xdg-desktop-portal (which validated it from /proc/<pid>/root/.flatpak-info).
- The `portal-keys` vault MUST be unlocked before any app can fetch
  a key; admin runs `qdistro-pwd-admin unlock portal-keys` once per
  session (or once with TPM-sealed v2 if auto-unlock is wired up
  later).
- Returned bytes are 32 random bytes per app_id, stable across
  sessions (so app's keyring re-keys consistently).
"""
from __future__ import annotations

import os
import signal
import sys

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

PORTAL_BUS_NAME = "org.qdistro.PortalSecret"
PORTAL_OBJ_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFC = "org.freedesktop.impl.portal.Secret"

PWD_BUS = "org.qdistro.Pwd1"
PWD_OBJ = "/org/qdistro/Pwd1"
XDG_DESKTOP_PORTAL_BUS = "org.freedesktop.portal.Desktop"
XDG_DESKTOP_PORTAL_EXES = tuple(
    p for p in os.environ.get(
        "QDISTRO_XDG_DESKTOP_PORTAL_EXES",
        ":".join((
            "/usr/libexec/xdg-desktop-portal",
            "/usr/lib/xdg-desktop-portal",
            "/usr/bin/xdg-desktop-portal",
        ))).split(":") if p)


# Per the portal Request flow, response codes are:
#   0 = success
#   1 = user cancelled
#   2 = generic error
RESP_SUCCESS = 0
RESP_USER_CANCELLED = 1
RESP_OTHER_ERROR = 2


class PortalSecretBackend(dbus.service.Object):
    def __init__(self, bus: dbus.Bus):
        super().__init__(bus, PORTAL_OBJ_PATH)
        self._session_bus = bus
        # System-bus connection to the pwd daemon. Cache it; D-Bus
        # auto-reconnects if the daemon restarts.
        self._sys_bus = dbus.SystemBus()

    def _pwd_iface(self) -> dbus.Interface:
        proxy = self._sys_bus.get_object(PWD_BUS, PWD_OBJ)
        return dbus.Interface(proxy, PWD_BUS)

    def _read_proc_exe(self, pid: int) -> str:
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return ""

    def _portal_frontend_allowed(self, sender: str | None) -> bool:
        """Only accept calls relayed by the xdg-desktop-portal frontend."""
        if not sender:
            return False
        try:
            proxy = self._session_bus.get_object(
                "org.freedesktop.DBus", "/org/freedesktop/DBus")
            ifc = dbus.Interface(proxy, "org.freedesktop.DBus")
            owner = str(ifc.GetNameOwner(XDG_DESKTOP_PORTAL_BUS))
            if owner != str(sender):
                return False
            pid = int(ifc.GetConnectionUnixProcessID(sender))
            exe = os.path.realpath(self._read_proc_exe(pid))
            allowed = {os.path.realpath(p) for p in XDG_DESKTOP_PORTAL_EXES}
            return bool(exe) and exe in allowed
        except dbus.DBusException:
            return False
        except (TypeError, ValueError):
            return False

    @dbus.service.method(
        PORTAL_IFC, in_signature="osha{sv}", out_signature="ua{sv}",
        sender_keyword="sender")
    def RetrieveSecret(self, handle, app_id, fd, options, sender=None):
        """Portal RetrieveSecret implementation.

        - `handle`  — Request object path (we ignore; synchronous reply).
        - `app_id`  — flatpak app id (e.g. "org.gnome.Calculator").
        - `fd`      — UnixFD passed by libsecret-portal; we write the
                      key bytes here and close.
        - `options` — currently unused; libsecret-portal passes empty.

        Returns (response_code, results_dict).
        """
        if not self._portal_frontend_allowed(sender):
            sys.stderr.write(
                "[qdistro-pwd-portal] rejecting non-portal frontend "
                f"caller {sender!r}\n")
            sys.stderr.flush()
            try:
                os.close(fd.take())
            except Exception:
                pass
            return dbus.UInt32(RESP_OTHER_ERROR), dbus.Dictionary({}, signature="sv")
        try:
            key = bytes(self._pwd_iface().GetPortalKey(str(app_id)))
        except dbus.DBusException as e:
            sys.stderr.write(
                f"[qdistro-pwd-portal] GetPortalKey({app_id!r}) failed: "
                f"{e.get_dbus_name()}: {e.get_dbus_message()}\n")
            sys.stderr.flush()
            try:
                os.close(fd.take())
            except Exception:
                pass
            return dbus.UInt32(RESP_OTHER_ERROR), dbus.Dictionary({}, signature="sv")
        try:
            real_fd = fd.take()
            written = 0
            buf = key
            while written < len(buf):
                n = os.write(real_fd, buf[written:])
                if n <= 0:
                    break
                written += n
            os.close(real_fd)
        except OSError as e:
            sys.stderr.write(
                f"[qdistro-pwd-portal] writing fd: {e}\n")
            sys.stderr.flush()
            return dbus.UInt32(RESP_OTHER_ERROR), dbus.Dictionary({}, signature="sv")
        return dbus.UInt32(RESP_SUCCESS), dbus.Dictionary({}, signature="sv")


_LOOP: GLib.MainLoop | None = None


def _on_term(*_user_data):
    print("[qdistro-pwd-portal] caught signal, shutting down", flush=True)
    if _LOOP is not None:
        _LOOP.quit()
    return False


def main() -> int:
    global _LOOP
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName(PORTAL_BUS_NAME, bus, do_not_queue=True)  # noqa: F841
    backend = PortalSecretBackend(bus)  # noqa: F841
    print(f"[qdistro-pwd-portal] listening on {PORTAL_BUS_NAME} {PORTAL_OBJ_PATH}",
          flush=True)
    _LOOP = GLib.MainLoop()
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _on_term, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT,  _on_term, None)
    _LOOP.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
