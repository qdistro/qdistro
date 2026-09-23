"""qdistro bridge adapter — Track 02 D-Bus surface.

When qdistro daemons are present on the session bus, this plugin owns a
per-process well-known D-Bus name (``org.qdistro.QdBrowser.<pid>``) and
exposes qdbrowser's tabs / page / downloads / media surface to the
qdistro daemons through ``org.qdistro.QdBrowser1``. Outbound signals
(``TabAdded``, ``TabRemoved``, ``DownloadStarted``, ``MediaStateChanged``)
let the daemons follow qdbrowser without polling.

This is the qdbrowser-side of the same protocol the WebExtension
native-messaging bridge speaks for Firefox / Chrome — same daemons,
no extension hop. The protocol contract lives in
``todo/browser/02-qdbrowser-unification.md``.

Auth model: every inbound call is gated by per-op polkit actions
(``org.qdistro.qdbrowser.tabs.open`` etc.) using ``pkcheck`` against
the caller's PID, matching the rest of qdistro.

Track-03 (agent guardrails) owns rate-limiting / audit-logging — this
file flags those gaps with ``TODO(track-03)`` comments rather than
implementing them locally.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import queue
import select
import subprocess
import threading
from collections.abc import Callable
from typing import Any

from qdbrowser.config import Config
from qdbrowser.plugin import Plugin

log = logging.getLogger("qdbrowser.bridge_adapter")

_UNSET = object()


# qdistro daemon D-Bus well-known names we probe for. Presence of any
# one of them is enough to flip the adapter active.
#
# Note: the pwd daemon's canonical well-known name is
# ``org.qdistro.Pwd1`` on the SYSTEM bus
# (see qdistro/pwd/qdistro_pwd_daemon.py). The full ``org.qdistro.*``
# rename has landed across the tree, so no legacy alias is kept here.
_DAEMON_NAMES = (
    "org.qdistro.Browser1",
    "org.qdistro.Downloads",
    "org.qdistro.Mpris",
    "org.qdistro.Notifications",
    "org.qdistro.Compositor",
    "org.qdistro.Pwd1",
)
_SYSTEM_DAEMON_NAMES = (
    "org.qdistro.Pwd1",
    "org.qdistro.AdminBroker1",
)


# Interface name for the qdbrowser-side surface. Track-01 will dispatch
# inbound calls against this; the matching well-known bus name is
# ``org.qdistro.QdBrowser.<pid>`` so admin can fan out to every running
# qdbrowser instance the user owns.
QDBROWSER_IFACE = "org.qdistro.QdBrowser1"
QDBROWSER_PATH = "/org/qdistro/QdBrowser"


# D-Bus introspection XML for the surface. Held as a constant so the
# tests can validate signatures without standing up a real bus.
QDBROWSER_INTROSPECTION_XML = """\
<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.qdistro.QdBrowser1">

    <!-- Tabs -->
    <method name="TabsList">
      <arg type="a(uss)" name="tabs" direction="out"/>
    </method>
    <method name="TabsOpen">
      <arg type="s" name="url" direction="in"/>
      <arg type="u" name="id" direction="out"/>
    </method>
    <method name="TabsClose">
      <arg type="u" name="id" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
    </method>

    <!-- Page -->
    <method name="PageExtract">
      <arg type="u" name="tab_id" direction="in"/>
      <arg type="s" name="mode" direction="in"/>
      <arg type="s" name="title" direction="out"/>
      <arg type="s" name="url" direction="out"/>
      <arg type="s" name="content" direction="out"/>
    </method>

    <!-- Downloads -->
    <method name="DownloadsList">
      <arg type="a(usu)" name="downloads" direction="out"/>
    </method>

    <!-- Media (MPRIS bridge) -->
    <method name="MediaStatus">
      <arg type="s" name="title" direction="out"/>
      <arg type="s" name="artist" direction="out"/>
      <arg type="s" name="state" direction="out"/>
    </method>

    <!-- History + Bookmarks (step 3) -->
    <method name="HistorySearch">
      <arg type="s" name="query" direction="in"/>
      <arg type="u" name="limit" direction="in"/>
      <arg type="a(sss)" name="results" direction="out"/>
    </method>
    <method name="BookmarksSearch">
      <arg type="s" name="query" direction="in"/>
      <arg type="u" name="limit" direction="in"/>
      <arg type="a(ss)" name="results" direction="out"/>
    </method>

    <!-- Outbound signals: emitted from window/downloads/media -->
    <signal name="TabAdded">
      <arg type="u" name="id"/>
      <arg type="s" name="url"/>
    </signal>
    <signal name="TabRemoved">
      <arg type="u" name="id"/>
    </signal>
    <signal name="DownloadStarted">
      <arg type="u" name="id"/>
      <arg type="s" name="filename"/>
    </signal>
    <signal name="MediaStateChanged">
      <arg type="s" name="state"/>
    </signal>

  </interface>
</node>
"""


# --------------------------------------------------------------------- #
# Polkit gate
# --------------------------------------------------------------------- #


# Mapping of bridge method name → polkit action id. Read-only methods
# whose policy entry is `allow:yes` still go through pkcheck so the
# enforcement path is uniform; pkcheck returns success for unauth'd
# actions on those.
METHOD_TO_ACTION = {
    "TabsList":         "org.qdistro.qdbrowser.tabs.list",
    "TabsOpen":         "org.qdistro.qdbrowser.tabs.open",
    "TabsClose":        "org.qdistro.qdbrowser.tabs.close",
    "PageExtract":      "org.qdistro.qdbrowser.page.extract",
    "DownloadsList":    "org.qdistro.qdbrowser.downloads.list",
    "MediaStatus":      "org.qdistro.qdbrowser.media.status",
    "HistorySearch":    "org.qdistro.qdbrowser.history.search",
    "BookmarksSearch":  "org.qdistro.qdbrowser.bookmarks.search",
    # Cookies.export not yet wired into a method; the action is reserved
    # for the eventual handler.
}


# Actions that need no polkit check (read-only *inventory* of the live
# session — open tabs, current media, in-flight downloads). pkcheck
# would also pass them via allow:yes, but skipping the subprocess is
# faster and the audit story stays clean.
#
# History and bookmarks are deliberately NOT here (finding #12): they
# expose privacy-sensitive browsing metadata that can be bulk-exfiltr-
# ated, so every caller must pass an explicit polkit authorization
# (auth_active in the policy) — there is no allow-on-disabled path.
_OPEN_ACTIONS = {
    "org.qdistro.qdbrowser.tabs.list",
    "org.qdistro.qdbrowser.media.status",
    "org.qdistro.qdbrowser.downloads.list",
}


# Upper bound on history/bookmarks search results (finding #12): cap
# how much browsing metadata a single authorized query can drain.
_MAX_SEARCH_LIMIT = 200


def _clamp_search_limit(limit: Any) -> int:
    """Coerce a caller-supplied result limit into [1, _MAX_SEARCH_LIMIT]."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return _MAX_SEARCH_LIMIT
    if n <= 0:
        return _MAX_SEARCH_LIMIT
    return min(n, _MAX_SEARCH_LIMIT)


