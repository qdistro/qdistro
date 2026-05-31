#!/usr/bin/env python3
"""qdistro xdg-desktop-portal **frontend** (permission-lineage Option A).

Owns ``org.freedesktop.portal.Desktop`` on a *silo session bus*. Unlike
the legacy backend (``qdistro_portal_backend.py``), which is activated by
the stock ``xdg-desktop-portal`` and therefore only ever sees a
self-asserted, spoofable ``app_id`` string (the D-Bus sender it observes
is xdg-desktop-portal, never the app), this frontend has the app connect
to it **directly**. That direct connection is the whole point: the
frontend can ``GetConnectionUnixProcessID`` / SO_PEERCRED the app's *own*
D-Bus connection to recover the app's real pid, read its starttime from
``/proc`` (anti-PID-reuse anchor), and relay that kernel-attested
``(pid, starttime)`` to the broker via
``CheckPermissionForClient`` / ``RequestPermissionForClient``.

The broker then resolves the CLIENT (not this frontend) against the
launch-record store and decides for it — reusing the exact proven lineage
chain qdshell uses for clipboard. See
``issues/qdistro/permission-lineage-findings.md`` §4 (Option A).

Trust posture (fail-closed throughout):

- The app NEVER supplies its own pid or app_id for the decision. Any
  ``handle_token`` / ``app_id`` an app passes in the portal call is
  advisory only; the broker overrides app_id/sandbox_engine with the
  launcher-attested value for the *resolved* pid. We pass the app's
  claimed ``app_id`` through purely so shadow-mode logging can compare it,
  exactly like the other lineage gates.
- The resolved pid is the connection pid the *kernel* reports, never a
  value carried in the message body.
- If the app's ``/proc`` entry is gone, unreadable, or its starttime
  reads as 0 (process vanished mid-call), we fail closed: the request is
  refused (``RESP_CANCELLED``) and no broker decision is consulted.

Architecture::

    App (Flatpak / GTK / Qt)
      |
      |  org.freedesktop.portal.Desktop.<Iface>.<Method>  (DIRECT)
      v
    qdistro-portal-frontend  (this daemon, silo session bus)
      |  GetConnectionUnixProcessID(app) -> pid; /proc -> starttime
      |
      |  system bus: org.qdistro.AdminBroker1.CheckPermissionForClient(
      |               action, details, client_pid, client_starttime)
      v
    qdistro-admin-broker  (system bus) -> resolve_subject(client_pid) -> decide

The dispatch core (``resolve_client_tuple`` / ``build_broker_call`` /
``decision_to_response`` / ``attested_decision``) is pure and takes an
injectable broker client, so it unit-tests without a live bus, mirroring
``qdistro_mpris_daemon`` / ``qdistro_portal_backend``.

The per-method handlers all route through the SAME attested core:

- ``handle_access``       -- representative Access method.
- ``handle_open_file`` / ``handle_save_file`` -- FileChooser; a denied
  request returns ``RESP_CANCELLED`` and NO files.
- ``handle_screenshot``   -- Screenshot; denied returns no ``uri``.
- ``handle_notification`` -- Notification; boolean gate, denied drops the
  notification.

Each handler decides strictly against the kernel-attested
``(pid, starttime)`` for the resolved client -- never an app-supplied
``pid`` / ``app_id``.
"""
from __future__ import annotations

import re
import sys
from urllib.parse import quote
from typing import Any, Callable

import qdistro_proc_identity as pi  # type: ignore[import-not-found]
import qdistro_resolver as resolver_mod  # type: ignore[import-not-found]
from qdistro_portal_backend import _run_file_picker  # type: ignore[import-not-found]

# Frontend-owned portal name + object (the real one, owned per silo).
PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJ_PATH = "/org/freedesktop/portal/desktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"

# Admin broker coordinates on the system bus.
BROKER_BUS = "org.qdistro.AdminBroker1"
BROKER_OBJ = "/org/qdistro/AdminBroker1"

# Portal response codes (xdg-desktop-portal spec).
RESP_SUCCESS = 0
RESP_CANCELLED = 1
RESP_OTHER = 2

