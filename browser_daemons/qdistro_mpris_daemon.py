#!/usr/bin/env python3
"""qdistro-mpris — Phase-9e cross-uid browser MPRIS republisher.

Owns ``org.qdistro.Mpris`` on the SESSION bus (object
``/org/qdistro/Mpris``, interface ``org.qdistro.Mpris1``). The browser
bridge — running as the *originating* user — forwards
``mpris.publish`` here via ``Mpris1.Publish(s body)`` whenever a tab's
``MediaSession`` metadata or playback state changes.

For every distinct (uid, browser) source we own a second well-known
name on the bus —
``org.mpris.MediaPlayer2.qdistro.<user>.<browser>`` — that exports the
standard ``org.mpris.MediaPlayer2`` + ``org.mpris.MediaPlayer2.Player``
interfaces. The admin's media widget (which already aggregates every
``org.mpris.MediaPlayer2.*`` peer it can see) thus shows browser media
from any user's silo with no extra wiring, and ``Play`` / ``Pause`` /
``PlayPause`` invoked on that exported player are routed back through
the bridge to the originating tab as an inbound ``mpris.control`` op.

Auth: every ``Publish`` is gated by
``qdistro_browser_daemon_identity.daemon_forward_allowed`` — the caller
must be EITHER the qdistro native-messaging host (``/proc`` executed-
script + allowlisted parent-browser exe) OR the first-party qdbrowser
process (``/proc`` executed-script match against the installed
``qdbrowser`` entry point). Both are kernel-attested; same anchors the
pwd daemon uses for browser-credential ops. The kernel-attested caller
uid (SO_PEERCRED via the bus) is what selects the ``<user>`` segment of
the player name; a body field can NOT spoof it.

The published metadata is presentation-only. No security-sensitive data
flows; the value is the cross-uid surfacing.

Bus model: like ``qdistro-user-relay``, one instance runs per user
session (``WantedBy=default.target``). dbus-broker rejects cross-uid
session-bus peers, so each user owns their own ``org.qdistro.Mpris`` and
publishes the standard ``org.mpris.MediaPlayer2.*`` players that the
admin's session aggregates.

The dispatch core (``handle_publish`` / ``handle_control``) is pure and
takes an injectable ``publisher`` sink + ``bridge_client`` so it unit-
tests without a real bus, mirroring the bridge's ``_dbus_client``
pattern.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from typing import Any

from qdistro_browser_daemon_identity import (  # type: ignore[import-not-found]
    browser_label,
    caller_advisory,
    daemon_forward_allowed,
    read_proc_ppid,
    username_for_uid,
)

BUS_NAME = "org.qdistro.Mpris"
OBJ_PATH = "/org/qdistro/Mpris"
IFACE = "org.qdistro.Mpris1"

# Exported standard-MPRIS player name template. <user>/<browser> are
# sanitised to the D-Bus member charset before interpolation.
MPRIS_NAME_FMT = "org.mpris.MediaPlayer2.qdistro.{user}.{browser}"
MPRIS_OBJ_PATH = "/org/mpris/MediaPlayer2"

# Well-known name template the browser bridge owns for its *inbound*
# surface (qdistro_browser_bridge.py: ``org.qdistro.BrowserBridge.<ppid>``
# where ppid is the bridge's parent — i.e. the browser pid). The bridge
# exposes a single method ``RequestTabs(s op, s args_json) -> s reply``;
# an ``mpris.control`` op delivered there is routed down to the
# originating tab. We derive the name from the kernel-attested bridge pid
# at publish time (its PPid is the same value the bridge interpolated),
# never from a body field — so a control op can only ever be addressed
# back to the bridge connection that actually published the player.
BRIDGE_NAME_FMT = "org.qdistro.BrowserBridge.{ppid}"
BRIDGE_OBJ_PATH = "/org/qdistro/BrowserBridge"
BRIDGE_IFACE = "org.qdistro.BrowserBridge1"
BRIDGE_METHOD = "RequestTabs"

_VALID_STATUSES = frozenset({"Playing", "Paused", "Stopped"})
# Chromium mediaSession.playbackState values + a few synonyms the
# extension may forward, mapped to the MPRIS PlaybackStatus enum.
_STATUS_MAP = {
    "playing": "Playing", "play": "Playing",
    "paused": "Paused", "pause": "Paused",
    "stopped": "Stopped", "stop": "Stopped", "none": "Stopped",
    "": "Stopped",
}

_NAME_SEG_RE = re.compile(r"[^A-Za-z0-9]")


def _sanitize_name_segment(seg: str) -> str:
    """Reduce an arbitrary string to ``[A-Za-z0-9_]`` so it is safe to
    splice into a D-Bus well-known name. Empty input -> ``unknown``."""
    cleaned = _NAME_SEG_RE.sub("_", seg or "")
    cleaned = cleaned.strip("_")
    return cleaned or "unknown"


def normalize_playback_status(value: Any) -> str:
    """Map a forwarded playback status to the MPRIS PlaybackStatus enum.

    Accepts the already-canonical MPRIS values as well as the lowercase
    mediaSession state names. Unknown values fall back to ``Stopped`` so
    the exported player is always in a valid state.
    """
    if isinstance(value, str) and value in _VALID_STATUSES:
        return value
    return _STATUS_MAP.get(str(value or "").strip().lower(), "Stopped")


def player_name_for(uid: int, parent_exe: str) -> str:
    """Build the ``org.mpris.MediaPlayer2.qdistro.<user>.<browser>`` name
    for a (kernel-attested uid, advisory parent-exe) source."""
    user = _sanitize_name_segment(username_for_uid(uid))
    browser = _sanitize_name_segment(browser_label(parent_exe))
    return MPRIS_NAME_FMT.format(user=user, browser=browser)


def build_metadata(req: dict[str, Any], player_key: str) -> dict[str, Any]:
    """Build an ``org.mpris.MediaPlayer2.Player.Metadata`` dict from a
    forwarded publish body. Only well-known MPRIS keys are emitted; the
    track id is derived from the source so the admin widget can tell two
    browsers' tracks apart.
    """
    title = str(req.get("title") or "")
    artist = str(req.get("artist") or "")
    album = str(req.get("album") or "")
    tab_id = req.get("tab_id")
    trackid = ("/org/qdistro/mpris/"
               + _sanitize_name_segment(player_key)
               + "/" + _sanitize_name_segment(str(tab_id if tab_id is not None
                                                  else "0")))
    meta: dict[str, Any] = {"mpris:trackid": trackid}
    if title:
        meta["xesam:title"] = title
    if artist:
        meta["xesam:artist"] = [artist]
    if album:
        meta["xesam:album"] = album
    return meta


class _BasePublisher:
    """Sink that owns the exported ``org.mpris.MediaPlayer2.*`` players.

    The pure handler calls :meth:`publish` with the resolved player name,
    PlaybackStatus, Metadata, and the control-routing context
    (``bridge_name`` / ``uid`` / ``browser``) bound to that player; the
    production glib-backed implementation claims the bus name lazily,
    pushes ``PropertiesChanged`` and wires the exported Play/Pause/etc
    methods back through that bridge. Tests record the calls instead.
    """

    def publish(self, player_name: str, playback_status: str,
                metadata: dict[str, Any], *, bridge_name: str = "",
                uid: int = -1, browser: str = "") -> None:
        raise NotImplementedError


def bridge_bus_name_for_pid(bridge_pid: int,
                            *, ppid_reader=read_proc_ppid) -> str:
    """Derive the bridge's inbound bus name from its kernel-attested pid.

    The bridge owns ``org.qdistro.BrowserBridge.<ppid>`` where ``ppid`` is
    its *own* parent (the browser pid, ``os.getppid()`` at bridge start).
    We read that same PPid from ``/proc/<bridge_pid>/status`` so the
    control op can only be addressed back to the connection that actually
    published — there is no body field involved. Returns ``""`` when the
    PPid is unreadable (racing/exited bridge), which fails the control
    closed downstream.
    """
    ppid = ppid_reader(int(bridge_pid))
    if not ppid:
        return ""
    return BRIDGE_NAME_FMT.format(ppid=int(ppid))


class _BridgeClient:
    """Routes an inbound ``mpris.control`` back to the originating bridge.

    The admin's media widget calls ``Play`` / ``Pause`` on the exported
    player; the publisher resolves which bridge connection published it
    (bound at publish time to a concrete ``org.qdistro.BrowserBridge.<ppid>``
    bus name) and asks this client to deliver the control op there via the
    bridge's ``RequestTabs("mpris.control", {...})`` inbound surface — the
    same path the bridge already routes down to the originating tab's
    media element (extension ``handleMprisControlInbound``).

    The actual D-Bus ``call(bus_name, op, args_json) -> dict`` is injected
    so the routing logic (op name, args shape, error envelope) unit-tests
    without a session bus, mirroring the bridge's own ``_dbus_client`` and
    qdbrowser's ``DaemonForwarder`` injectable-client pattern. Production
    binds a jeepney-backed client lazily (see ``_main``).
    """

    def __init__(self, bridge_bus_name: str,
                 call: Callable[[str, str, str], dict] | None = None):
        self._bus_name = bridge_bus_name
        self._call = call

    def control(self, uid: int, browser: str, action: str,
                tab_id: Any) -> dict:
        """Deliver an ``mpris.control`` op to the bound bridge.

        ``uid`` / ``browser`` are carried for audit/symmetry but are NOT a
        privilege: the bridge connection is already pinned to the
        publishing (uid, browser) by its bus name, so a control can never
        be steered at a different user's bridge. The extension's inbound
        handler keys off ``action`` + ``tab_id``.
        """
        if not self._bus_name:
            return {"ok": False, "error": "no_bridge"}
        if self._call is None:  # pragma: no cover - production lazy bind
            self._call = _jeepney_bridge_call
        args = json.dumps({"action": action, "tab_id": tab_id,
                           "uid": int(uid), "browser": str(browser or "")})
        return dict(self._call(self._bus_name, "mpris.control", args))


def handle_publish(req: dict[str, Any], *, caller_uid: int, caller_pid: int,
                   publisher: _BasePublisher,
                   bridge_gate: Callable[..., tuple[bool, str]]
                   = daemon_forward_allowed,
                   bridge_name_fn: Callable[[int], str]
                   = bridge_bus_name_for_pid) -> dict:
    """Pure ``mpris.publish`` core.

    ``req`` is the decoded bridge body; ``caller_uid`` / ``caller_pid``
    are the kernel-attested SO_PEERCRED values resolved by the D-Bus
    method wrapper (or supplied directly by tests).

    Besides updating the exported player, this binds the player to the
    *originating bridge's* inbound bus name (derived from the attested
    ``caller_pid``), so a later Play/Pause on the exported player routes
    the control back to exactly that bridge connection. The binding is
    handed to ``publisher.publish`` as ``bridge_name``; a publisher that
    does not export controls may ignore it.

    Returns the JSON-able reply dict. Fails closed (``parent_not_allowed``)
    when the caller is not the qdistro browser bridge.
    """
    allowed, reason = bridge_gate(caller_pid)
    if not allowed:
        return {"ok": False, "error": "parent_not_allowed", "reason": reason}
    _ext, parent_exe = caller_advisory(req)
    player_name = player_name_for(caller_uid, parent_exe)
    status = normalize_playback_status(req.get("playback_status"))
    metadata = build_metadata(req, player_name)
    browser = browser_label(parent_exe)
    bridge_name = bridge_name_fn(caller_pid)
    try:
        publisher.publish(player_name, status, metadata,
                          bridge_name=bridge_name, uid=caller_uid,
                          browser=browser)
    except Exception as e:  # noqa: BLE001 — never crash the RPC on a sink error
        return {"ok": False, "error": "publish_failed",
                "detail": str(e)[:200]}
    return {"ok": True, "player": player_name, "playback_status": status}


def handle_control(action: str, *, uid: int, browser: str, tab_id: Any,
                   bridge_client: _BridgeClient) -> dict:
    """Pure ``mpris.control`` core — route a Play/Pause/etc back to the
    originating browser tab through the bridge. Validates the action
    against the MPRIS control verbs before forwarding."""
    verb = str(action or "").strip().lower()
    if verb not in ("play", "pause", "playpause", "next", "previous",
                    "stop"):
        return {"ok": False, "error": "bad_action", "action": action}
    try:
        return dict(bridge_client.control(uid, browser, verb, tab_id))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "control_failed",
                "detail": str(e)[:200]}


# --------------------------------------------------------------------- #
# D-Bus glue (production only; skipped in unit tests via import guard)
# --------------------------------------------------------------------- #

def _jeepney_bridge_call(bridge_bus_name: str, op: str,
                         args_json: str) -> dict:  # pragma: no cover
    """Default jeepney-backed call into a bridge's inbound surface.

    Invokes ``RequestTabs(op, args_json) -> reply_json`` on the bridge's
    ``org.qdistro.BrowserBridge.<ppid>`` connection over the SESSION bus
    and decodes the JSON reply. Mirrors the bridge's own
    ``_JeepneyDBusClient.call`` / qdbrowser's ``_jeepney_session_call``.
    """
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    addr = DBusAddress(BRIDGE_OBJ_PATH, bus_name=bridge_bus_name,
                       interface=BRIDGE_IFACE)
    msg = new_method_call(addr, BRIDGE_METHOD, "ss", (op, args_json))
    conn = open_dbus_connection(bus="SESSION")
    try:
        reply = conn.send_and_get_reply(msg, timeout=5.0)
    finally:
        conn.close()
    if reply.header.message_type.name == "ERROR":
        return {"ok": False, "error": "dbus_error",
                "detail": str(reply.body)[:200]}
    if reply.body and isinstance(reply.body[0], str):
        try:
            return json.loads(reply.body[0])
        except ValueError:
            return {"ok": True, "raw": reply.body[0]}
    return {"ok": True, "body": list(reply.body)}


def _main() -> int:  # pragma: no cover - requires a live session bus
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    class _GlibPublisher(_BasePublisher):
        """Owns one exported MediaPlayer2 object per (uid, browser)."""

        def __init__(self, session_bus):
            self._bus = session_bus
            self._players: dict[str, _ExportedPlayer] = {}

        def publish(self, player_name, playback_status, metadata, *,
                    bridge_name="", uid=-1, browser=""):
            player = self._players.get(player_name)
            if player is None:
                player = _ExportedPlayer(self._bus, player_name)
                self._players[player_name] = player
            # Re-bind the control route on every publish: the bridge pid
            # (hence its inbound bus name) can change across browser
            # restarts while the (uid, browser) player name is stable.
            player.bind_control(bridge_name, uid, browser)
            player.update(playback_status, metadata)

    class _ExportedPlayer(dbus.service.Object):
        def __init__(self, session_bus, player_name):
            self._name = dbus.service.BusName(player_name, session_bus)
            super().__init__(session_bus, MPRIS_OBJ_PATH)
            self._status = "Stopped"
            self._metadata: dict[str, Any] = {}
            self._bridge_name = ""
            self._uid = -1
            self._browser = ""
            self._tab_id = None

        def bind_control(self, bridge_name, uid, browser):
            self._bridge_name = bridge_name or ""
            self._uid = int(uid)
            self._browser = str(browser or "")

        def update(self, status, metadata):
            self._status = status
            self._metadata = metadata
            # Remember the active tab so a control op can be routed to the
            # exact media element the metadata came from.
            tid = metadata.get("mpris:trackid", "")
            if isinstance(tid, str) and "/" in tid:
                self._tab_id = tid.rsplit("/", 1)[-1]
            self.PropertiesChanged(
                "org.mpris.MediaPlayer2.Player",
                {"PlaybackStatus": status, "Metadata": metadata}, [])

        def _route(self, verb):
            """Dispatch an exported-player control verb back to the bound
            bridge via the pure ``handle_control`` core."""
            client = _BridgeClient(self._bridge_name,
                                   call=_jeepney_bridge_call)
            return handle_control(verb, uid=self._uid, browser=self._browser,
                                  tab_id=self._tab_id, bridge_client=client)

        @dbus.service.method("org.mpris.MediaPlayer2.Player")
        def PlayPause(self):
            self._route("playpause")

        @dbus.service.method("org.mpris.MediaPlayer2.Player")
        def Play(self):
            self._route("play")

        @dbus.service.method("org.mpris.MediaPlayer2.Player")
        def Pause(self):
            self._route("pause")

        @dbus.service.method("org.mpris.MediaPlayer2.Player")
        def Stop(self):
            self._route("stop")

        @dbus.service.method("org.mpris.MediaPlayer2.Player")
        def Next(self):
            self._route("next")

        @dbus.service.method("org.mpris.MediaPlayer2.Player")
        def Previous(self):
            self._route("previous")

        @dbus.service.signal("org.freedesktop.DBus.Properties",
                             signature="sa{sv}as")
        def PropertiesChanged(self, iface, changed, invalidated):
            pass

    publisher = _GlibPublisher(bus)

    class MprisDaemon(dbus.service.Object):
        def __init__(self):
            self._busname = dbus.service.BusName(BUS_NAME, bus)
            super().__init__(bus, OBJ_PATH)

        def _peer(self, sender):
            proxy = bus.get_object("org.freedesktop.DBus",
                                   "/org/freedesktop/DBus")
            ifc = dbus.Interface(proxy, "org.freedesktop.DBus")
            uid = int(ifc.GetConnectionUnixUser(sender))
            pid = int(ifc.GetConnectionUnixProcessID(sender))
            return uid, pid

        @dbus.service.method(IFACE, in_signature="s", out_signature="s",
                             sender_keyword="sender")
        def Publish(self, body, sender=None):
            try:
                req = json.loads(body)
                if not isinstance(req, dict):
                    raise ValueError
            except (TypeError, ValueError):
                return json.dumps({"ok": False, "error": "malformed_body"})
            uid, pid = self._peer(sender)
            return json.dumps(handle_publish(
                req, caller_uid=uid, caller_pid=pid, publisher=publisher))

    MprisDaemon()
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