def polkit_check(action_id: str, caller_pid: int | None,
                 caller_start_time: int | None = None,
                 pkcheck_bin: str = "pkcheck") -> bool:
    """Return True iff polkit authorises ``action_id`` for ``caller_pid``.

    Read-only actions short-circuit to True without a subprocess. For
    mutating actions we shell out to ``pkcheck`` — this matches the
    pattern used elsewhere in qdistro (there is no decent python-polkit
    binding).

    ``caller_pid`` of ``None`` means "internal call" (e.g. tests, the
    plugin's own activation path); polkit is skipped.
    """
    if action_id in _OPEN_ACTIONS:
        return True
    if caller_pid is None:
        return True
    args = [pkcheck_bin, "--action-id", action_id,
            "--process", str(caller_pid)]
    if caller_start_time is not None:
        # pkcheck expects pid,start_time[,uid]. Pass pid,start_time so
        # polkit refuses if the pid has been recycled between auth and
        # check.
        args = [pkcheck_bin, "--action-id", action_id,
                "--process", f"{caller_pid},{caller_start_time}"]
    args.append("--allow-user-interaction")
    try:
        result = subprocess.run(args, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("pkcheck failed for %s pid=%s: %s",
                    action_id, caller_pid, exc)
        return False
    return result.returncode == 0


# --------------------------------------------------------------------- #
# Proxies — duck-typed views over qdbrowser internals
# --------------------------------------------------------------------- #


class TabsProxy:
    """Adapter from the qdbrowser MainWindow into the bridge tabs API.

    The bridge handlers never import PyQt widgets directly; they only
    call into this proxy. That keeps the unit-test surface clean — a
    test can supply any object with the same method shape.
    """

    def __init__(self, window):
        self._window = window

    def list(self) -> list[tuple[int, str, str]]:
        out: list[tuple[int, str, str]] = []
        win = self._window
        if win is None or not hasattr(win, "_tabs"):
            return out
        for i in range(win._tabs.count()):
            split = win._tabs.widget(i)
            views = []
            if hasattr(split, "find_webviews"):
                views = split.find_webviews()
            if not views:
                continue
            wv = views[0]
            # 02/S9: never expose private (off-the-record) tabs to the bridge.
            # An agent/extension must not learn a private tab exists, its
            # title, or its URL.
            if getattr(wv, "is_off_the_record", False):
                continue
            try:
                tid = int(getattr(wv, "stable_id",
                                  getattr(wv, "_stable_id", 0)))
                title = wv.title() if callable(getattr(wv, "title", None)) else ""
                url = wv.url() if callable(getattr(wv, "url", None)) else ""
            except Exception:
                continue
            out.append((tid, title or "", url or ""))
        return out

    def open(self, url: str) -> int:
        win = self._window
        if win is None or not hasattr(win, "new_tab"):
            raise RuntimeError("no window")
        wv = win.new_tab(url=url or "about:blank")
        return int(getattr(wv, "stable_id", getattr(wv, "_stable_id", 0)))

    def close(self, tab_id: int) -> bool:
        win = self._window
        if win is None or not hasattr(win, "_tabs"):
            return False
        for i in range(win._tabs.count()):
            split = win._tabs.widget(i)
            if not hasattr(split, "find_webviews"):
                continue
            for wv in split.find_webviews():
                if int(getattr(wv, "stable_id",
                               getattr(wv, "_stable_id", -1))) == int(tab_id):
                    # 02/S9: refuse to close a private (off-the-record) tab.
                    # Hiding it from TabsList is not an authorization boundary
                    # (stable ids are monotonic and guessable), so the close
                    # path must enforce the boundary itself. Fail closed if the
                    # privacy flag can't be read.
                    try:
                        otr = bool(getattr(wv, "is_off_the_record", False))
                    except Exception:
                        otr = True
                    if otr:
                        raise PermissionError(
                            "close denied for private (off-the-record) tab")
                    win._on_tab_close_requested(i)
                    return True
        return False


class PagesProxy:
    """Wraps page extraction. Modes: text | html | selection."""

    _VALID_MODES = ("text", "html", "selection")

    def __init__(self, window, run_js: Callable | None = None):
        self._window = window
        # ``run_js`` is the synchronous-with-timeout helper from
        # agent_control. We accept it as a constructor arg so tests
        # don't need to spin up the whole plugin graph.
        self._run_js = run_js

    def _find_webview(self, tab_id: int):
        win = self._window
        if win is None or not hasattr(win, "_tabs"):
            return None
        for i in range(win._tabs.count()):
            split = win._tabs.widget(i)
            if not hasattr(split, "find_webviews"):
                continue
            for wv in split.find_webviews():
                if int(getattr(wv, "stable_id",
                               getattr(wv, "_stable_id", -1))) == int(tab_id):
                    return wv
        return None

    def extract(self, tab_id: int, mode: str) -> tuple[str, str, str]:
        if mode not in self._VALID_MODES:
            raise ValueError(f"unknown mode: {mode!r}")
        wv = self._find_webview(tab_id)
        if wv is None:
            raise LookupError(f"no tab {tab_id}")
        # 02/S9: refuse to extract content from a private (off-the-record) tab.
        # PageExtract is the bridge's read-the-page surface; a private tab's
        # text/HTML/selection must never leave the browser via an agent.
        if getattr(wv, "is_off_the_record", False):
            raise PermissionError(
                "page extract denied for private (off-the-record) tab")
        title = wv.title() if callable(getattr(wv, "title", None)) else ""
        url = wv.url() if callable(getattr(wv, "url", None)) else ""
        # JS expressions for each mode — mirror agent_control verbs.
        if mode == "text":
            script = "document.body ? document.body.innerText : ''"
        elif mode == "html":
            script = ("document.documentElement "
                      "? document.documentElement.outerHTML : ''")
        else:  # selection
            script = "window.getSelection ? window.getSelection().toString() : ''"
        content = ""
        if self._run_js is not None:
            try:
                content = self._run_js(wv, script) or ""
            except Exception as exc:
                log.warning("page extract JS failed: %s", exc)
                content = ""
        return (title or "", url or "", str(content))


class DownloadsProxy:
    """Snapshot of the downloads side-panel state."""

    # State strings match qdbrowser's QWebEngineDownloadRequest states.
    _STATE_MAP = {0: "requested", 1: "in_progress", 2: "completed",
                  3: "cancelled", 4: "interrupted"}

    def __init__(self, downloads_plugin):
        self._plugin = downloads_plugin

    def list(self) -> list[tuple[int, str, int]]:
        out: list[tuple[int, str, int]] = []
        plug = self._plugin
        if plug is None:
            return out
        panel = getattr(plug, "_panel", None)
        if panel is None:
            return out
        # Active items first, then a slice of recent history.
        is_otr = getattr(plug, "_request_is_off_the_record", None)
        for idx, (_item, widget) in enumerate(getattr(panel, "_items", [])):
            try:
                req = getattr(widget, "_request", None)
                # 02/S9: never expose a private (off-the-record) download. Use
                # the marker set in add_active; if it is somehow missing, derive
                # it from the live request (fail closed — treat unknown as
                # private rather than leak it).
                marker = getattr(widget, "_private", None)
                if marker is None and is_otr is not None:
                    marker = is_otr(req)
                if marker:
                    continue
                path = widget.path()
                state = req.state() if req is not None else 0
                if hasattr(state, "value"):
                    state = state.value
                out.append((idx, os.path.basename(path), int(state)))
            except Exception:
                continue
        # Historical (finished) downloads use index >= 1<<16 so a tab id
        # space conflict can't arise.
        for hidx, entry in enumerate(getattr(panel, "_history", [])[-25:]):
            try:
                out.append((
                    (1 << 16) + hidx,
                    os.path.basename(str(entry.get("path", ""))),
                    2,  # completed
                ))
            except Exception:
                continue
        return out


class MediaProxy:
    """Best-effort media snapshot.

    Real MPRIS forwarding is daemon-side; here we just expose what the
    qdbrowser process knows. Track 02 wires a minimal state — Track 04
    is responsible for the full picture_in_picture / MPRIS plumb-through.
    """

    def __init__(self):
        self.title = ""
        self.artist = ""
        self.state = "stopped"  # one of: stopped | playing | paused

    def status(self) -> tuple[str, str, str]:
        return (self.title, self.artist, self.state)

    def update(self, title: str = "", artist: str = "",
               state: str = "stopped") -> None:
        self.title = title or ""
        self.artist = artist or ""
        self.state = state or "stopped"


# --------------------------------------------------------------------- #
# Outbound forwarder — Track 02 step 4 (Downloads + MPRIS)
# --------------------------------------------------------------------- #

# The Phase-9e SESSION-bus daemons qdbrowser forwards to. Same bus
# names + interfaces the WebExtension native bridge calls
# (qdistro/browser_daemons/), so qdbrowser participates in the exact
# same desktop-integration surface with no extension hop.
_DOWNLOADS_DAEMON = ("org.qdistro.Downloads", "/org/qdistro/Downloads",
                     "org.qdistro.Downloads1", "Notify")
_MPRIS_DAEMON = ("org.qdistro.Mpris", "/org/qdistro/Mpris",
                 "org.qdistro.Mpris1", "Publish")

# Map qdbrowser's QWebEngineDownloadRequest state ints to the
# chrome.downloads state strings the Downloads daemon expects (its
# handle_notify only surfaces UI on a terminal "complete").
_DL_STATE_TO_WIRE = {
    0: "in_progress",   # requested
    1: "in_progress",   # downloading
    2: "complete",      # completed
    3: "interrupted",   # cancelled
    4: "interrupted",   # interrupted
}

# qdbrowser is a first-party qdistro browser, not a third-party browser
# launched by an RPM binary, so it cannot satisfy the daemons'
# `browser_bridge_allowed` gate (no native-messaging host script, no
# allowlisted parent-browser exe). The daemon side now carries an
# EXPLICIT, narrow allowance for it: `daemon_forward_allowed` admits a
# caller whose kernel-attested executed script (read from /proc, never
# the body) is the installed `qdbrowser` entry point
# (`qdbrowser_forwarder_allowed` in qdistro_browser_daemon_identity.py).
# So these forwards are now ACCEPTED when this process is the real
# qdbrowser — and still fail closed (`parent_not_allowed`) for anything
# else. The marker below stays ADVISORY only: it labels the forward as
# qdbrowser-sourced for the player-name suffix / audit, but the daemon
# never trusts it for the security decision — caller uid comes from
# SO_PEERCRED and the allow decision from the /proc executed-script.
_QDBROWSER_MARKER = "qdbrowser"


class DaemonForwarder:
    """Forwards qdbrowser downloads + media to the Phase-9e daemons.

    Pure D-Bus call surface: the actual ``call(bus, service, path,
    interface, method, body)`` is injected so the forwarding logic
    (field mapping, state translation, fire-and-forget error handling)
    unit-tests without a session bus — the same injectable-client
    pattern the bridge uses for ``_dbus_client``.

    A ``None`` caller (default) binds a jeepney-backed client lazily.
    Every call is best-effort: a missing daemon / bus error is swallowed
    (logged) so a transient daemon outage never breaks a download.
    """

    def __init__(self, call: Callable[..., dict] | None = None):
        self._call = call

    def _client(self) -> Callable[..., dict]:
        if self._call is not None:
            return self._call
        self._call = _jeepney_session_call
        return self._call

    def notify_download(self, download_id: int, filename: str,
                        state: int, *, url: str = "", mime: str = "",
                        total_bytes: int = 0,
                        bytes_received: int = 0) -> dict:
        """Forward a download state change to ``org.qdistro.Downloads``.

        ``state`` is the qdbrowser/QWebEngine state int; it is translated
        to the daemon's wire-state string. Returns the daemon reply dict
        (or an ``{"ok": False, ...}`` envelope on a transport error)."""
        body = {
            "download_id": int(download_id),
            "filename": str(filename or ""),
            "state": _DL_STATE_TO_WIRE.get(int(state), "in_progress"),
            "url": str(url or ""),
            "mime": str(mime or ""),
            "total_bytes": int(total_bytes or 0),
            "bytes_received": int(bytes_received or 0),
            "parent_exe": _QDBROWSER_MARKER,
            "extension_id": _QDBROWSER_MARKER,
        }
        return self._forward(_DOWNLOADS_DAEMON, body)

    def publish_media(self, *, title: str = "", artist: str = "",
                      album: str = "", state: str = "stopped",
                      position_us: int = 0,
                      tab_id: int | None = None) -> dict:
        """Forward a media snapshot to ``org.qdistro.Mpris`` so the admin
        media widget shows qdbrowser playback alongside Firefox/Chrome."""
        body = {
            "title": str(title or ""),
            "artist": str(artist or ""),
            "album": str(album or ""),
            "playback_status": str(state or "stopped"),
            "position_us": int(position_us or 0),
            "tab_id": tab_id,
            "parent_exe": _QDBROWSER_MARKER,
            "extension_id": _QDBROWSER_MARKER,
        }
        return self._forward(_MPRIS_DAEMON, body)

    def _forward(self, daemon: tuple, body: dict) -> dict:
        service, path, interface, method = daemon
        try:
            return self._client()(
                "SESSION", service, path, interface, method,
                json.dumps(body))
        except Exception as exc:  # noqa: BLE001 — never break on a daemon outage
            log.warning("daemon forward to %s.%s failed: %s",
                        service, method, exc)
            return {"ok": False, "error": "forward_failed",
                    "detail": str(exc)[:200]}


def _jeepney_session_call(bus: str, service: str, path: str,
                          interface: str, method: str,
                          body_json: str) -> dict:  # pragma: no cover
    """Default jeepney-backed SESSION-bus call. Mirrors the bridge's
    _JeepneyDBusClient.call: send one string arg, decode the JSON reply.
    """
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    addr = DBusAddress(path, bus_name=service, interface=interface)
    msg = new_method_call(addr, method, "s", (body_json,))
    conn = open_dbus_connection(bus=bus)
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


class HistoryProxy:
    """Read-only view over the history plugin for bridge protocol ops.

    Searches the history store and returns ``(url, title, timestamp)``
    triples. The timestamp is ISO-8601 (string) so it survives D-Bus
    without custom type marshalling.
    """

    def __init__(self, history_plugin):
        self._plugin = history_plugin

    def search(self, query: str, limit: int = 50
               ) -> list[tuple[str, str, str]]:
        plug = self._plugin
        if plug is None:
            return []
        store = getattr(plug, "_store", None)
        if store is None:
            return []
        q = query.lower().strip()
        results: list[tuple[str, str, str]] = []
        for rec in store.all():
            if limit and len(results) >= limit:
                break
            url = rec.get("url", "")
            title = rec.get("title", "")
            ts = rec.get("ts", 0)
            if q and q not in url.lower() and q not in title.lower():
                continue
            # Format timestamp as ISO-8601 string for D-Bus transport.
            try:
                ts_str = datetime.datetime.fromtimestamp(
                    float(ts), tz=datetime.UTC
                ).isoformat()
            except (ValueError, OSError, OverflowError):
                ts_str = ""
            results.append((url, title or "", ts_str))
        return results


class BookmarksProxy:
    """Read-only view over the bookmarks plugin for bridge protocol ops.

    Returns ``(url, title)`` pairs matching the query.
    """

    def __init__(self, bookmarks_plugin):
        self._plugin = bookmarks_plugin

    def search(self, query: str, limit: int = 50
               ) -> list[tuple[str, str]]:
        plug = self._plugin
        if plug is None:
            return []
        panel = getattr(plug, "_panel", None)
        if panel is None:
            return []
        q = query.lower().strip()
        results: list[tuple[str, str]] = []
        for b in panel.all():
            if limit and len(results) >= limit:
                break
            url = b.get("url", "")
            title = b.get("title", "")
            if q and q not in url.lower() and q not in title.lower():
                continue
            results.append((url, title or ""))
        return results


# --------------------------------------------------------------------- #
# Method dispatcher
# --------------------------------------------------------------------- #


class BridgeAdapterHandlers:
    """Pure-Python method dispatcher.

    The jeepney bus loop translates an incoming message into a
    ``(method_name, args, caller_pid)`` triple and calls
    :meth:`dispatch`. Returns ``(body_tuple, signature)`` ready to wrap
    in a method-return message.

    Splitting the dispatcher out of the plugin keeps it (a) jeepney-
    free for unit tests and (b) reusable from the Qt-D-Bus path the
    qdshell side will eventually want.
    """

    def __init__(self, tabs: TabsProxy, pages: PagesProxy,
                 downloads: DownloadsProxy, media: MediaProxy,
                 polkit: Callable[[str, int | None], bool] = polkit_check,
                 history: HistoryProxy | None = None,
                 bookmarks: BookmarksProxy | None = None):
        self.tabs = tabs
        self.pages = pages
        self.downloads = downloads
        self.media = media
        self.history = history
        self.bookmarks = bookmarks
        self._polkit = polkit

    def authorize(self, method: str, caller_pid: int | None = None,
                  caller_start_time: int | None = None) -> None:
        """Resolve+enforce the polkit gate for ``method``.

        Runs the (potentially slow / interactive) polkit check WITHOUT
        touching any Qt widget, so it is safe to call from the D-Bus
        receive thread rather than the GUI thread. Raises
        ``LookupError`` for an unknown method and ``PermissionError``
        when polkit denies the action — in which case the caller must
        NOT proceed to :meth:`invoke`.

        ``caller_start_time`` (the kernel process start time of the
        caller) is forwarded to the polkit hook so polkit can refuse if
        the caller PID was recycled between authorization and check.
        The hook is invoked with ``(action, caller_pid)`` for backward
        compatibility, and additionally with ``caller_start_time`` as a
        keyword when it accepts it.
        """
        action = METHOD_TO_ACTION.get(method)
        if action is None:
            raise LookupError(f"unknown method {method!r}")
        if not self._call_polkit(action, caller_pid, caller_start_time):
            # TODO(track-03): rate-limit denied calls per caller PID
            # once the agent-guardrails audit hook lands.
            raise PermissionError(f"polkit denied {action}")

    @staticmethod
    def method_needs_pkcheck(method: str) -> bool:
        """True iff ``method`` is gated by a real ``pkcheck`` for an
        external caller (i.e. its action is not a read-only
        ``_OPEN_ACTIONS`` inventory call). Used to decide whether a
        missing caller start time must fail closed."""
        action = METHOD_TO_ACTION.get(method)
        return action is not None and action not in _OPEN_ACTIONS

    def _polkit_accepts_start_time(self) -> bool:
        """Whether the configured polkit hook accepts a
        ``caller_start_time`` keyword. Computed by signature inspection
        (cached) so we never have to swallow a TypeError from the hook's
        own body to discover this."""
        cached = getattr(self, "_polkit_start_time_ok", None)
        if cached is not None:
            return cached
        accepts = True
        try:
            import inspect
            sig = inspect.signature(self._polkit)
            params = sig.parameters
            has_kw = "caller_start_time" in params
            has_var_kw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in params.values())
            accepts = has_kw or has_var_kw
        except (TypeError, ValueError):
            # Un-introspectable callable (some C builtins) — assume it
            # does NOT take the keyword and use the 2-arg form.
            accepts = False
        self._polkit_start_time_ok = accepts
        return accepts

    def _call_polkit(self, action: str, caller_pid: int | None,
                     caller_start_time: int | None) -> bool:
        """Invoke the polkit hook, passing ``caller_start_time`` when the
        hook signature accepts it (the default :func:`polkit_check`
        does). Test/sibling hooks supplying only ``(action, pid)`` keep
        working unchanged. We decide by inspecting the hook signature
        rather than catching ``TypeError``, so a ``TypeError`` raised
        from inside a start-time-aware hook is never silently downgraded
        to the weaker (no-start-time) check."""
        if caller_start_time is None or not self._polkit_accepts_start_time():
            return self._polkit(action, caller_pid)
        return self._polkit(action, caller_pid,
                            caller_start_time=caller_start_time)

    def dispatch(self, method: str, args: tuple,
                 caller_pid: int | None = None,
                 caller_start_time: int | None = None
                 ) -> tuple[tuple, str]:
        """Authorize then invoke ``method`` in one call.

        Convenience wrapper used by unit tests and any in-process
        caller. The D-Bus receive path instead calls :meth:`authorize`
        on the receive thread and :meth:`invoke` on the GUI thread so
        the polkit check never blocks the GUI and the mutating op never
        runs after an authorization failure.
        """
        self.authorize(method, caller_pid, caller_start_time)
        return self.invoke(method, args, caller_pid=caller_pid)

    def invoke(self, method: str, args: tuple,
               caller_pid: int | None = None) -> tuple[tuple, str]:
        """Execute an already-authorized ``method``.

        MUST run on the GUI thread (it touches Qt proxies). Callers are
        responsible for having passed :meth:`authorize` first; this
        method does NOT re-check polkit.
        """
        if method == "TabsList":
            return ((self.tabs.list(),), "a(uss)")
        if method == "TabsOpen":
            (url,) = args
            return ((self.tabs.open(url),), "u")
        if method == "TabsClose":
            (tab_id,) = args
            return ((self.tabs.close(int(tab_id)),), "b")
        if method == "PageExtract":
            tab_id, mode = args
            title, url, content = self.pages.extract(int(tab_id), str(mode))
            return ((title, url, content), "sss")
        if method == "DownloadsList":
            return ((self.downloads.list(),), "a(usu)")
        if method == "MediaStatus":
            return (self.media.status(), "sss")
        if method == "HistorySearch":
            query, limit = args
            limit = _clamp_search_limit(limit)
            proxy = self.history
            if proxy is None:
                return (([],), "a(sss)")
            results = proxy.search(str(query), limit)
            # Audit (finding #12): history is privacy-sensitive and now
            # requires polkit authorization; record who queried what.
            log.info("HistorySearch authorized: pid=%s query_len=%d limit=%d hits=%d",
                     caller_pid, len(str(query)), limit, len(results))
            return ((results[:limit],), "a(sss)")
        if method == "BookmarksSearch":
            query, limit = args
            limit = _clamp_search_limit(limit)
            proxy = self.bookmarks
            if proxy is None:
                return (([],), "a(ss)")
            results = proxy.search(str(query), limit)
            log.info("BookmarksSearch authorized: pid=%s query_len=%d limit=%d hits=%d",
                     caller_pid, len(str(query)), limit, len(results))
            return ((results[:limit],), "a(ss)")
        raise LookupError(f"unhandled method {method!r}")