_APPID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _sanitize_app_id(app_id: Any) -> str:
    """Reduce an app-claimed app_id to the broker-detail charset.

    The value is ADVISORY ONLY — the broker overrides it with the
    launcher-attested app_id for the resolved pid (enforce mode) or logs
    the divergence (shadow mode). We sanitize purely so a hostile app
    can't smuggle control characters into audit/detail strings.
    """
    s = str(app_id or "unknown")[:128]
    s = _APPID_RE.sub("_", s).strip("._-")
    return s or "unknown"


def app_id_for_client_pid(client_pid: int,
                          resolver: Callable[[int], Any] | None = None
                          ) -> str:
    """Resolve a stable advisory app id for action scoping.

    The broker still overrides identity from the launch record for the
    attested pid/starttime. This value is only the cache/audit action
    suffix, so it must be stable across D-Bus reconnects and must not be
    the transient sender name.
    """
    if resolver is None:
        resolver = lambda pid: resolver_mod.resolve_subject(pid, None)
    try:
        subject = resolver(int(client_pid))
    except Exception:
        return "unknown"
    for attr in ("app_id", "sandbox_engine"):
        try:
            value = getattr(subject, attr, "")
        except Exception:
            value = ""
        if value:
            return _sanitize_app_id(value)
    return "unknown"


def _portal_action(base: str, app_id: str) -> str:
    """Scope a portal approval to the requesting app_id (advisory)."""
    return f"{base}:{_sanitize_app_id(app_id)}"


def resolve_client_tuple(client_pid: int) -> tuple[int, int, bool]:
    """Resolve the app's own connection pid to ``(pid, starttime, ok)``.

    ``client_pid`` is the pid the *kernel* reported for the app's D-Bus
    connection (``GetConnectionUnixProcessID`` / SO_PEERCRED) — NEVER a
    value the app supplied in the message body. We read the starttime from
    ``/proc`` here so the broker's relayed tuple is anchored against
    PID reuse: a process that vanished (or whose pid was recycled) reads a
    starttime of 0 and we fail closed (``ok=False``).

    Returns ``(pid, starttime, ok)``. On any failure ``ok`` is False and
    the caller must refuse the request without consulting the broker.
    """
    try:
        pid = int(client_pid)
    except (TypeError, ValueError):
        return (-1, 0, False)
    if pid <= 0:
        return (-1, 0, False)
    starttime = pi.read_starttime(pid)
    if not starttime:  # 0 -> gone / unreadable / malformed: fail closed
        return (pid, 0, False)
    return (pid, int(starttime), True)


def build_broker_call(action: str, app_id: str,
                      extra: dict[str, str] | None = None
                      ) -> tuple[str, dict[str, str]]:
    """Build the ``(action, details)`` pair handed to the broker.

    Crucially this does NOT place any pid or app-asserted identity that the
    broker would *trust*: the (client_pid, client_starttime) travel as
    separate, kernel-attested broker arguments, and the broker overrides
    app_id/sandbox_engine from the launch record. The ``app_id`` we put in
    ``details`` is the app's claim, forwarded only for shadow-mode
    comparison / audit — never authoritative.
    """
    app = _sanitize_app_id(app_id)
    details: dict[str, str] = {"app_id": app}
    if extra:
        for k, v in extra.items():
            details[str(k)] = str(v)
    return _portal_action(action, app), details


def decision_to_response(verdict: str) -> int:
    """Map a broker verdict to an xdg-desktop-portal Response code.

    - ``allow``   -> ``RESP_SUCCESS``   (0)
    - ``deny``    -> ``RESP_CANCELLED`` (1)
    - anything else (``unknown`` / unexpected) -> ``RESP_CANCELLED``

    Fail-closed default: an unrecognised verdict is treated as a refusal,
    never a success.
    """
    if verdict == "allow":
        return RESP_SUCCESS
    return RESP_CANCELLED


class BrokerClient:
    """Relay to the root broker's portal-client methods.

    Pure handlers depend on this interface, not on dbus, so they test with
    a stub. The production glib-backed implementation lives in ``_main``.
    The two methods mirror
    ``CheckPermissionForClient`` (sync gate) and
    ``RequestPermissionForClient`` (fire-and-forget admin prompt).
    """

    def check_for_client(self, action: str, details: dict[str, str],
                         client_pid: int, client_starttime: int) -> str:
        raise NotImplementedError

    def request_for_client(self, action: str, details: dict[str, str],
                           client_pid: int, client_starttime: int) -> int:
        raise NotImplementedError


def attested_decision(base_action: str, app_id: str, *,
                      client_pid: int,
                      broker: BrokerClient,
                      extra: dict[str, str] | None = None,
                      resolver: Callable[[int], tuple[int, int, bool]]
                      = resolve_client_tuple) -> int:
    """Shared kernel-attested decision core for every portal method.

    ``client_pid`` is the kernel-attested pid of the app's *own* D-Bus
    connection (resolved by the D-Bus wrapper via
    ``GetConnectionUnixProcessID``). ``app_id`` is the app's claim and is
    forwarded only as an advisory detail.

    Steps (all fail-closed):

    1. Resolve ``client_pid`` to ``(pid, starttime, ok)`` via ``/proc``.
       On ``ok=False`` (gone / recycled / unreadable) return
       ``RESP_CANCELLED`` WITHOUT calling the broker — an unauthenticatable
       client gets no decision.
    2. Call ``broker.check_for_client`` with the action/details and the
       *resolved* (pid, starttime) — never any app-supplied pid.
    3. On ``unknown`` fire ``broker.request_for_client`` (so admin sees a
       prompt for next time) and return cancelled.
    4. Map the verdict to a portal Response code.

    Any exception talking to the broker is treated as a denial
    (fail-closed), mirroring the backend's ``CheckPermission`` posture.

    Returns a portal Response code; ``RESP_SUCCESS`` ONLY on an explicit
    ``allow`` verdict for the resolved client.
    """
    pid, starttime, ok = resolver(client_pid)
    if not ok:
        return RESP_CANCELLED
    action, details = build_broker_call(base_action, app_id, extra)
    try:
        verdict = str(broker.check_for_client(action, details, pid, starttime))
    except Exception:  # noqa: BLE001 — broker down / timeout -> fail closed
        return RESP_CANCELLED
    if verdict == "unknown":
        try:
            broker.request_for_client(action, details, pid, starttime)
        except Exception:  # noqa: BLE001 — best-effort prompt
            pass
        return RESP_CANCELLED
    return decision_to_response(verdict)


def handle_access(base_action: str, app_id: str, *,
                  client_pid: int,
                  broker: BrokerClient,
                  extra: dict[str, str] | None = None,
                  resolver: Callable[[int], tuple[int, int, bool]]
                  = resolve_client_tuple) -> int:
    """Pure handler for the representative portal Access method.

    Thin wrapper over :func:`attested_decision` (passing the caller's
    ``base_action`` through, for the existing test suite and the Access
    D-Bus glue). ``app_id`` is advisory only; see ``attested_decision``
    for the fail-closed decision steps.
    """
    return attested_decision(base_action, app_id,
                             client_pid=client_pid, broker=broker,
                             extra=extra, resolver=resolver)


def handle_open_file(app_id: str, *, client_pid: int, broker: BrokerClient,
                     extra: dict[str, str] | None = None,
                     title: str = "",
                     options: dict[str, Any] | None = None,
                     picker: Callable[..., list[str]] | None = _run_file_picker,
                     resolver: Callable[[int], tuple[int, int, bool]]
                     = resolve_client_tuple) -> tuple[int, dict[str, Any]]:
    """Pure handler for ``FileChooser.OpenFile`` on the attested core.

    Routes ``com.qdistro.fs.open`` (the backend's FileChooser action) for
    the *resolved* client. On ``allow`` runs the picker and returns
    ``uris``. On ANY non-allow / fail-closed path returns
    ``(RESP_CANCELLED, {})`` with **no files**: a denied client must never
    receive a file URI.
    """
    response = attested_decision("com.qdistro.fs.open", app_id,
                                 client_pid=client_pid, broker=broker,
                                 extra=extra, resolver=resolver)
    if response != RESP_SUCCESS:
        return (response, {})
    return _file_chooser_results(title=title, options=options,
                                 save=False, picker=picker)