# --------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------- #


def _daemons_available() -> bool:
    """Best-effort probe: does any qdistro daemon own a well-known name
    on the user session bus?

    Pure jeepney to keep the probe lightweight and dependency-free of
    Qt's D-Bus binding. Any failure (no jeepney, no bus, RPC error)
    returns False — qdbrowser stays standalone.
    """
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        return False
    names: set[str] = set()
    for bus_kind, probe_names in (
            ("SESSION", _DAEMON_NAMES),
            ("SYSTEM", _SYSTEM_DAEMON_NAMES)):
        try:
            conn = open_dbus_connection(bus=bus_kind)
        except Exception:
            continue
        try:
            bus = DBusAddress(
                "/org/freedesktop/DBus",
                bus_name="org.freedesktop.DBus",
                interface="org.freedesktop.DBus",
            )
            reply = conn.send_and_get_reply(
                new_method_call(bus, "ListNames"), timeout=2.0)
            bus_names = set(reply.body[0]) if reply.body else set()
            # Only keep names this bus is responsible for so a cross-
            # bus impostor can't pose as a probed daemon.
            names.update(bus_names.intersection(probe_names))
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return bool(names)


def _enabled_config_override() -> bool | None:
    """Return explicit bridge_adapter config, or None for autodetect."""
    value = Config().get("plugins", "bridge_adapter", default=_UNSET)
    if value is _UNSET:
        return None
    if isinstance(value, dict):
        if "enabled" not in value:
            return None
        return bool(value.get("enabled"))
    return bool(value)