def handle_save_file(app_id: str, *, client_pid: int, broker: BrokerClient,
                     extra: dict[str, str] | None = None,
                     title: str = "",
                     options: dict[str, Any] | None = None,
                     picker: Callable[..., list[str]] | None = _run_file_picker,
                     resolver: Callable[[int], tuple[int, int, bool]]
                     = resolve_client_tuple) -> tuple[int, dict[str, Any]]:
    """Pure handler for ``FileChooser.SaveFile`` on the attested core.

    Shares the FileChooser action/contract with :func:`handle_open_file`.
    """
    response = attested_decision("com.qdistro.fs.open", app_id,
                                 client_pid=client_pid, broker=broker,
                                 extra=extra, resolver=resolver)
    if response != RESP_SUCCESS:
        return (response, {})
    return _file_chooser_results(title=title, options=options,
                                 save=True, picker=picker)


def handle_screenshot(app_id: str, *, client_pid: int, broker: BrokerClient,
                      extra: dict[str, str] | None = None,
                      resolver: Callable[[int], tuple[int, int, bool]]
                      = resolve_client_tuple) -> tuple[int, dict[str, Any]]:
    """Pure handler for ``Screenshot`` on the attested core.

    Routes ``com.qdistro.screen.capture`` for the *resolved* client. On
    ``allow`` returns ``RESP_OTHER`` until the compositor screencopy path
    can return a real ``uri``. On any non-allow / fail-closed path returns
    ``(RESP_CANCELLED, {})`` with no ``uri``.
    """
    response = attested_decision("com.qdistro.screen.capture", app_id,
                                 client_pid=client_pid, broker=broker,
                                 extra=extra, resolver=resolver)
    if response == RESP_SUCCESS:
        return (RESP_OTHER, {})
    return (response, {})