# --------------------------------------------------------------------- #
# Outbound-forward worker (keeps D-Bus forwards off the GUI thread)
# --------------------------------------------------------------------- #


class _ForwardWorker:
    """Single background thread that runs outbound daemon forwards.

    Outbound forwards (download / media state) open a fresh session-bus
    connection and block up to 5 s. They are triggered from Qt signal
    handlers (``emit_download_started`` / ``emit_media_state_changed``)
    which run on the GUI thread — doing the blocking call inline froze
    the UI whenever a Phase-9e daemon was slow or hung. This worker
    moves the call off the GUI thread: ``submit`` enqueues a callable
    and returns immediately; a dedicated daemon thread drains the queue.

    The queue is bounded so a wedged daemon can't grow it without limit;
    once full, the oldest pending forward is dropped (forwards are
    best-effort desktop-integration hints, not durable events).
    """

    _MAX_PENDING = 256

    def __init__(self):
        self._queue: queue.Queue = queue.Queue(self._MAX_PENDING)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="bridge_adapter_forward")
        self._thread.start()

    def submit(self, fn: Callable[[], Any]) -> None:
        """Enqueue ``fn`` for execution on the worker thread.

        Never blocks the caller (the GUI thread). If the queue is full
        (daemon wedged), drop the oldest pending item to make room so
        the newest state still gets a chance to be delivered."""
        if self._thread is None:
            # Not started (e.g. tests, or no daemons) — run inline so
            # behaviour is unchanged for callers that never start it.
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("inline forward failed: %s", exc)
            return
        try:
            self._queue.put_nowait(fn)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(fn)
            except queue.Full:
                log.debug("forward queue full; dropping forward")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                fn = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — never die on a forward
                log.warning("background forward failed: %s", exc)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None


# --------------------------------------------------------------------- #
# Thread-safe dispatch helper
# --------------------------------------------------------------------- #


class _DispatchHelper:
    """Bounces handler dispatch from the D-Bus recv thread to the main
    thread so Qt widgets are only touched from the GUI thread.

    The recv thread calls :meth:`call_on_main_thread` which posts a
    callable into a queue and waits (with timeout) for the main thread
    to execute it. The main thread is woken by a signal on a QObject
    whose thread affinity is the QApplication's thread, so the queued
    delivery runs there. (A ``QTimer.singleShot(0, fn)`` posted from the
    recv thread belongs to THAT thread, which has no event loop, so it
    never fired and every bridge call timed out.) Called on the main
    thread itself, the signal is delivered directly — no deadlock.

    If no Qt event loop is running (e.g. unit tests) the helper falls
    back to direct invocation in the calling thread.
    """

    def __init__(self):
        self._queue: list = []
        self._lock = threading.Lock()
        self._waker = None

    def _get_waker(self):
        """Lazily build the main-thread waker (needs a QApplication)."""
        with self._lock:
            if self._waker is None:
                from PyQt6.QtCore import QObject, pyqtSignal
                from PyQt6.QtWidgets import QApplication

                helper = self

                class _Waker(QObject):
                    wake = pyqtSignal()

                    def drain(self):
                        helper._drain()

                waker = _Waker()
                # Affinity decides where queued slots run: pin it to the
                # GUI thread even when first used from the recv thread.
                waker.moveToThread(QApplication.instance().thread())
                waker.wake.connect(waker.drain)
                self._waker = waker
            return self._waker

    def _qt_app_running(self) -> bool:
        """Return True if a QApplication exists (i.e. we have a real
        event loop to post to). In unit tests without QApplication,
        we fall back to direct invocation."""
        try:
            from PyQt6.QtWidgets import QApplication
            return QApplication.instance() is not None
        except ImportError:
            return False

    def call_on_main_thread(self, fn: Callable, timeout: float = 10.0
                            ) -> Any:
        """Execute ``fn()`` on the Qt main thread and return its result.

        Blocks the calling thread until the main thread has finished
        or ``timeout`` seconds have elapsed (raises ``TimeoutError``).
        """
        if not self._qt_app_running():
            # No Qt event loop (unit tests, headless) — run directly.
            return fn()

        result_holder: dict = {"value": None, "exc": None, "done": False}
        done_event = threading.Event()

        def _run():
            try:
                result_holder["value"] = fn()
            except Exception as exc:
                result_holder["exc"] = exc
            finally:
                result_holder["done"] = True
                done_event.set()

        with self._lock:
            self._queue.append(_run)

        # Wake the main thread to drain the queue.
        try:
            self._get_waker().wake.emit()
        except Exception:
            # Fallback: execute directly (e.g. no QApp).
            _run()
            done_event.set()

        if not done_event.wait(timeout=timeout):
            raise TimeoutError("main-thread dispatch timed out")

        if result_holder["exc"] is not None:
            raise result_holder["exc"]
        return result_holder["value"]

    def _drain(self):
        with self._lock:
            pending = list(self._queue)
            self._queue.clear()
        for fn in pending:
            fn()


# --------------------------------------------------------------------- #
# Plugin
# --------------------------------------------------------------------- #