def _file_chooser_results(*, title: str = "",
                          options: dict[str, Any] | None = None,
                          save: bool = False,
                          picker: Callable[..., list[str]] | None
                          = _run_file_picker
                          ) -> tuple[int, dict[str, Any]]:
    if picker is None:
        return (RESP_OTHER, {})
    opts = dict(options) if options else {}
    picker_title = str(title or opts.get(
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
    paths = picker(title=picker_title, save=save, multiple=multiple,
                   directory=directory, current_folder=current_folder)
    if not paths:
        return (RESP_CANCELLED, {})
    return (RESP_SUCCESS, {
        "uris": ["file://" + quote(str(path), safe="/") for path in paths],
    })


def handle_notification(app_id: str, *, client_pid: int, broker: BrokerClient,
                        extra: dict[str, str] | None = None,
                        resolver: Callable[[int], tuple[int, int, bool]]
                        = resolve_client_tuple) -> bool:
    """Pure handler for ``Notification.AddNotification`` on the core.

    The Notification interface has no Request/Response object, so the
    contract is a boolean: ``True`` means the notification is allowed to
    be shown, ``False`` means it must be dropped. An unauthenticatable
    client, a broker exception, or any non-``allow`` verdict all
    fail-closed to ``False`` (the notification is silently dropped).
    """
    response = attested_decision("portal.notification", app_id,
                                 client_pid=client_pid, broker=broker,
                                 extra=extra, resolver=resolver)
    return response == RESP_SUCCESS


# --------------------------------------------------------------------- #
# D-Bus glue (production only; skipped in unit tests via import guard)
# --------------------------------------------------------------------- #

def _main() -> int:  # pragma: no cover - requires a live silo session bus
    import signal

    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    sys_bus = dbus.SystemBus()

    def _broker_iface():
        proxy = sys_bus.get_object(BROKER_BUS, BROKER_OBJ)
        return dbus.Interface(proxy, BROKER_BUS)

    class _DbusBrokerClient(BrokerClient):
        def check_for_client(self, action, details, client_pid,
                             client_starttime):
            iface = _broker_iface()
            return str(iface.CheckPermissionForClient(
                action,
                dbus.Dictionary(details, signature="sv"),
                dbus.UInt32(client_pid),
                dbus.UInt64(client_starttime)))

        def request_for_client(self, action, details, client_pid,
                               client_starttime):
            iface = _broker_iface()
            return int(iface.RequestPermissionForClient(
                action,
                dbus.Dictionary(details, signature="sv"),
                dbus.UInt32(client_pid),
                dbus.UInt64(client_starttime)))

    broker = _DbusBrokerClient()

    def _conn_pid(sender) -> int:
        """Kernel-attested pid of the *app's own* connection."""
        proxy = bus.get_object("org.freedesktop.DBus",
                               "/org/freedesktop/DBus")
        ifc = dbus.Interface(proxy, "org.freedesktop.DBus")
        return int(ifc.GetConnectionUnixProcessID(sender))

    class _Request(dbus.service.Object):
        """org.freedesktop.portal.Request — carries the async Response.

        The portal Request/Response object protocol: the portal method
        returns a Request object path immediately; the result is delivered
        later via the Request's ``Response`` signal. We export the object,
        emit ``Response`` synchronously after the broker verdict, then
        unexport.
        """

        def __init__(self, session_bus, handle_path):
            self._path = handle_path
            super().__init__(session_bus, handle_path)

        @dbus.service.signal(REQUEST_IFACE, signature="ua{sv}")
        def Response(self, response, results):
            pass

        @dbus.service.method(REQUEST_IFACE, in_signature="",
                             out_signature="")
        def Close(self):
            try:
                self.remove_from_connection()
            except Exception:  # noqa: BLE001
                pass

        def finish(self, response_code, results=None):
            self.Response(dbus.UInt32(response_code),
                          dbus.Dictionary(results or {}, signature="sv"))
            try:
                self.remove_from_connection()
            except Exception:  # noqa: BLE001
                pass

    class PortalFrontend(dbus.service.Object):
        def __init__(self):
            self._busname = dbus.service.BusName(
                PORTAL_BUS_NAME, bus, do_not_queue=True)
            super().__init__(bus, PORTAL_OBJ_PATH)
            self._req_seq = 0

        @staticmethod
        def _handle_token(options) -> str:
            try:
                token = str(dict(options or {}).get("handle_token", ""))
            except Exception:
                token = ""
            token = re.sub(r"[^A-Za-z0-9_]+", "_", token)[:128]
            return token

        def _new_request(self, sender, options=None) -> "_Request":
            self._req_seq += 1
            token = self._handle_token(options) or f"qdistro_{self._req_seq}"
            base = sender.replace(":", "").replace(".", "_") or "x"
            path = f"/org/freedesktop/portal/desktop/request/{base}/{token}"
            return _Request(bus, path)

        @staticmethod
        def _finish_later(req, response, results):
            GLib.idle_add(lambda: (req.finish(response, results), False)[1])

        @staticmethod
        def _run_later(callback):
            GLib.idle_add(lambda: (callback(), False)[1])

        @dbus.service.method(
            "org.freedesktop.portal.Access",
            in_signature="osssssa{sv}", out_signature="o",
            sender_keyword="sender")
        def AccessDialog(self, parent_window, app_id, title, subtitle,
                         body, options, sender=None):
            """Representative gated portal method.

            Resolves the app's OWN connection pid (NOT any app-supplied
            value), relays the kernel-attested (pid, starttime) to the
            broker via ``handle_access``, and delivers the verdict through
            the Request object's Response signal.
            """
            req = self._new_request(sender, options)
            client_pid = self._resolve_caller(sender)
            response = handle_access(
                "portal.access", str(app_id or ""),
                client_pid=client_pid, broker=broker)
            self._finish_later(req, response, {})
            return req._path

        def _resolve_caller(self, sender) -> int:
            """Kernel-attested pid of the app's OWN connection (or -1).

            Always resolves from the D-Bus daemon's view of the connection,
            never from anything in the message body. On any failure returns
            -1 so the pure core's resolver fails closed.
            """
            try:
                return _conn_pid(sender)
            except dbus.DBusException:
                return -1

        @dbus.service.method(
            "org.freedesktop.portal.FileChooser",
            in_signature="ssa{sv}", out_signature="o",
            sender_keyword="sender")
        def OpenFile(self, parent_window, title, options, sender=None):
            """Gated FileChooser.OpenFile.

            Resolves the app's OWN connection pid (NOT any app-supplied
            value) and relays the kernel-attested (pid, starttime) to the
            broker via ``handle_open_file``. On allow the real picker would
            run here and populate ``uris``; a denied request delivers an
            empty result (no files).
            """
            req = self._new_request(sender, options)
            client_pid = self._resolve_caller(sender)
            opts = dict(options or {})
            title_s = str(title or "")

            def _work():
                app_id = app_id_for_client_pid(client_pid)
                response, results = handle_open_file(
                    app_id, client_pid=client_pid, broker=broker,
                    title=title_s, options=opts)
                req.finish(response, results)

            self._run_later(_work)
            return req._path

        @dbus.service.method(
            "org.freedesktop.portal.FileChooser",
            in_signature="ssa{sv}", out_signature="o",
            sender_keyword="sender")
        def SaveFile(self, parent_window, title, options, sender=None):
            """Gated FileChooser.SaveFile (see ``OpenFile``)."""
            req = self._new_request(sender, options)
            client_pid = self._resolve_caller(sender)
            opts = dict(options or {})
            title_s = str(title or "")

            def _work():
                app_id = app_id_for_client_pid(client_pid)
                response, results = handle_save_file(
                    app_id, client_pid=client_pid, broker=broker,
                    title=title_s, options=opts)
                req.finish(response, results)

            self._run_later(_work)
            return req._path

        @dbus.service.method(
            "org.freedesktop.portal.Screenshot",
            in_signature="sa{sv}", out_signature="o",
            sender_keyword="sender")
        def Screenshot(self, parent_window, options, sender=None):
            """Gated Screenshot.

            Same attested core: resolves the caller's own connection pid
            and relays it to the broker via ``handle_screenshot``. On allow
            the compositor screencopy would run and fill ``uri``; a denied
            request returns an empty result.
            """
            req = self._new_request(sender, options)
            client_pid = self._resolve_caller(sender)
            app_id = app_id_for_client_pid(client_pid)
            response, results = handle_screenshot(
                app_id, client_pid=client_pid, broker=broker,
                extra={k: str(v) for k, v in dict(options or {}).items()})
            self._finish_later(req, response, results)
            return req._path

        @dbus.service.method(
            "org.freedesktop.portal.Notification",
            in_signature="sa{sv}", out_signature="",
            sender_keyword="sender")
        def AddNotification(self, id_, notification, sender=None):
            """Gated Notification.AddNotification.

            No Request/Response object (the portal spec gives this method
            no reply). Resolves the caller's own connection pid and relays
            it to the broker via ``handle_notification``; if not allowed
            the notification is silently dropped.
            """
            client_pid = self._resolve_caller(sender)
            app_id = app_id_for_client_pid(client_pid)
            if handle_notification(
                    app_id, client_pid=client_pid, broker=broker,
                    extra={"notification_id": str(id_ or "")}):
                # Allowed: the glue would forward to the host notification
                # surface here. The attested gate has passed.
                pass

        @dbus.service.method(
            "org.freedesktop.portal.Notification",
            in_signature="s", out_signature="",
            sender_keyword="sender")
        def RemoveNotification(self, id_, sender=None):
            """Gated Notification.RemoveNotification.

            Policy-checked identically so a denied app cannot remove
            notifications it did not create.
            """
            client_pid = self._resolve_caller(sender)
            app_id = app_id_for_client_pid(client_pid)
            if handle_notification(
                    app_id, client_pid=client_pid, broker=broker,
                    extra={"notification_id": str(id_ or "")}):
                pass

    PortalFrontend()
    print(f"[qdistro-portal-frontend] listening on {PORTAL_BUS_NAME} "
          f"{PORTAL_OBJ_PATH}", flush=True)
    loop = GLib.MainLoop()

    def _on_term(*_a):
        loop.quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, _on_term, None)
    GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, _on_term, None)
    loop.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