class BridgeAdapterPlugin(Plugin):
    name = "bridge_adapter"
    description = "Publishes qdbrowser state to qdistro daemons via D-Bus."
    version = "0.2"
    capabilities = ["bridge_adapter"]

    def __init__(self):
        super().__init__()
        self._active = False
        self._window = None
        self._conn = None
        # Separate connection for PID lookups so that
        # send_and_get_reply doesn't consume inbound method-call
        # messages from the main receive connection.
        self._pid_conn = None
        self._bus_name: str | None = None
        self._handlers: BridgeAdapterHandlers | None = None
        self.tabs_proxy: TabsProxy | None = None
        self.pages_proxy: PagesProxy | None = None
        self.downloads_proxy: DownloadsProxy | None = None
        self.media_proxy: MediaProxy | None = None
        self.history_proxy: HistoryProxy | None = None
        self.bookmarks_proxy: BookmarksProxy | None = None
        # Step-4 outbound forwarder to the Phase-9e Downloads/MPRIS daemons.
        self.forwarder: DaemonForwarder | None = None
        self._media_connections: dict = {}
        self._audible_tabs: set[int] = set()
        self._media_tabs: set[int] = set()
        self._recv_thread: threading.Thread | None = None
        self._dispatch_helper = _DispatchHelper()
        # Runs outbound daemon forwards off the GUI thread.
        self._forward_worker = _ForwardWorker()
        self._stop = threading.Event()

    @property
    def active(self) -> bool:
        return self._active

    # -- public hooks exposed for tests + sibling plugins ----

    @property
    def bus_name(self) -> str | None:
        """The per-pid well-known D-Bus name this adapter claims."""
        return self._bus_name

    @property
    def handlers(self) -> BridgeAdapterHandlers | None:
        return self._handlers

    def emit_tab_added(self, tab_id: int, url: str) -> None:
        self._emit_signal("TabAdded", (int(tab_id), str(url)), "us")

    def emit_tab_removed(self, tab_id: int) -> None:
        self._emit_signal("TabRemoved", (int(tab_id),), "u")

    def emit_download_started(self, download_id: int, filename: str,
                              state: int = 0, **kw) -> None:
        self._emit_signal(
            "DownloadStarted", (int(download_id), str(filename)), "us")
        self.forward_download_state(download_id, filename, 0, **kw)

    def forward_download_state(self, download_id: int, filename: str,
                               state: int = 0, **kw) -> None:
        # Step-4: also forward to the Downloads daemon so the admin
        # notification area sees qdbrowser downloads. Best-effort; the
        # forwarder swallows transport errors. The actual D-Bus call
        # (which can block up to 5 s) runs on the forward worker thread,
        # NOT the GUI thread that invoked this signal handler — snapshot
        # the args now and hand them off.
        if self.forwarder is None:
            return
        fwd = self.forwarder
        url = str(kw.get("url", ""))
        mime = str(kw.get("mime", ""))
        total_bytes = int(kw.get("total_bytes", 0) or 0)
        bytes_received = int(kw.get("bytes_received", 0) or 0)
        self._forward_worker.submit(
            lambda: fwd.notify_download(
                download_id, filename, state,
                url=url, mime=mime,
                total_bytes=total_bytes, bytes_received=bytes_received))

    def emit_media_state_changed(self, state: str, *, title: str = "",
                                 artist: str = "",
                                 tab_id: int | None = None) -> None:
        self._emit_signal("MediaStateChanged", (str(state),), "s")
        # Step-4: republish via the MPRIS daemon. Pull the current
        # title/artist off the media proxy so the admin widget shows
        # metadata, not just a bare state. The blocking D-Bus call runs
        # on the forward worker thread, not the GUI thread — snapshot
        # the metadata here (on the GUI thread, where reading the proxy
        # is safe) and hand the call off.
        if self.forwarder is None:
            return
        fwd = self.forwarder
        if self.media_proxy is not None:
            proxy_title, proxy_artist, _ = self.media_proxy.status()
            title = title or proxy_title
            artist = artist or proxy_artist
        self._forward_worker.submit(
            lambda: fwd.publish_media(
                title=title, artist=artist, state=str(state),
                tab_id=tab_id))

    # -- lifecycle ----

    def activate(self, app_controller):
        self._window = app_controller
        explicit = _enabled_config_override()
        if explicit is False:
            log.info("bridge_adapter disabled by config")
            return
        if explicit is not True and not _daemons_available():
            log.info(
                "qdistro daemons not detected on the session bus; "
                "bridge_adapter staying inactive")
            return

        # Build the proxies. They are duck-typed so unit tests can
        # bypass them entirely; here we wire to the real window.
        downloads_plugin = None
        try:
            downloads_plugin = app_controller.plugins.get_by_capability(
                "side_panel")
            downloads_plugin = next(
                (p for p in downloads_plugin if getattr(p, "name", "")
                 == "downloads"), None)
        except Exception:
            downloads_plugin = None

        run_js = None
        try:
            ac_plugin = app_controller.plugins._instances.get("agent_control")
            if ac_plugin is not None and hasattr(ac_plugin, "_run_js"):
                run_js = ac_plugin._run_js
        except Exception:
            run_js = None

        self.tabs_proxy = TabsProxy(app_controller)
        self.pages_proxy = PagesProxy(app_controller, run_js=run_js)
        self.downloads_proxy = DownloadsProxy(downloads_plugin)
        self.media_proxy = MediaProxy()

        # History + bookmarks proxies (step 3). These interface with
        # the existing history and bookmarks plugins. If a plugin is
        # not yet enabled (possible ordering edge) the proxy degrades
        # to returning empty results.
        history_plugin = None
        try:
            history_plugin = app_controller.plugins._instances.get(
                "history")
        except Exception:
            pass
        bookmarks_plugin = None
        try:
            bookmarks_plugin = app_controller.plugins._instances.get(
                "bookmarks")
        except Exception:
            pass
        self.history_proxy = HistoryProxy(history_plugin)
        self.bookmarks_proxy = BookmarksProxy(bookmarks_plugin)
        # Step-4 outbound forwarder. Lazily binds a jeepney session-bus
        # client on first use; only reachable once daemons are present
        # (this whole activate() path is gated by _daemons_available()).
        self.forwarder = DaemonForwarder()
        # Start the worker that runs the (blocking) forwards off the GUI
        # thread, so a slow/hung daemon can't freeze the UI.
        self._forward_worker.start()

        self._handlers = BridgeAdapterHandlers(
            self.tabs_proxy, self.pages_proxy,
            self.downloads_proxy, self.media_proxy,
            history=self.history_proxy,
            bookmarks=self.bookmarks_proxy)

        # Claim a per-pid well-known name on the session bus. We do NOT
        # crash qdbrowser if the bus rejects us — the plugin degrades
        # to "inactive" the same way as the no-daemons path.
        if not self._claim_bus_name():
            log.warning("bridge_adapter could not claim a D-Bus name; "
                        "staying inactive")
            # We started the forward worker above; tear it back down so a
            # failed activation doesn't leak the thread (deactivate()
            # early-returns while inactive and would never stop it).
            self._forward_worker.stop()
            return

        # Subscribe to window-level signals so we emit outbound D-Bus
        # signals for tab adds/removes. Downloads + media signals get
        # emitted from inside those plugins via
        # ``emit_download_started`` / ``emit_media_state_changed`` —
        # they look up the adapter via the plugin manager.
        try:
            app_controller.webview_added.connect(self._on_webview_added)
            app_controller.webview_removed.connect(self._on_webview_removed)
        except Exception as exc:
            log.warning("could not connect window signals: %s", exc)

        # Start the inbound D-Bus message receive loop. Runs in a
        # dedicated daemon thread so it never blocks the Qt event loop.
        self._start_recv_loop()

        self._active = True
        log.info("bridge_adapter active — bus=%s", self._bus_name)

    def deactivate(self):
        if not self._active:
            return
        self._active = False
        self._stop.set()
        try:
            if self._window is not None:
                try:
                    self._window.webview_added.disconnect(
                        self._on_webview_added)
                except Exception:
                    pass
                try:
                    self._window.webview_removed.disconnect(
                        self._on_webview_removed)
                except Exception:
                    pass
                for wv in list(self._media_connections):
                    self._disconnect_media_signals(wv)
        finally:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
            if self._pid_conn is not None:
                try:
                    self._pid_conn.close()
                except Exception:
                    pass
                self._pid_conn = None
            # Wait for the receive thread to notice the stop event
            # and exit. The thread checks _stop every 0.5 s and the
            # socket close above unblocks any pending select().
            if self._recv_thread is not None:
                self._recv_thread.join(timeout=3.0)
                self._recv_thread = None
            # Drain + stop the outbound-forward worker.
            self._forward_worker.stop()
            self._handlers = None
            self._bus_name = None

    # -- bus-name claim ----

    def _claim_bus_name(self) -> bool:
        try:
            from jeepney import DBusAddress, new_method_call
            from jeepney.io.blocking import open_dbus_connection
        except ImportError:
            return False
        try:
            self._conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:
            log.warning("session bus unavailable: %s", exc)
            return False
        # Open a second connection dedicated to PID lookups. This
        # avoids consuming inbound method-call messages from the
        # main receive connection when send_and_get_reply blocks.
        try:
            self._pid_conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:
            log.warning("could not open PID-lookup bus connection: %s",
                        exc)
            # Non-fatal: PID resolution will fall back to denying
            # mutating calls when _pid_conn is None.
        name = f"org.qdistro.QdBrowser.pid{os.getpid()}"
        bus = DBusAddress(
            "/org/freedesktop/DBus",
            bus_name="org.freedesktop.DBus",
            interface="org.freedesktop.DBus",
        )
        try:
            reply = self._conn.send_and_get_reply(
                new_method_call(bus, "RequestName", "su", (name, 0)),
                timeout=2.0)
            # 1 == PRIMARY_OWNER, 4 == ALREADY_OWNER.
            owner_code = reply.body[0] if reply.body else 0
            if owner_code not in (1, 4):
                log.warning("RequestName(%s) returned %s", name, owner_code)
                return False
        except Exception as exc:
            log.warning("RequestName failed: %s", exc)
            return False
        self._bus_name = name
        return True

    # -- outbound signal emission ----

    def _emit_signal(self, signal: str, body: tuple, signature: str) -> None:
        if not self._active or self._conn is None:
            return
        try:
            from jeepney import DBusAddress, new_signal
        except ImportError:
            return
        try:
            emitter = DBusAddress(
                QDBROWSER_PATH,
                bus_name=self._bus_name,
                interface=QDBROWSER_IFACE,
            )
            self._conn.send(new_signal(emitter, signal, signature, body))
        except Exception as exc:
            log.warning("could not emit %s signal: %s", signal, exc)

    # -- window signal handlers ----

    def _on_webview_added(self, wv) -> None:
        # 02/S9: do not announce private (off-the-record) tabs over the bridge.
        # A TabAdded signal would leak the existence + URL of a private tab to
        # any subscribed agent/extension.
        if getattr(wv, "is_off_the_record", False):
            return
        try:
            tid = int(getattr(wv, "stable_id",
                              getattr(wv, "_stable_id", 0)))
            url = wv.url() if callable(getattr(wv, "url", None)) else ""
        except Exception:
            return
        self.emit_tab_added(tid, url or "")
        self._wire_media_signals(wv)

    def _on_webview_removed(self, wv) -> None:
        try:
            tid = int(getattr(wv, "stable_id",
                              getattr(wv, "_stable_id", 0)))
        except Exception:
            return
        # 02/S9: a private (off-the-record) tab was never announced (TabAdded is
        # suppressed) and never had media wired, so it has no bridge-visible
        # state to tear down. Do NOT emit TabRemoved or a media-stop event —
        # either would leak the existence/id/timing of a private tab. Still run
        # the local media disconnect defensively (it is a no-op for OTR).
        if getattr(wv, "is_off_the_record", False):
            self._disconnect_media_signals(wv)
            self._media_tabs.discard(tid)
            self._audible_tabs.discard(tid)
            return
        self._disconnect_media_signals(wv)
        if tid in self._media_tabs:
            self._media_tabs.discard(tid)
            self._audible_tabs.discard(tid)
            self._publish_media_for_webview(wv, "stopped")
        self.emit_tab_removed(tid)

    def _wire_media_signals(self, wv) -> None:
        if wv in self._media_connections:
            return
        conns = []
        try:
            page = wv.view.page()
        except Exception:
            page = None
        signal = getattr(page, "recentlyAudibleChanged", None)
        if signal is not None:
            try:
                conn = signal.connect(
                    lambda audible, _wv=wv:
                    self._on_recently_audible_changed(_wv, audible))
                conns.append((signal, conn))
            except Exception as exc:
                log.debug("media audible signal wiring failed: %s", exc)
        for signal_name, handler in (
                ("title_changed", self._on_media_title_changed),
                ("load_started", self._on_media_load_started)):
            signal = getattr(wv, signal_name, None)
            if signal is None:
                continue
            try:
                conn = signal.connect(
                    lambda *args, _wv=wv, _handler=handler:
                    _handler(_wv, *args))
                conns.append((signal, conn))
            except Exception as exc:
                log.debug("media %s wiring failed: %s", signal_name, exc)
        if conns:
            self._media_connections[wv] = conns

    def _disconnect_media_signals(self, wv) -> None:
        conns = self._media_connections.pop(wv, None)
        if not conns:
            return
        for signal, conn in conns:
            try:
                signal.disconnect(conn)
            except Exception:
                pass

    def _on_recently_audible_changed(self, wv, audible: bool) -> None:
        tid = self._webview_tab_id(wv)
        if tid is None:
            return
        if audible:
            self._audible_tabs.add(tid)
            self._media_tabs.add(tid)
            self._publish_media_for_webview(wv, "playing")
            return
        if tid in self._audible_tabs:
            self._audible_tabs.discard(tid)
            self._publish_media_for_webview(wv, "paused")

    def _on_media_title_changed(self, wv, *args) -> None:
        tid = self._webview_tab_id(wv)
        if tid in self._audible_tabs:
            self._publish_media_for_webview(wv, "playing")

    def _on_media_load_started(self, wv, *args) -> None:
        tid = self._webview_tab_id(wv)
        if tid in self._media_tabs:
            self._media_tabs.discard(tid)
            self._audible_tabs.discard(tid)
            self._publish_media_for_webview(wv, "stopped")

    @staticmethod
    def _webview_tab_id(wv) -> int | None:
        try:
            return int(getattr(wv, "stable_id", getattr(wv, "_stable_id", 0)))
        except Exception:
            return None

    @staticmethod
    def _webview_title(wv) -> str:
        try:
            if callable(getattr(wv, "title", None)):
                return str(wv.title() or "")
        except Exception:
            pass
        return ""

    def _publish_media_for_webview(self, wv, state: str) -> None:
        title = self._webview_title(wv)
        if self.media_proxy is not None:
            self.media_proxy.update(title=title, state=state)
        self.emit_media_state_changed(
            state, title=title, tab_id=self._webview_tab_id(wv))

    # -- inbound D-Bus receive loop ----

    def _start_recv_loop(self) -> None:
        """Spin up a daemon thread that blocks on the D-Bus connection fd
        and dispatches inbound method calls to the handlers.

        The thread uses ``select`` on the connection's socket fd with a
        short timeout so it can check ``_stop`` periodically and exit
        cleanly on deactivate.
        """
        if self._conn is None or self._handlers is None:
            return
        self._stop.clear()
        t = threading.Thread(target=self._recv_loop, daemon=True,
                             name="bridge_adapter_recv")
        self._recv_thread = t
        t.start()

    def _recv_loop(self) -> None:
        """Blocking receive loop — runs in a background thread."""
        try:
            from jeepney import (
                HeaderFields,
                MessageType,
                new_error,
                new_method_return,
            )
        except ImportError:
            log.warning("jeepney not available; receive loop not started")
            return

        conn = self._conn
        if conn is None:
            return

        while not self._stop.is_set():
            try:
                # Use select with a timeout so we can check _stop.
                # conn.sock is the underlying socket object.
                sock = getattr(conn, "sock", None)
                if sock is None:
                    break
                ready, _, _ = select.select([sock], [], [], 0.5)
                if not ready:
                    continue
                msg = conn.receive(timeout=2.0)
            except (TimeoutError, OSError):
                continue
            except Exception as exc:
                if self._stop.is_set():
                    break
                log.debug("recv_loop error: %s", exc)
                continue

            # Only handle method calls addressed to our interface.
            if msg.header.message_type != MessageType.method_call:
                continue

            fields = msg.header.fields
            iface = fields.get(HeaderFields.interface, "")
            path = fields.get(HeaderFields.path, "")
            member = fields.get(HeaderFields.member, "")

            # Handle standard D-Bus introspection.
            if (iface == "org.freedesktop.DBus.Introspectable"
                    and member == "Introspect"):
                reply = new_method_return(
                    msg, "s", (QDBROWSER_INTROSPECTION_XML,))
                try:
                    conn.send(reply)
                except Exception as exc:
                    log.debug("send introspect reply failed: %s", exc)
                continue

            # Filter to our interface + path.
            if iface != QDBROWSER_IFACE or path != QDBROWSER_PATH:
                continue

            # Resolve the caller's PID for polkit gating. The polkit
            # check (which may run an interactive ``pkcheck`` with a
            # 15 s cap) happens HERE on the receive thread — never on
            # the GUI thread — so an auth prompt can't freeze the UI and
            # can't outlive the main-thread dispatch timeout. Only after
            # authorization succeeds do we bounce the actual op (which
            # touches Qt widgets) onto the main thread. If authorization
            # fails the op never runs.
            sender = fields.get(HeaderFields.sender)
            try:
                caller_pid = self._resolve_sender_pid(sender)
                caller_start = None
                if caller_pid is not None:
                    caller_start = self._resolve_sender_start_time(
                        caller_pid)
                    # Fail CLOSED for an external caller on a
                    # pkcheck-gated action when we can't bind the check
                    # to the caller's start time: without it pkcheck
                    # matches on the bare PID, reopening the PID-reuse
                    # window the start_time guard exists to close. (Read-
                    # only _OPEN_ACTIONS skip pkcheck entirely, so they
                    # never needed a start time and are not denied here.)
                    if (caller_start is None
                            and self._handlers.method_needs_pkcheck(
                                member)):
                        raise PermissionError(
                            "could not resolve caller start time; "
                            "refusing pkcheck-gated call to avoid a "
                            "PID-reuse race")
                self._handlers.authorize(
                    member, caller_pid=caller_pid,
                    caller_start_time=caller_start)
                body, sig = self._dispatch_helper.call_on_main_thread(
                    lambda _member=member, _body=msg.body, _pid=caller_pid:
                        self._handlers.invoke(_member, _body, caller_pid=_pid))
                reply = new_method_return(msg, sig, body)
            except PermissionError as exc:
                reply = new_error(
                    msg,
                    "org.freedesktop.DBus.Error.AccessDenied",
                    "s", (str(exc),))
            except LookupError as exc:
                reply = new_error(
                    msg,
                    "org.freedesktop.DBus.Error.UnknownMethod",
                    "s", (str(exc),))
            except Exception as exc:
                log.warning("dispatch %s failed: %s", member, exc)
                reply = new_error(
                    msg,
                    "org.freedesktop.DBus.Error.Failed",
                    "s", (str(exc),))
            try:
                conn.send(reply)
            except Exception as exc:
                log.debug("send reply for %s failed: %s", member, exc)

    def _resolve_sender_pid(self, sender: str | None
                            ) -> int | None:
        """Ask the bus daemon for the Unix PID of ``sender``.

        Returns the PID as an int, or raises ``PermissionError`` if
        the PID cannot be resolved for an external caller. This
        prevents an authorization bypass where a failed
        ``GetConnectionUnixProcessID`` call would previously return
        ``None`` (treated as 'internal/trusted' by ``polkit_check``).

        Uses ``_pid_conn`` (a dedicated D-Bus connection) so the
        blocking ``send_and_get_reply`` does not consume inbound
        method-call messages from the main receive connection.
        """
        if sender is None:
            # No sender header — treat as internal (e.g. tests).
            return None
        pid_conn = self._pid_conn
        if pid_conn is None:
            raise PermissionError(
                "no PID-lookup bus connection; cannot authorize caller")
        try:
            from jeepney import DBusAddress, new_method_call
            bus = DBusAddress(
                "/org/freedesktop/DBus",
                bus_name="org.freedesktop.DBus",
                interface="org.freedesktop.DBus",
            )
            reply = pid_conn.send_and_get_reply(
                new_method_call(
                    bus, "GetConnectionUnixProcessID",
                    "s", (sender,)),
                timeout=2.0)
            if reply.body:
                return int(reply.body[0])
        except Exception as exc:
            log.debug("could not resolve PID for %s: %s", sender, exc)
        raise PermissionError(
            f"could not resolve PID for D-Bus sender {sender!r}")

    @staticmethod
    def _resolve_sender_start_time(caller_pid: int | None
                                   ) -> int | None:
        """Read the caller PID's kernel start-time (clock ticks since
        boot) from ``/proc/<pid>/stat`` field 22.

        Passed to polkit as the second component of ``pid,start_time``
        so polkit refuses the check if the PID was recycled between the
        bus daemon resolving it and the authorization check (defeating a
        PID-reuse race). Best-effort: any failure returns ``None`` and
        authorization proceeds with the PID alone (no regression vs. the
        prior behaviour, which never passed a start time at all)."""
        if caller_pid is None:
            return None
        try:
            with open(f"/proc/{int(caller_pid)}/stat", "rb") as fh:
                data = fh.read()
            # comm (field 2) is parenthesised and may contain spaces or
            # ')'; split on the LAST ')' so the remaining fields align.
            rparen = data.rfind(b")")
            if rparen < 0:
                return None
            rest = data[rparen + 2:].split()
            # After comm, field 3 (state) is rest[0]; starttime is
            # field 22, i.e. rest[22 - 3] == rest[19].
            if len(rest) <= 19:
                return None
            return int(rest[19])
        except (OSError, ValueError, IndexError) as exc:
            log.debug("could not read start time for pid=%s: %s",
                      caller_pid, exc)
            return None
