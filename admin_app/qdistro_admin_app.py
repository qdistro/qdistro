#!/usr/bin/env python3
"""qdistro admin approval app — Phase 2.

PyQt6 master/detail UI. Subscribes to broker's RequestPending signal;
shows queue on the left, detail+Approve/Deny on the right. Tabs:
Pending, Cache, Rules, History, Silos (optional). System tray icon
with pending-count overlay + pulse animation. Keyboard shortcuts per
doc/admin-approval.md: ↑/↓ navigate; Ctrl+Y/Alt+A approve current;
Ctrl+N/Alt+D deny current; Ctrl+Shift+A approve-all-with-confirm;
Ctrl+Shift+D deny-all-with-confirm; Ctrl+R rule-from-this;
Ctrl+Shift+1..8 scope picker.
"""
from __future__ import annotations

import logging
import os
import shlex
import sys
import time as _time
from datetime import datetime

import dbus
import dbus.mainloop.glib
import dbus.service
from PyQt6.QtCore import QEvent, Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import (
    QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut,
    QStandardItem, QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView,
    QMainWindow, QMessageBox, QProgressDialog, QPushButton, QRadioButton,
    QSpinBox, QSplitter, QStackedWidget, QStyle, QTableView, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget,
)
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu

BUS_NAME = "org.qdistro.AdminBroker1"
OBJ_PATH = "/org/qdistro/AdminBroker1"

SESSION_MANAGER_BUS_NAME = "org.qdistro.SessionManager1"
SESSION_MANAGER_OBJ_PATH = "/org/qdistro/SessionManager1"

# NOTE: HISTORY_FILE is an aspirational path documented in the task spec.
# Current history is served from the broker's sqlite audit DB via
# ListHistory(); this constant exists as a stable reference for future
# work that may mirror history rows to JSONL. Not read or written today.
HISTORY_FILE = "/var/lib/qdistro/admin-history.jsonl"
MAX_HISTORY_ENTRIES = 100

# Rules.d directory the admin app may delete YAML files from. Anchored
# here so the delete path can validate `source_path` stays inside it
# (no symlinks, no ../, no /etc/passwd surprises). Matches the broker's
# default `RulesEngine` directory.
RULES_DIR = "/etc/qdistro/rules.d"
FIREFOX_CONTAINERS_ACTION = "qdistro.browser.containers.cross_uid:containers.*"
FIREFOX_CONTAINERS_RULE_PREFIX = "firefox-containers-cross-user-uid"


_log = logging.getLogger("qdistro.admin_app")


def _firefox_containers_rule_filename(uid: int) -> str:
    return f"{FIREFOX_CONTAINERS_RULE_PREFIX}{int(uid)}.yaml"


def _firefox_containers_rule_yaml(uid: int) -> str:
    uid_i = int(uid)
    return (
        f"- name: {FIREFOX_CONTAINERS_RULE_PREFIX}{uid_i}\n"
        "  decision: allow\n"
        "  match:\n"
        f"    uid: {uid_i}\n"
        f"    action: {FIREFOX_CONTAINERS_ACTION}\n"
        "  rationale: Enable admin cross-user Firefox container relay\n"
    )


def _is_firefox_containers_rule(rule: dict, uid: int) -> bool:
    return (
        str(rule.get("decision", "")) == "allow"
        and int(rule.get("uid", -1)) == int(uid)
        and str(rule.get("action", "")) == FIREFOX_CONTAINERS_ACTION
    )


# ---------------------------------------------------------------------------
# Bounded broker-error labels
# ---------------------------------------------------------------------------
#
# Raw D-Bus exception strings (e.g. `org.qdistro.AdminBroker1.RulesEngineRefused:
# rule 'foo.yaml' at /etc/qdistro/rules.d/foo.yaml: scope 'forever' ...`) can
# carry file paths, internal identifiers, and stack-y detail that should not be
# rendered in a shoulder-visible modal. They belong in logs. The admin sees a
# short, bounded, human-readable label for the *known* typed broker errors; the
# full raw exception is always logged (and, where a widget supports it, kept
# behind an explicit "Details" affordance).
#
# Keys are the D-Bus error names the broker actually raises (enumerated from
# broker/qdistro_admin_broker.py and SessionManager). Anything not in this map
# falls back to a generic "Broker error" label — never the raw string.
_BUS_NAME = "org.qdistro.AdminBroker1"
_SM_BUS_NAME = "org.qdistro.SessionManager1"

# (title, label) for each known typed broker error name.
_FRIENDLY_BROKER_ERRORS: dict[str, tuple[str, str]] = {
    f"{_BUS_NAME}.ScopeNotPermitted": (
        "Scope not permitted",
        "The broker rejected the chosen approval scope for this request. "
        "A narrower scope (for example “Just this once”) may be "
        "accepted. The request is still pending — you can retry.",
    ),
    f"{_BUS_NAME}.RulesEngineRefused": (
        "Rules engine refused",
        "The broker’s rules engine refused this operation. The rule or "
        "decision did not pass validation.",
    ),
    f"{_BUS_NAME}.RateLimited": (
        "Rate limited",
        "Too many requests in a short window. Wait a moment and try again.",
    ),
    f"{_BUS_NAME}.InvalidArgument": (
        "Invalid argument",
        "The broker rejected one of the supplied values as invalid.",
    ),
    f"{_BUS_NAME}.BadArgument": (
        "Invalid argument",
        "The broker rejected one of the supplied values as invalid.",
    ),
    f"{_BUS_NAME}.AccessDenied": (
        "Access denied",
        "The broker denied this operation for the current admin identity.",
    ),
    f"{_BUS_NAME}.Denied": (
        "Denied",
        "The broker denied this operation.",
    ),
    f"{_BUS_NAME}.AuditUnavailable": (
        "Audit unavailable",
        "The broker could not write its audit log, so it failed the "
        "operation closed as a safety measure.",
    ),
    f"{_BUS_NAME}.CallerGone": (
        "Caller gone",
        "The requesting process exited before the decision was recorded.",
    ),
    f"{_BUS_NAME}.CallerIdentityMismatch": (
        "Caller identity mismatch",
        "The requesting process identity changed; the broker refused to act "
        "on a stale request.",
    ),
    f"{_BUS_NAME}.RelayFailed": (
        "Relay failed",
        "The broker could not relay the decision to the user session.",
    ),
    f"{_BUS_NAME}.CacheGcFailed": (
        "Cache cleanup failed",
        "The broker could not garbage-collect its approval cache.",
    ),
    f"{_BUS_NAME}.SnapshotFailed": (
        "Snapshot failed",
        "The broker could not take the requested snapshot.",
    ),
    f"{_BUS_NAME}.SnapperUnavailable": (
        "Snapshots unavailable",
        "The snapshot subsystem (snapper) is not available on this host.",
    ),
    f"{_BUS_NAME}.SiloManagerUnreachable": (
        "Silo manager unreachable",
        "The broker could not reach the session/silo manager.",
    ),
    f"{_BUS_NAME}.SiloNotActive": (
        "Silo not active",
        "The target silo is not currently active.",
    ),
    f"{_BUS_NAME}.TargetNotReady": (
        "Target not ready",
        "The target service is not ready to handle this request yet.",
    ),
    f"{_BUS_NAME}.PrintAuditError": (
        "Audit read failed",
        "The broker could not read the audit log for this request.",
    ),
    f"{_BUS_NAME}.Internal": (
        "Broker error",
        "The broker hit an internal error and could not complete the "
        "operation.",
    ),
    # Bus-level failures (broker not running / restarting).
    "org.freedesktop.DBus.Error.ServiceUnknown": (
        "Broker unavailable",
        "The admin broker is not running. Start "
        "qdistro-admin-broker.service and try again.",
    ),
    "org.freedesktop.DBus.Error.NameHasNoOwner": (
        "Broker unavailable",
        "The admin broker is not running. Start "
        "qdistro-admin-broker.service and try again.",
    ),
    "org.freedesktop.DBus.Error.NoReply": (
        "Broker not responding",
        "The admin broker did not reply in time. It may be restarting; "
        "try again in a moment.",
    ),
    "org.freedesktop.DBus.Error.UnknownMethod": (
        "Unsupported by broker",
        "The running broker does not support this operation. It may be an "
        "older version.",
    ),
    # Session manager raises its own typed errors (see
    # session_manager/qdistro_session_manager.py SessionError subclasses).
    f"{_SM_BUS_NAME}.UnknownSilo": (
        "Unknown silo",
        "The named silo does not exist.",
    ),
    f"{_SM_BUS_NAME}.SiloExists": (
        "Silo already exists",
        "A silo with that name already exists.",
    ),
    f"{_SM_BUS_NAME}.SiloBusy": (
        "Silo busy",
        "The silo is busy with another operation; try again shortly.",
    ),
    f"{_SM_BUS_NAME}.BadState": (
        "Silo in wrong state",
        "The silo is not in a state that allows this operation.",
    ),
    f"{_SM_BUS_NAME}.BadArgument": (
        "Invalid argument",
        "The session manager rejected one of the supplied values.",
    ),
    f"{_SM_BUS_NAME}.NotAuthorized": (
        "Not authorized",
        "The session manager denied this operation for the current "
        "admin identity.",
    ),
    f"{_SM_BUS_NAME}.Generic": (
        "Session manager error",
        "The session manager could not complete the operation.",
    ),
}

_GENERIC_BROKER_ERROR = (
    "Broker error",
    "The broker rejected this operation. See the application log "
    "(stderr) for the full diagnostic detail.",
)


def _friendly_broker_error(exc: Exception) -> tuple[str, str]:
    """Map a broker D-Bus exception to a short, bounded (title, label).

    Raw D-Bus strings can leak file paths and internal identifiers into a
    shoulder-visible dialog, so callers should render only the returned
    (title, label) to the user. The full raw exception is logged here at
    WARNING so nothing is lost for operators reading stderr/journald.

    Unknown error names fall back to a generic label — never the raw string.
    """
    name = ""
    try:
        getter = getattr(exc, "get_dbus_name", None)
        if callable(getter):
            name = getter() or ""
    except Exception:  # noqa: BLE001 - defensive; never let logging break UX
        name = ""
    # Log the full raw exception (name + message) for operators. This is the
    # only place the raw string is surfaced; the UI text never includes it.
    _log.warning("broker error %s: %s", name or "(no dbus name)", exc)
    return _FRIENDLY_BROKER_ERRORS.get(name, _GENERIC_BROKER_ERROR)


# ---------------------------------------------------------------------------
# StatusNotifierItem D-Bus interface
# ---------------------------------------------------------------------------
#
# On Wayland compositors, QSystemTrayIcon.show() is often a no-op because
# the XEmbed protocol is not available.  The org.kde.StatusNotifierItem
# specification is the Wayland-era replacement that qdshell, KDE Plasma,
# and GNOME (via extensions) all support.  We implement the interface
# directly with dbus-python's service helpers so the tray icon works on
# Wayland without relying on Qt's platform integration (which requires
# the xdg-desktop-portal SNI provider to be installed).
#
# Fallback: if org.kde.StatusNotifierWatcher is not running we fall back
# to the QSystemTrayIcon code path (X11, or hosts that use the old XEmbed
# protocol).

SNI_IFACE = "org.kde.StatusNotifierItem"
DBUSMENU_IFACE = "com.canonical.dbusmenu"
SNI_WATCHER_BUS = "org.kde.StatusNotifierWatcher"
SNI_WATCHER_PATH = "/StatusNotifierWatcher"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# Menu item IDs for the DBusMenu protocol.
_MENU_ROOT_ID = 0
_MENU_SHOW_ID = 1
_MENU_MUTE_ID = 2
_MENU_QUIT_ID = 3


class _DBusMenuService(dbus.service.Object):
    """Minimal com.canonical.dbusmenu service exposing Show / Mute / Quit.

    The DBusMenu protocol is used by StatusNotifier hosts (panels) to
    render right-click context menus without requiring client-side window
    surfaces.
    """

    def __init__(self, bus, object_path: str, callbacks: dict):
        super().__init__(bus, object_path)
        self._callbacks = callbacks  # {item_id: callable}
        self._revision = 1
        self._mute_label = "Mute 30m"

    # -- properties (via org.freedesktop.DBus.Properties) ---------------------

    @dbus.service.method(dbus_interface=PROPERTIES_IFACE,
                         in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        if interface_name != DBUSMENU_IFACE:
            raise dbus.DBusException(
                f"No such interface: {interface_name}",
                name="org.freedesktop.DBus.Error.UnknownInterface")
        return self._get_prop(property_name)

    @dbus.service.method(dbus_interface=PROPERTIES_IFACE,
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        if interface_name != DBUSMENU_IFACE:
            return dbus.Dictionary({}, signature="sv")
        props = {}
        for name in ("Version", "TextDirection", "Status", "IconThemePath"):
            try:
                props[name] = self._get_prop(name)
            except dbus.DBusException:
                pass
        return dbus.Dictionary(props, signature="sv")

    def _get_prop(self, name: str):
        if name == "Version":
            return dbus.UInt32(3, variant_level=1)
        if name == "TextDirection":
            return dbus.String("ltr", variant_level=1)
        if name == "Status":
            return dbus.String("normal", variant_level=1)
        if name == "IconThemePath":
            return dbus.Array([], signature="s", variant_level=1)
        raise dbus.DBusException(
            f"No such property: {name}",
            name="org.freedesktop.DBus.Error.UnknownProperty")

    # -- methods --------------------------------------------------------------

    @dbus.service.method(dbus_interface=DBUSMENU_IFACE,
                         in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):
        """Return the menu tree starting from *parent_id*."""
        children = []
        if parent_id == _MENU_ROOT_ID:
            children = [
                (_MENU_SHOW_ID, {"label": "Show", "enabled": True}, []),
                (_MENU_MUTE_ID, {"label": self._mute_label,
                                 "enabled": True}, []),
                (_MENU_QUIT_ID, {"label": "Quit", "enabled": True}, []),
            ]
        # Pack into the required (id, {props}, [children]) structure.
        packed_children = dbus.Array([], signature="v")
        for cid, props, _ in children:
            props_dbus = dbus.Dictionary(
                {k: _variant(v) for k, v in props.items()},
                signature="sv")
            entry = dbus.Struct(
                (dbus.Int32(cid), props_dbus,
                 dbus.Array([], signature="v")),
                signature=None)
            packed_children.append(entry)
        root_props = dbus.Dictionary({}, signature="sv")
        root = dbus.Struct(
            (dbus.Int32(parent_id), root_props, packed_children),
            signature=None)
        return dbus.UInt32(self._revision), root

    @dbus.service.method(dbus_interface=DBUSMENU_IFACE,
                         in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, property_names):
        return dbus.Array([], signature="(ia{sv})")

    @dbus.service.method(dbus_interface=DBUSMENU_IFACE,
                         in_signature="isvu", out_signature="")
    def Event(self, item_id, event_type, data, timestamp):
        """Called when the user clicks a menu item."""
        if event_type == "clicked":
            cb = self._callbacks.get(int(item_id))
            if cb is not None:
                cb()

    @dbus.service.method(dbus_interface=DBUSMENU_IFACE,
                         in_signature="ai", out_signature="")
    def EventGroup(self, events):
        pass

    @dbus.service.method(dbus_interface=DBUSMENU_IFACE,
                         in_signature="i", out_signature="b")
    def AboutToShow(self, item_id):
        return False

    @dbus.service.method(dbus_interface=DBUSMENU_IFACE,
                         in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        return dbus.Array([], signature="i"), dbus.Array([], signature="i")

    # -- signals --------------------------------------------------------------

    @dbus.service.signal(dbus_interface=DBUSMENU_IFACE,
                         signature="ui")
    def LayoutUpdated(self, revision, parent):
        pass

    @dbus.service.signal(dbus_interface=DBUSMENU_IFACE,
                         signature="a(ia{sv})a(ias)")
    def ItemsPropertiesUpdated(self, updated_props, removed_props):
        pass

    # -- helpers --------------------------------------------------------------

    def set_mute_label(self, label: str) -> None:
        """Update the mute menu label and bump the layout revision."""
        self._mute_label = label
        self._revision += 1
        self.LayoutUpdated(dbus.UInt32(self._revision),
                           dbus.Int32(_MENU_ROOT_ID))


def _variant(value):
    """Wrap a Python value in a D-Bus Variant."""
    if isinstance(value, bool):
        return dbus.Boolean(value, variant_level=1)
    if isinstance(value, int):
        return dbus.Int32(value, variant_level=1)
    if isinstance(value, str):
        return dbus.String(value, variant_level=1)
    return dbus.String(str(value), variant_level=1)


class StatusNotifierItemService(dbus.service.Object):
    """org.kde.StatusNotifierItem D-Bus service.

    Exposes the properties and signals required by StatusNotifier hosts
    (KDE Plasma, qdshell, GNOME+AppIndicator extensions) to render a
    tray icon, tooltip, and context menu for the admin app.

    Lifecycle
    ---------
    Constructed by `MainWindow.__init__` via `_try_status_notifier()`.
    The caller passes a *session_bus* (``dbus.SessionBus()``) and a
    *callbacks* dict mapping activation actions to callables.  On
    success `register()` claims the well-known bus name and pokes
    `StatusNotifierWatcher.RegisterStatusNotifierItem`.

    The instance keeps a mutable ``_status`` / ``_tooltip_text`` /
    ``_pending_count`` that `MainWindow` updates; each setter emits the
    matching ``New*`` signal so the host repaints.
    """

    def __init__(self, session_bus: dbus.Bus, object_path: str,
                 callbacks: dict):
        self._bus = session_bus
        self._object_path = object_path
        self._callbacks = callbacks  # {"activate": callable, ...}
        self._bus_name_str = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._well_known = dbus.service.BusName(
            self._bus_name_str, self._bus, do_not_queue=True)
        super().__init__(self._bus, self._object_path)

        # Mutable state — setters emit signals.
        self._status = "Active"   # Active | NeedsAttention | Passive
        self._tooltip_text = "qdistro admin approvals"
        self._pending_count = 0
        self._icon_name = "computer"
        self._attention_icon_name = "dialog-warning"
        # Mutable icon pixmap data (SNI IconPixmap property).
        # Updated by set_icon_pixmap() when the badge overlay changes.
        # Format: list of (width, height, ARGB_bytes) tuples.
        self._icon_pixmap_data: list[tuple[int, int, bytes]] = []

        # DBusMenu service for context menu
        self._menu_path = f"{self._object_path}/menu"
        self._menu: _DBusMenuService | None = None

    @property
    def bus_name_str(self) -> str:
        return self._bus_name_str

    # ---- registration -------------------------------------------------------

    def register(self) -> bool:
        """Register with the StatusNotifierWatcher.  Returns True on success."""
        try:
            watcher = self._bus.get_object(SNI_WATCHER_BUS, SNI_WATCHER_PATH)
            watcher.RegisterStatusNotifierItem(
                self._bus_name_str,
                dbus_interface=SNI_WATCHER_BUS)
            _log.info("Registered SNI %s with StatusNotifierWatcher",
                      self._bus_name_str)
            return True
        except dbus.DBusException as exc:
            _log.warning("StatusNotifierWatcher registration failed: %s", exc)
            return False

    def setup_menu(self, callbacks: dict) -> None:
        """Create the DBusMenu service on our menu object path."""
        self._menu = _DBusMenuService(self._bus, self._menu_path, callbacks)

    # ---- D-Bus properties (org.kde.StatusNotifierItem) ----------------------

    @dbus.service.method(dbus_interface=PROPERTIES_IFACE,
                         in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        if interface_name != SNI_IFACE:
            raise dbus.DBusException(
                f"No such interface: {interface_name}",
                name="org.freedesktop.DBus.Error.UnknownInterface")
        return self._get_property(property_name)

    @dbus.service.method(dbus_interface=PROPERTIES_IFACE,
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        if interface_name != SNI_IFACE:
            return dbus.Dictionary({}, signature="sv")
        props = {}
        for name in ("Category", "Id", "Title", "Status", "IconName",
                     "IconPixmap", "AttentionIconName", "ToolTip",
                     "Menu", "ItemIsMenu", "IconThemePath"):
            try:
                props[name] = self._get_property(name)
            except dbus.DBusException:
                pass
        return dbus.Dictionary(props, signature="sv")

    def _get_property(self, name: str):
        if name == "Category":
            return dbus.String("SystemServices", variant_level=1)
        if name == "Id":
            return dbus.String("qdistro-admin", variant_level=1)
        if name == "Title":
            return dbus.String("qdistro admin approvals", variant_level=1)
        if name == "Status":
            return dbus.String(self._status, variant_level=1)
        if name == "IconName":
            return dbus.String(self._icon_name, variant_level=1)
        if name == "IconPixmap":
            pixmaps = dbus.Array([], signature="(iiay)")
            for w, h, data in self._icon_pixmap_data:
                pixmaps.append(dbus.Struct(
                    (dbus.Int32(w), dbus.Int32(h),
                     dbus.ByteArray(data)),
                    signature=None))
            return dbus.Array(pixmaps, signature="(iiay)",
                              variant_level=1)
        if name == "AttentionIconName":
            return dbus.String(self._attention_icon_name, variant_level=1)
        if name == "ToolTip":
            # (icon-name, icon-pixmap[], title, body)
            return dbus.Struct(
                (dbus.String(self._icon_name),
                 dbus.Array([], signature="(iiay)"),
                 dbus.String("qdistro admin"),
                 dbus.String(self._tooltip_text)),
                signature=None, variant_level=1)
        if name == "Menu":
            return dbus.ObjectPath(self._menu_path, variant_level=1)
        if name == "ItemIsMenu":
            return dbus.Boolean(False, variant_level=1)
        if name == "IconThemePath":
            return dbus.Array([], signature="s", variant_level=1)
        raise dbus.DBusException(
            f"No such property: {name}",
            name="org.freedesktop.DBus.Error.UnknownProperty")

    # ---- D-Bus methods (org.kde.StatusNotifierItem) -------------------------

    @dbus.service.method(dbus_interface=SNI_IFACE,
                         in_signature="ii", out_signature="")
    def Activate(self, x, y):
        """Primary click — bring the admin window to front."""
        cb = self._callbacks.get("activate")
        if cb is not None:
            cb()

    @dbus.service.method(dbus_interface=SNI_IFACE,
                         in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        pass

    @dbus.service.method(dbus_interface=SNI_IFACE,
                         in_signature="is", out_signature="")
    def Scroll(self, delta, orientation):
        pass

    @dbus.service.method(dbus_interface=SNI_IFACE,
                         in_signature="ii", out_signature="")
    def ContextMenu(self, x, y):
        pass

    # ---- D-Bus signals (org.kde.StatusNotifierItem) -------------------------

    @dbus.service.signal(dbus_interface=SNI_IFACE, signature="s")
    def NewStatus(self, status):
        pass

    @dbus.service.signal(dbus_interface=SNI_IFACE, signature="")
    def NewIcon(self):
        pass

    @dbus.service.signal(dbus_interface=SNI_IFACE, signature="")
    def NewToolTip(self):
        pass

    @dbus.service.signal(dbus_interface=SNI_IFACE, signature="")
    def NewAttentionIcon(self):
        pass

    @dbus.service.signal(dbus_interface=SNI_IFACE, signature="")
    def NewTitle(self):
        pass

    # ---- public setters — update state + emit signals -----------------------

    def set_status(self, status: str) -> None:
        """Update the Status property and emit NewStatus."""
        if status == self._status:
            return
        self._status = status
        self.NewStatus(self._status)

    def set_tooltip(self, text: str) -> None:
        """Update the tooltip body text and emit NewToolTip."""
        if text == self._tooltip_text:
            return
        self._tooltip_text = text
        self.NewToolTip()

    def set_pending_count(self, count: int) -> None:
        """Update the pending count, adjusting status + icon signals."""
        if count == self._pending_count:
            return
        self._pending_count = count
        if count > 0:
            self.set_status("NeedsAttention")
        else:
            self.set_status("Active")
        # The host should re-read the icon (badge may have changed).
        self.NewIcon()

    def set_icon_pixmap(self, pixmap) -> None:
        """Update IconPixmap from a QPixmap with the badge overlay.

        Converts the QPixmap to ARGB32 format and stores it for the
        host to retrieve via the IconPixmap D-Bus property.
        """
        try:
            from PyQt6.QtGui import QImage
            img = pixmap.toImage().convertToFormat(
                QImage.Format.Format_ARGB32)
            w, h = img.width(), img.height()
            # SNI expects network byte order (big-endian) ARGB per pixel.
            # QImage ARGB32 is already 0xAARRGGBB per 32-bit word in
            # native byte order; on little-endian hosts we need to
            # byte-swap each pixel.
            import struct
            raw = img.bits()
            if raw is None:
                self._icon_pixmap_data = []
                return
            raw.setsize(w * h * 4)
            pixels = bytes(raw)
            if sys.byteorder == "little":
                # Byte-swap each 4-byte ARGB word to network order.
                words = struct.unpack(f"<{w * h}I", pixels)
                pixels = struct.pack(f">{w * h}I", *words)
            self._icon_pixmap_data = [(w, h, pixels)]
        except Exception:  # noqa: BLE001
            # If conversion fails, keep the old data.
            pass

    def set_mute_label(self, label: str) -> None:
        """Forward mute label change to the DBusMenu service."""
        if self._menu is not None:
            self._menu.set_mute_label(label)


def _try_status_notifier(callbacks: dict) -> StatusNotifierItemService | None:
    """Attempt to create and register a StatusNotifierItem on the session bus.

    Returns the service instance on success, or None if the watcher is
    unavailable (falls back to QSystemTrayIcon).
    """
    try:
        session_bus = dbus.SessionBus()
        # Probe whether the watcher exists before committing.
        watcher_obj = session_bus.get_object(SNI_WATCHER_BUS,
                                             SNI_WATCHER_PATH)
        # The SNI spec says clients should also check whether a host
        # (panel / tray) has registered with the watcher. If
        # IsStatusNotifierHostRegistered is false the watcher is running
        # but no panel is listening — the icon would be invisible.
        try:
            props = dbus.Interface(watcher_obj, PROPERTIES_IFACE)
            host_registered = bool(
                props.Get(SNI_WATCHER_BUS, "IsStatusNotifierHostRegistered"))
            if not host_registered:
                _log.info("StatusNotifierWatcher found but no host "
                          "registered; falling back to QSystemTrayIcon")
                return None
        except dbus.DBusException:
            # Watcher exists but doesn't support the property query —
            # proceed optimistically (some older watchers omit the
            # property interface and always act as a host).
            pass
    except dbus.DBusException:
        _log.info("StatusNotifierWatcher not found on session bus; "
                  "falling back to QSystemTrayIcon")
        return None
    except Exception:  # noqa: BLE001
        _log.info("Failed to connect to session bus; "
                  "falling back to QSystemTrayIcon")
        return None

    sni_path = "/StatusNotifierItem"
    try:
        sni = StatusNotifierItemService(session_bus, sni_path, callbacks)
        sni.setup_menu({
            _MENU_SHOW_ID: callbacks.get("activate", lambda: None),
            _MENU_MUTE_ID: callbacks.get("toggle_mute", lambda: None),
            _MENU_QUIT_ID: callbacks.get("quit", lambda: None),
        })
        if sni.register():
            _log.info("Using StatusNotifierItem D-Bus tray integration")
            return sni
        _log.info("StatusNotifierWatcher registration failed; "
                  "falling back to QSystemTrayIcon")
        return None
    except dbus.DBusException as exc:
        _log.warning("StatusNotifierItem setup failed (%s); "
                     "falling back to QSystemTrayIcon", exc)
        return None
    except Exception:  # noqa: BLE001
        _log.info("StatusNotifierItem setup failed; "
                  "falling back to QSystemTrayIcon")
        return None


class BrokerBridge(QObject):
    requestPending = pyqtSignal(int)
    requestDecided = pyqtSignal(int, str)
    approvalRevoked = pyqtSignal(int, str, str)
    rulesReloaded = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self._proxy = self.bus.get_object(BUS_NAME, OBJ_PATH)
        # No bus_name= filter: dbus-python resolves a well-known name
        # to a unique sender name once and silently keeps that filter
        # even after the broker restarts (new unique name). Dropping
        # the filter lets signals from the fresh connection reach us.
        # This is safe because the system-bus policy file already
        # restricts who can own the well-known name and emit these
        # signals to root (the broker service).
        self.bus.add_signal_receiver(
            self._on_pending, signal_name="RequestPending",
            dbus_interface=BUS_NAME,
        )
        self.bus.add_signal_receiver(
            self._on_decided, signal_name="RequestDecided",
            dbus_interface=BUS_NAME,
        )
        self.bus.add_signal_receiver(
            self._on_revoked, signal_name="ApprovalRevoked",
            dbus_interface=BUS_NAME,
        )
        self.bus.add_signal_receiver(
            self._on_rules_reloaded, signal_name="RulesReloaded",
            dbus_interface=BUS_NAME,
        )

    def _on_pending(self, rid):
        self.requestPending.emit(int(rid))

    def _on_decided(self, rid, decision):
        self.requestDecided.emit(int(rid), str(decision))

    def _on_revoked(self, uid, action, exe):
        self.approvalRevoked.emit(int(uid), str(action), str(exe))

    def _on_rules_reloaded(self, rule_count):
        self.rulesReloaded.emit(int(rule_count))

    def _reconnect(self) -> None:
        """Rebuild the proxy after the broker restarted.

        dbus-python's ProxyObject holds state the bus daemon no longer
        honors once the old owner of BUS_NAME is gone; a fresh
        `bus.get_object(...)` picks up the new owner. Called from the
        method wrappers on DBusException so one retry recovers.
        """
        self._proxy = self.bus.get_object(BUS_NAME, OBJ_PATH)

    # D-Bus error names that indicate the broker/service restarted or the
    # bus lost the name owner — retrying with a fresh proxy is appropriate.
    # Application-level errors (the service raised a typed DBusException)
    # must NOT be retried: they reflect real business logic rejections.
    _RETRIABLE_DBUS_ERRORS = frozenset((
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.NoReply",
    ))

    def _call(self, name, *args):
        method = getattr(self._proxy, name)
        try:
            return method(*args, dbus_interface=BUS_NAME)
        except dbus.DBusException as exc:
            # Only retry on bus-level / owner-loss errors that signal a
            # broker restart. Application errors propagate immediately.
            if exc.get_dbus_name() not in self._RETRIABLE_DBUS_ERRORS:
                raise
            self._reconnect()
            method = getattr(self._proxy, name)
            return method(*args, dbus_interface=BUS_NAME)

    def get_pending(self) -> list[dict]:
        raw = self._call("GetPending")
        out = []
        for r in raw:
            out.append({k: (int(v) if k in ("id", "uid", "pid") else dict(v) if k == "details" else str(v))
                        for k, v in r.items()})
        return out

    def decide(self, rid: int, decision: str, scope: str):
        self._call("DecideRequest", int(rid), str(decision), str(scope))

    def list_cache(self) -> list[dict]:
        raw = self._call("ListCache")
        out = []
        for r in raw:
            out.append({
                "id":           int(r["id"]),
                "caller_uid":   int(r["caller_uid"]),
                "action":       str(r["action"]),
                "match_kind":   str(r["match_kind"]),
                "match_value":  str(r["match_value"]),
                "decision":     bool(r["decision"]),
                "scope":        str(r["scope"]),
                "approver_uid": int(r["approver_uid"]),
                "expires_at":   int(r["expires_at"]),
                "created_at":   int(r["created_at"]),
            })
        return out

    def revoke_approval(self, approval_id: int) -> bool:
        return bool(self._call("RevokeApproval", int(approval_id)))

    def list_rules(self) -> list[dict]:
        raw = self._call("ListRules")
        out = []
        for r in raw:
            out.append({
                "name":         str(r["name"]),
                "decision":     str(r["decision"]),
                "source_path":  str(r["source_path"]),
                "uid":          int(r["uid"]) if r["uid"] != -1 else -1,
                "action":       str(r["action"]),
                "exe":          str(r["exe"]),
                "app_id":       str(r.get("app_id", "")),
                "sandbox_engine": str(r.get("sandbox_engine", "")),
                "mime_type":    str(r.get("mime_type", "")),
                "argv_exact":   [str(x) for x in r.get("argv_exact", [])],
                "argv_basename": str(r.get("argv_basename", "")),
                "argv_prefix":  [str(x) for x in r.get("argv_prefix", [])],
                "scope":        str(r["scope"]),
                "rationale":    str(r["rationale"]),
            })
        return out

    def save_rule(self, filename: str, yaml_body: str) -> str:
        return str(self._call("SaveRule", str(filename), str(yaml_body)))

    def get_rule_source(self, source_path: str) -> str:
        """Fetch the raw on-disk YAML text of a rule file for editing.

        The admin app cannot read /etc/qdistro/rules.d directly
        (root-owned), so the broker exposes a read-only GetRuleSource
        RPC that re-applies all path safety fail-closed and returns the
        verbatim file text. Editing an existing rule loads THIS (not the
        projected ListRules fields) so comments and any future/unknown
        keys are preserved on round-trip.
        """
        return str(self._call("GetRuleSource", str(source_path)))

    def reload_rules(self) -> None:
        self._call("ReloadRules")

    def delete_rule(self, source_path: str, name: str) -> tuple[bool, int, list[str]]:
        """Ask the broker to delete a rule file (broker-side audit + reload).

        Returns (deleted, reload_count, errors). The broker re-applies
        all path-safety fail-closed and writes the audit row, so the
        admin app no longer unlinks the file itself.
        """
        ret = self._call("DeleteRule", str(source_path), str(name))
        deleted, count, errors = ret
        return bool(deleted), int(count), [str(e) for e in (errors or [])]

    def list_workflows(self) -> list[dict]:
        raw = self._call("ListWorkflows")
        out = []
        for r in raw:
            out.append({
                "name":         str(r["name"]),
                "trigger_type": str(r["trigger_type"]),
                "description":  str(r["description"]),
                "needs":        [str(x) for x in r.get("needs", [])],
                "step_count":   int(r["step_count"]),
                "source_path":  str(r["source_path"]),
            })
        return out

    def list_workflow_runs(self, limit: int) -> list[dict]:
        raw = self._call("ListWorkflowRuns", int(limit))
        out = []
        for r in raw:
            out.append({
                "run_id":        str(r["run_id"]),
                "workflow_name": str(r["workflow_name"]),
                "state":         str(r["state"]),
                "started_at":    float(r["started_at"]),
                "completed_at":  float(r["completed_at"]),
                "error":         str(r["error"]),
            })
        return out

    def approve_workflow_run(self, run_id: str) -> bool:
        """Approve a PENDING workflow run (admin-gated, server-side)."""
        return bool(self._call("ApproveWorkflowRun", str(run_id)))

    def list_history(self, limit: int) -> list[dict]:
        raw = self._call("ListHistory", int(limit))
        out = []
        for r in raw:
            out.append({
                "ts":              int(r["ts"]),
                "caller_uid":        int(r["caller_uid"]),
                "caller_pid":        int(r["caller_pid"]),
                "caller_exe":        str(r["caller_exe"]),
                "action":            str(r["action"]),
                "decision":          bool(r["decision"]),
                "scope":             str(r["scope"]) if r["scope"] else None,
                "source":            str(r["source"]),
                "approver_uid":      int(r["approver_uid"]) if r["approver_uid"] else None,
                "rule_path":         str(r["rule_path"]) if r["rule_path"] else None,
                "request_id":        int(r["request_id"]) if r["request_id"] else None,
                "selinux_subj_type": str(r["selinux_subj_type"]) if r["selinux_subj_type"] else None,
                "argv":              list(r["argv"]) if r["argv"] else [],
            })
        return out


class DetailPane(QWidget):
    decided = pyqtSignal(int, str, str)
    ruleFromThis = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._rid: int | None = None
        lay = QVBoxLayout(self)

        # All four labels render caller-supplied strings (uid/pid/action/
        # exe/details). Force PlainText so a malicious caller can't
        # smuggle <a href> / <b> / image tags through QLabel's HTML auto-
        # detection. Without this, action="<font color=red>OK</font>"
        # would render as red text and could be used for clickjacking.
        self.lbl_user = QLabel("(no selection)")
        self.lbl_user.setObjectName("detail_user")
        self.lbl_user.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_user.setStyleSheet("font-weight: bold; font-size: 14pt;")
        lay.addWidget(self.lbl_user)

        self.lbl_action = QLabel("")
        self.lbl_action.setObjectName("detail_action")
        self.lbl_action.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.lbl_action)

        self.lbl_exe = QLabel("")
        self.lbl_exe.setObjectName("detail_exe")
        self.lbl_exe.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_exe.setStyleSheet("color: gray; font-family: monospace;")
        lay.addWidget(self.lbl_exe)

        self.lbl_details = QLabel("")
        self.lbl_details.setObjectName("detail_details")
        self.lbl_details.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_details.setWordWrap(True)
        lay.addWidget(self.lbl_details)

        scope_box = QVBoxLayout()
        scope_box.addWidget(QLabel("Scope:"))
        self._scope_group = QButtonGroup(self)
        self._scope_buttons: list[tuple[str, QRadioButton]] = []
        # task(072): argv-aware Forever scopes. Backend (cache + broker
        # _VALID_SCOPES) supports them; admin-app exposes them too so
        # the legacy Phase-1 Qt admin app keeps parity with the qdshell
        # admin-approval-app. argv-aware scopes are always visible
        # here — the rule engine ignores the argv selector when the
        # request carries no argv.
        for key, label, default in (
            ("once",             "Just this once",                  True),
            ("1h",               "1 hour",                          False),
            ("24h",              "24 hours",                        False),
            ("forever",          "Forever, any command",            False),
            ("forever_exe",      "Forever, only this exact program", False),
            ("forever_argv",     "Forever, only this exact argv tuple", False),
            ("forever_basename", "Forever, this argv basename anywhere", False),
            ("forever_prefix",   "Forever, this argv prefix + any trailing args", False),
        ):
            rb = QRadioButton(label)
            rb.setObjectName(f"scope_{key}")
            rb.setChecked(default)
            self._scope_group.addButton(rb)
            scope_box.addWidget(rb)
            self._scope_buttons.append((key, rb))
        lay.addLayout(scope_box)

        # Add error details widget
        self.error_widget = ErrorDetailsWidget("")
        self.error_widget.hide()
        lay.addWidget(self.error_widget)

        btns = QHBoxLayout()
        self.btn_approve = QPushButton("Approve"); self.btn_approve.setObjectName("btn_approve")
        self.btn_deny = QPushButton("Deny"); self.btn_deny.setObjectName("btn_deny")
        self.btn_rule_from_this = QPushButton("Rule from this")
        self.btn_rule_from_this.setObjectName("btn_rule_from_this")
        self.btn_rule_from_this.setToolTip("Create a permanent rule from this request (Ctrl+R)")
        self.btn_approve.clicked.connect(lambda: self._emit("allow"))
        self.btn_deny.clicked.connect(lambda: self._emit("deny"))
        self.btn_rule_from_this.clicked.connect(self._emit_rule_from_this)
        btns.addWidget(self.btn_approve); btns.addWidget(self.btn_deny)
        btns.addWidget(self.btn_rule_from_this); btns.addStretch()
        lay.addLayout(btns)

        lay.addStretch()
        self._current_req: dict | None = None
        # rid -> (bounded guidance label, raw diagnostic) for broker
        # refusals on still-pending requests (e.g. ScopeNotPermitted).
        self._broker_errors: dict[int, tuple[str, str]] = {}

    def show_request(self, req: dict):
        self._rid = req["id"]
        self._current_req = req
        self.lbl_user.setText(f"uid={req['uid']}  pid={req['pid']}")
        self.lbl_action.setText(f"Action: {req['action']}")
        self.lbl_exe.setText(req["exe"])
        details = req.get("details") or {}
        self.lbl_details.setText("Details: " + (", ".join(f"{k}={v}" for k, v in details.items()) or "(none)"))

        # Hide error widget and reset its raw-detail toggle when showing a
        # (re)selected request — start from a clean, collapsed state.
        self._reset_error_widget()

        # A broker refusal recorded for THIS pending request (e.g.
        # ScopeNotPermitted) is re-surfaced inline so it persists in the
        # detail view, not only as a transient popup. The request stays in
        # the pending list and remains retryable. (item 3)
        broker_err = self._broker_errors.get(self._rid)
        if broker_err is not None:
            label, raw = broker_err
            self.error_widget.set_guidance(label)
            self.error_widget.error_text.setPlainText(raw)
            self.error_widget.show()
        # Check for error details carried on the request itself.
        elif "error" in details:
            self._show_error(details["error"])

    def _reset_error_widget(self) -> None:
        self.error_widget.set_guidance("")
        self.error_widget.toggle_button.setChecked(False)
        self.error_widget.error_text.hide()
        self.error_widget.error_text.clear()
        self.error_widget.toggle_button.setText("Show Error Details")
        self.error_widget.hide()

    def _show_error(self, error_msg: str):
        self.error_widget.error_text.setPlainText(error_msg)
        self.error_widget.show()

    def set_broker_error(self, rid: int, label: str, raw: str) -> None:
        """Record a broker refusal for a pending request so it surfaces
        inline in the detail view (not only as a transient popup) and
        survives refreshes while the request stays pending."""
        self._broker_errors[rid] = (label, raw)
        # If the failed request is the one on screen, surface it now.
        if self._rid == rid:
            self._reset_error_widget()
            self.error_widget.set_guidance(label)
            self.error_widget.error_text.setPlainText(raw)
            self.error_widget.show()

    def clear_broker_error(self, rid: int) -> None:
        self._broker_errors.pop(rid, None)

    def clear(self):
        self._rid = None
        self._current_req = None
        self.lbl_user.setText("(no selection)")
        self.lbl_action.setText(""); self.lbl_exe.setText(""); self.lbl_details.setText("")
        self._reset_error_widget()

    def reset_scope(self) -> None:
        """Reset the scope picker back to the safe default ("once").

        A non-`once` scope (forever/until/exe/...) chosen for one request
        must NOT silently carry across to the next request as the default.
        Called after each decision so every fresh request starts at the
        narrowest scope. (item 2)
        """
        for key, rb in self._scope_buttons:
            rb.setChecked(key == "once")

    def _emit(self, decision: str):
        if self._rid is None:
            return
        scope = "once"
        for key, rb in self._scope_buttons:
            if rb.isChecked():
                scope = key
                break
        self.decided.emit(self._rid, decision, scope)
        # Reset the picker to "once" immediately so a non-`once` scope
        # never silently persists as the default for the next request,
        # even if the broker refuses and the request stays pending. (item 2)
        self.reset_scope()

    def _emit_rule_from_this(self):
        if self._current_req is not None:
            self.ruleFromThis.emit(self._current_req)


class CacheTab(QWidget):
    """Read/revoke view of cached approvals. Sits alongside the Pending
    tab in MainWindow's QTabWidget.

    The table is a QStandardItemModel with one row per cached approval;
    Revoke calls the broker, which both deletes the sqlite row and
    writes an audit entry. Refresh re-queries the broker — cheap
    (ListCache is read-only and runs in-process in the broker).
    """
    COLUMNS = ("id", "uid", "action", "scope", "exe", "expires")

    def __init__(self, broker: BrokerBridge):
        super().__init__()
        self.broker = broker

        self.table = QTableView(self); self.table.setObjectName("cache_table")
        self.table.setSelectionBehavior(
            QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        # Let Tab advance OUT of the table to the row-action buttons (Revoke);
        # Qt's default tabKeyNavigation traps Tab inside the view, and mouse is
        # unavailable on the labwc/XWayland GUI test template.
        self.table.setTabKeyNavigation(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setModel(self.model)

        btns = QHBoxLayout()
        self.btn_revoke = QPushButton("Revoke"); self.btn_revoke.setObjectName("btn_revoke")
        self.btn_revoke.clicked.connect(self._on_revoke)
        self.btn_refresh = QPushButton("Refresh"); self.btn_refresh.setObjectName("btn_refresh_cache")
        self.btn_refresh.clicked.connect(self.refresh)
        btns.addWidget(self.btn_revoke); btns.addWidget(self.btn_refresh)
        btns.addStretch()

        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(btns)

    def refresh(self) -> None:
        try:
            rows = self.broker.list_cache()
        except dbus.DBusException as e:
            # Transient broker outage — don't blow up the whole app.
            title, label = _friendly_broker_error(e)
            QMessageBox.warning(self, title,
                                f"Couldn't list the cache.\n\n{label}")
            return
        self.model.removeRows(0, self.model.rowCount())
        for r in rows:
            expires = "never" if not r["expires_at"] else _fmt_epoch(r["expires_at"])
            items = [
                QStandardItem(str(r["id"])),
                QStandardItem(str(r["caller_uid"])),
                QStandardItem(r["action"]),
                QStandardItem(r["scope"] or ""),
                QStandardItem(r["match_value"] or ""),
                QStandardItem(expires),
            ]
            # Stash the raw row on column 0 for easy recovery on Revoke.
            items[0].setData(r, Qt.ItemDataRole.UserRole + 1)
            for it in items:
                it.setEditable(False)
            self.model.appendRow(items)
        # ResizeToContents at setup-time only measures the header; re-
        # measure after rows arrive so the data actually fits.
        self.table.resizeColumnsToContents()

    def _on_revoke(self) -> None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return
        row_item = self.model.item(idx.row(), 0)
        row = row_item.data(Qt.ItemDataRole.UserRole + 1)
        try:
            ok = self.broker.revoke_approval(int(row["id"]))
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(self, "Revoke failed", label)
            return
        if not ok:
            QMessageBox.information(self, "Nothing to revoke",
                                    f"Approval id={row['id']} was already gone.")
        self.refresh()


class SessionManagerBridge(QObject):
    """Thin proxy around org.qdistro.SessionManager1 on the system bus.

    Signal `siloChanged(name, state)` re-emits the daemon's SiloChanged
    signal onto the Qt main-thread. The Silos tab refreshes on every
    emit (cheap: ListSilos is read-only and runs in-process in the
    manager).
    """

    siloChanged = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        # DBusGMainLoop is already set up by BrokerBridge in main();
        # constructing a SystemBus here just hooks onto it.
        self.bus = dbus.SystemBus()
        self._proxy = self.bus.get_object(
            SESSION_MANAGER_BUS_NAME, SESSION_MANAGER_OBJ_PATH)
        self.bus.add_signal_receiver(
            self._on_changed, signal_name="SiloChanged",
            dbus_interface=SESSION_MANAGER_BUS_NAME,
        )

    def _on_changed(self, name, state):
        self.siloChanged.emit(str(name), str(state))

    def _reconnect(self) -> None:
        self._proxy = self.bus.get_object(
            SESSION_MANAGER_BUS_NAME, SESSION_MANAGER_OBJ_PATH)

    # Same retriable set as BrokerBridge — only retry on owner-loss /
    # restart errors, never on application-level DBusExceptions.
    _RETRIABLE_DBUS_ERRORS = BrokerBridge._RETRIABLE_DBUS_ERRORS

    def _call(self, name, *args):
        method = getattr(self._proxy, name)
        try:
            return method(*args, dbus_interface=SESSION_MANAGER_BUS_NAME)
        except dbus.DBusException as exc:
            if exc.get_dbus_name() not in self._RETRIABLE_DBUS_ERRORS:
                raise
            self._reconnect()
            method = getattr(self._proxy, name)
            return method(*args, dbus_interface=SESSION_MANAGER_BUS_NAME)

    def list_silos(self) -> list[dict]:
        import json
        raw = self._call("ListSilos")
        return json.loads(str(raw))

    def create(self, name: str, uid: int) -> None:
        self._call("CreateSilo", str(name), int(uid))

    def delete(self, name: str) -> None:
        self._call("DeleteSilo", str(name))

    def start(self, name: str) -> None:
        self._call("StartSilo", str(name))

    def stop(self, name: str, grace_s: int = 5) -> None:
        self._call("StopSilo", str(name), int(grace_s))

    def freeze(self, name: str) -> None:
        self._call("FreezeSilo", str(name))

    def resume(self, name: str) -> None:
        self._call("ResumeSilo", str(name))


class NewSiloDialog(QDialog):
    """+ New silo dialog. Default uid suggestion = max(2000, max_uid+1)."""

    def __init__(self, suggested_uid: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New silo")
        form = QFormLayout(self)
        self.name_edit = QLineEdit(self); self.name_edit.setObjectName("silo_name_edit")
        self.uid_spin = QSpinBox(self); self.uid_spin.setObjectName("silo_uid_spin")
        self.uid_spin.setRange(2000, 60000)
        self.uid_spin.setValue(int(suggested_uid))
        form.addRow("Name:", self.name_edit)
        form.addRow("UID:", self.uid_spin)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class SilosTab(QWidget):
    """Tab listing silos with state badges + lifecycle action buttons.

    Refreshes on SiloChanged or after a successful action. Refuses to
    construct without a SessionManagerBridge — the tab is gated at
    MainWindow startup based on whether the system bus has the
    SessionManager1 well-known name.
    """

    COLUMNS = ("name", "uid", "state", "autostart")
    STATE_COLOURS = {
        "Created":  QColor("#a0a0a0"),
        "Active":   QColor("#7bc97b"),
        "Frozen":   QColor("#e6c95a"),
        "Stopping": QColor("#e69a5a"),
        "Stopped":  QColor("#d56b6b"),
        "Deleting": QColor("#666666"),
    }

    def __init__(self, session: SessionManagerBridge):
        super().__init__()
        self.session = session

        self.table = QTableView(self); self.table.setObjectName("silos_table")
        self.table.setSelectionBehavior(
            QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        # Let Tab advance OUT of the table to the row-action buttons; Qt's
        # default tabKeyNavigation traps Tab inside the view, and mouse is
        # unavailable on the labwc/XWayland GUI test template.
        self.table.setTabKeyNavigation(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setModel(self.model)

        btns = QHBoxLayout()
        self.btn_new = QPushButton("+ New silo"); self.btn_new.setObjectName("btn_new_silo")
        self.btn_start = QPushButton("Start"); self.btn_start.setObjectName("btn_start_silo")
        self.btn_stop = QPushButton("Stop"); self.btn_stop.setObjectName("btn_stop_silo")
        self.btn_freeze = QPushButton("Freeze"); self.btn_freeze.setObjectName("btn_freeze_silo")
        self.btn_resume = QPushButton("Resume"); self.btn_resume.setObjectName("btn_resume_silo")
        self.btn_delete = QPushButton("Delete"); self.btn_delete.setObjectName("btn_delete_silo")
        self.btn_refresh = QPushButton("Refresh"); self.btn_refresh.setObjectName("btn_refresh_silos")
        for b in (self.btn_new, self.btn_start, self.btn_stop, self.btn_freeze,
                  self.btn_resume, self.btn_delete, self.btn_refresh):
            btns.addWidget(b)
        btns.addStretch()

        self.btn_new.clicked.connect(self._on_new)
        self.btn_start.clicked.connect(lambda: self._do("start"))
        self.btn_stop.clicked.connect(lambda: self._do("stop"))
        self.btn_freeze.clicked.connect(lambda: self._do("freeze"))
        self.btn_resume.clicked.connect(lambda: self._do("resume"))
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_refresh.clicked.connect(self.refresh)

        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(btns)

        # Coalesce SiloChanged bursts into one refresh per ~200ms.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(200)
        self._refresh_timer.timeout.connect(self.refresh)
        self.session.siloChanged.connect(
            lambda _n, _s: self._refresh_timer.start())

    def refresh(self) -> None:
        try:
            rows = self.session.list_silos()
        except dbus.DBusException as e:
            title, label = _friendly_broker_error(e)
            QMessageBox.warning(self, title,
                                f"Couldn't list silos.\n\n{label}")
            return
        self.model.removeRows(0, self.model.rowCount())
        for r in rows:
            items = [
                QStandardItem(str(r.get("name", ""))),
                QStandardItem(str(r.get("uid", ""))),
                QStandardItem(str(r.get("state", ""))),
                QStandardItem("yes" if r.get("autostart") else "no"),
            ]
            colour = self.STATE_COLOURS.get(str(r.get("state", "")))
            if colour is not None:
                items[2].setForeground(colour)
            for it in items:
                it.setEditable(False)
            items[0].setData(r, Qt.ItemDataRole.UserRole + 1)
            self.model.appendRow(items)
        self.table.resizeColumnsToContents()

    def _selected_row(self) -> dict | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        item = self.model.item(idx.row(), 0)
        return item.data(Qt.ItemDataRole.UserRole + 1)

    def _suggest_next_uid(self) -> int:
        try:
            rows = self.session.list_silos()
        except dbus.DBusException:
            return 2000
        if not rows:
            return 2000
        return max(2000, max(int(r.get("uid", 1999)) for r in rows) + 1)

    def _on_new(self) -> None:
        dlg = NewSiloDialog(self._suggest_next_uid(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.name_edit.text().strip()
        uid = int(dlg.uid_spin.value())
        if not name:
            QMessageBox.warning(self, "Invalid name", "Silo name required.")
            return
        try:
            self.session.create(name, uid)
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(self, "Create failed", label)
            return
        self.refresh()

    def _do(self, action: str) -> None:
        row = self._selected_row()
        if row is None:
            return
        name = str(row.get("name", ""))
        try:
            if action == "start":
                self.session.start(name)
            elif action == "stop":
                self.session.stop(name, 5)
            elif action == "freeze":
                self.session.freeze(name)
            elif action == "resume":
                self.session.resume(name)
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(self, f"{action.capitalize()} failed",
                                 label)
            return
        self.refresh()

    def _on_delete(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        name = str(row.get("name", ""))
        # Destructive: explicit confirm.
        confirm = QMessageBox.question(
            self, "Delete silo",
            f"Delete silo {name!r}? This removes the Linux user, "
            f"the per-silo state directory, and the home subvolume.")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.session.delete(name)
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(self, "Delete failed", label)
            return
        self.refresh()


def _fmt_epoch(ts: int) -> str:
    """Short human-ish expiry rendering. Mirrors the CLI's _fmt_expiry
    but is kept local to avoid a cross-module import — the CLI has
    root-only dependencies we don't want in the admin app."""
    import time as _t
    left = ts - int(_t.time())
    if left <= 0:
        return "expired"
    if left < 3600:
        return f"in {left // 60}m"
    if left < 86400:
        return f"in {left // 3600}h{(left % 3600) // 60}m"
    return f"in {left // 86400}d"


def _format_age(epoch_s: float) -> str:
    """Return a short human-readable string for how long ago `epoch_s` was.

    Examples: "3s", "45s", "2m10s", "7m", "1h2m".
    """
    elapsed = max(0, int(_time.time() - epoch_s))
    if elapsed < 60:
        return f"{elapsed}s"
    mins, secs = divmod(elapsed, 60)
    if mins < 60:
        return f"{mins}m{secs}s" if secs else f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins}m" if mins else f"{hours}h"


def _age_color(epoch_s: float) -> QColor:
    """Return a color for the age of a pending request.

    green  <30s, yellow 30s-2m, orange 2m-5m, red >5m.
    """
    elapsed = max(0, _time.time() - epoch_s)
    if elapsed < 30:
        return QColor("#4caf50")   # green
    if elapsed < 120:
        return QColor("#ffeb3b")   # yellow
    if elapsed < 300:
        return QColor("#ff9800")   # orange
    return QColor("#f44336")       # red


# Default mute duration in seconds (30 minutes).
MUTE_DURATION_S = 30 * 60

# Escalation thresholds (seconds).
ESCALATION_THRESHOLD_S = 60    # re-notify after 60s pending
CRITICAL_THRESHOLD_S = 300     # critical-level after 5 min
CROSS_UID_THRESHOLD_S = 30     # cross-uid / sensitive actions escalate faster
# Maximum escalation notifications per age-tick to prevent burst spam
# when many requests cross a threshold simultaneously.
ESCALATION_MAX_PER_TICK = 3


class NotificationManager(QObject):
    """Owns mute state, pending-request timestamps, desktop notifications,
    and the age-display refresh timer.

    Separated from MainWindow so the notification logic is testable
    without constructing the full window hierarchy.
    """
    # Emitted every ~10 seconds so the UI can repaint age columns.
    ageTickFired = pyqtSignal()

    ESCALATION_THRESHOLD_S = ESCALATION_THRESHOLD_S
    CRITICAL_THRESHOLD_S = CRITICAL_THRESHOLD_S
    CROSS_UID_THRESHOLD_S = CROSS_UID_THRESHOLD_S
    ESCALATION_MAX_PER_TICK = ESCALATION_MAX_PER_TICK

    def __init__(self, tray_icon: QSystemTrayIcon, parent: QObject | None = None):
        super().__init__(parent)
        self._tray_icon = tray_icon
        self._muted = False
        self._mute_timer = QTimer(self)
        self._mute_timer.setSingleShot(True)
        self._mute_timer.timeout.connect(self._unmute)

        # Map of request-id -> arrival epoch (float). Populated by
        # on_request_pending(); removed by on_request_decided().
        self._arrival_times: dict[int, float] = {}

        # Per-request metadata for escalation decisions.
        # Map of request-id -> {"action": str, "uid": int}.
        self._request_meta: dict[int, dict] = {}

        # Escalation tracking: rids that already received an escalation
        # notification (avoids spamming every 10s tick).
        self._escalated_rids: set[int] = set()
        # Rids that already received a critical-level notification.
        self._critical_rids: set[int] = set()

        # Age-display refresh: tick every 10 seconds.
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(10_000)
        self._age_timer.timeout.connect(self._on_age_tick)
        self._age_timer.start()

    # ---- mute ---------------------------------------------------------------

    @property
    def muted(self) -> bool:
        return self._muted

    def mute(self, duration_s: int = MUTE_DURATION_S) -> None:
        """Suppress desktop notification popups for `duration_s` seconds."""
        self._muted = True
        self._mute_timer.start(duration_s * 1000)

    def unmute(self) -> None:
        self._muted = False
        self._mute_timer.stop()

    def _unmute(self) -> None:
        self._muted = False

    # ---- pending tracking ---------------------------------------------------

    def on_request_pending(self, rid: int, action: str = "",
                           uid: int = 0) -> None:
        """Record arrival time and metadata for a new pending request."""
        self._arrival_times.setdefault(rid, _time.time())
        if rid not in self._request_meta and (action or uid):
            self._request_meta[rid] = {"action": action, "uid": uid}

    def on_request_decided(self, rid: int) -> None:
        """Remove arrival time and escalation state for a decided request."""
        self._arrival_times.pop(rid, None)
        self._request_meta.pop(rid, None)
        self._escalated_rids.discard(rid)
        self._critical_rids.discard(rid)

    def arrival_time(self, rid: int) -> float | None:
        return self._arrival_times.get(rid)

    def sync_pending(self, pending_rids: set[int],
                     pending_requests: list[dict] | None = None) -> None:
        """Synchronize arrival-time map with the actual pending set.

        Called after a full refresh so stale entries (requests decided
        between signal and refresh) are cleaned up and new entries
        (requests that arrived before the app connected) get a synthetic
        'now' timestamp.

        If *pending_requests* is provided (the full list of dicts from
        the broker), metadata (action, uid) is backfilled for any
        request that lacks it, so escalation logic works even for
        requests discovered via refresh rather than the signal path.
        """
        # Remove entries no longer pending.
        stale = set(self._arrival_times) - pending_rids
        for rid in stale:
            del self._arrival_times[rid]
            self._request_meta.pop(rid, None)
            self._escalated_rids.discard(rid)
            self._critical_rids.discard(rid)
        # Add entries we haven't seen yet.
        now = _time.time()
        for rid in pending_rids - set(self._arrival_times):
            self._arrival_times[rid] = now
        # Backfill metadata for requests that have arrival times but
        # no action/uid metadata (e.g. discovered at startup via
        # refresh rather than through the RequestPending signal).
        if pending_requests is not None:
            for req in pending_requests:
                rid = int(req["id"])
                if rid not in self._request_meta:
                    action = req.get("action", "")
                    uid = int(req.get("uid", 0))
                    if action or uid:
                        self._request_meta[rid] = {
                            "action": action, "uid": uid}

    @property
    def pending_count(self) -> int:
        return len(self._arrival_times)

    # ---- desktop notification -----------------------------------------------

    def maybe_notify(self, action: str, uid: int,
                     window_active: bool) -> None:
        """Show a desktop notification if the window is not focused and
        notifications are not muted."""
        if self._muted:
            return
        if window_active:
            return
        self._tray_icon.showMessage(
            "Pending approval request",
            f"uid={uid}  {action}",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    # ---- escalation ---------------------------------------------------------

    # Action prefixes that represent cross-uid / cross-silo or otherwise
    # sensitive operations in the broker. These match the real action
    # strings constructed in qdistro_admin_broker.py and qsu, as well as
    # legacy/test names containing "cross"/"xuid".
    _CROSS_UID_PREFIXES = (
        "qdistro.clipboard.transfer:",
        "qdistro.clipboard.receive:",
        "qdistro.handoff.activate:",
        "app.send-to:",
        "qsu.exec:",
    )

    @staticmethod
    def _is_cross_uid_action(action: str) -> bool:
        """Return True if *action* looks like a cross-uid or sensitive op."""
        lower = action.lower()
        if "cross" in lower or "xuid" in lower:
            return True
        for prefix in NotificationManager._CROSS_UID_PREFIXES:
            if action.startswith(prefix):
                return True
        return False

    def _escalation_threshold(self, rid: int) -> int:
        """Return the escalation threshold (seconds) for *rid*.

        Cross-uid or sensitive actions use the shorter
        ``CROSS_UID_THRESHOLD_S``; everything else uses
        ``ESCALATION_THRESHOLD_S``.
        """
        meta = self._request_meta.get(rid, {})
        if self._is_cross_uid_action(meta.get("action", "")):
            return self.CROSS_UID_THRESHOLD_S
        return self.ESCALATION_THRESHOLD_S

    def _check_escalation(self, window_active: bool) -> None:
        """Re-notify for stale pending requests.

        Called from the age-tick timer (~every 10s). Walks all pending
        requests and, if the admin window is still unfocused and
        notifications are not muted, sends escalating re-notifications:

        * After ``ESCALATION_THRESHOLD_S`` (or ``CROSS_UID_THRESHOLD_S``
          for cross-uid actions): send an *Information*-level repeat.
        * After ``CRITICAL_THRESHOLD_S``: upgrade to *Critical*-level.

        Each level fires at most once per request (tracked by
        ``_escalated_rids`` / ``_critical_rids``).  At most
        ``ESCALATION_MAX_PER_TICK`` notifications are emitted per tick
        to avoid burst spam when many requests cross a threshold
        simultaneously; the remainder are deferred to the next tick.
        """
        if self._muted or window_active:
            return

        now = _time.time()
        emitted = 0
        for rid, arrival in self._arrival_times.items():
            if emitted >= self.ESCALATION_MAX_PER_TICK:
                break
            elapsed = now - arrival
            meta = self._request_meta.get(rid, {})
            action = meta.get("action", "request")
            uid = meta.get("uid", 0)
            threshold = self._escalation_threshold(rid)

            # Critical escalation (>300s by default).
            if elapsed >= self.CRITICAL_THRESHOLD_S \
                    and rid not in self._critical_rids:
                self._critical_rids.add(rid)
                # Also mark as escalated so we don't double-fire below.
                self._escalated_rids.add(rid)
                self._tray_icon.showMessage(
                    "CRITICAL — stale approval request",
                    f"uid={uid}  {action}  "
                    f"(waiting {_format_age(arrival)})",
                    QSystemTrayIcon.MessageIcon.Critical,
                    10_000,
                )
                emitted += 1
                continue

            # Standard escalation (>60s or >30s for cross-uid).
            if elapsed >= threshold and rid not in self._escalated_rids:
                self._escalated_rids.add(rid)
                self._tray_icon.showMessage(
                    "Reminder — pending approval request",
                    f"uid={uid}  {action}  "
                    f"(waiting {_format_age(arrival)})",
                    QSystemTrayIcon.MessageIcon.Information,
                    7000,
                )
                emitted += 1

    def _on_age_tick(self) -> None:
        """Age-timer callback: emit the repaint signal and run escalation."""
        self.ageTickFired.emit()
        # Escalation needs to know whether the window is active.
        # The parent is typically MainWindow; fall back to False (escalate)
        # if not available.
        window_active = False
        parent = self.parent()
        if parent is not None and hasattr(parent, "isActiveWindow"):
            window_active = parent.isActiveWindow()
        self._check_escalation(window_active)


class MainWindow(QMainWindow):
    def __init__(self, broker: BrokerBridge,
                 session: SessionManagerBridge | None = None):
        super().__init__()
        self.broker = broker
        self.session = session
        self.setWindowTitle("admin approvals")
        self.resize(900, 600)

        # Pending approvals pane (existing splitter layout).
        pending = QWidget(); pending.setObjectName("pending_tab")
        self.pending_tab = pending
        psplit = QSplitter(Qt.Orientation.Horizontal, pending)
        self.list = QListView(); self.list.setObjectName("queue_list")
        self.model = QStandardItemModel(self)
        self.list.setModel(self.model)
        psplit.addWidget(self.list)

        self.detail = DetailPane()
        self.stack = QStackedWidget(); self.stack.addWidget(self.detail)
        psplit.addWidget(self.stack)
        psplit.setStretchFactor(0, 1); psplit.setStretchFactor(1, 2)
        pending_lay = QVBoxLayout(pending); pending_lay.setContentsMargins(0, 0, 0, 0)
        pending_lay.addWidget(psplit)

        # Cache pane — second tab.
        self.cache_tab = CacheTab(broker)

        # Silos pane — third tab, only when the session manager is up.
        self.silos_tab: SilosTab | None = None
        if self.session is not None:
            self.silos_tab = SilosTab(self.session)

        # Rules, History, and Workflows tabs
        self.rules_tab = RulesTab(broker)
        self.history_tab = HistoryTab(broker)
        self.workflows_tab = WorkflowsTab(broker)

        # Tab container. Pending first so it's the default on launch.
        self.tabs = QTabWidget(self); self.tabs.setObjectName("main_tabs")
        self.tabs.addTab(pending, "Pending")
        self.tabs.addTab(self.cache_tab, "Cache")
        self.tabs.addTab(self.rules_tab, "Rules")
        self.tabs.addTab(self.history_tab, "History")
        self.tabs.addTab(self.workflows_tab, "Workflows")
        if self.silos_tab is not None:
            self.tabs.addTab(self.silos_tab, "Silos")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)

        self.list.selectionModel().currentChanged.connect(self._on_selection)
        self.detail.decided.connect(self._on_decided)
        # "Rule from this" — DetailPane button and HistoryTab button
        # both route into _create_rule_from_current (pending) or
        # _create_rule_from_history (history entry). The Ctrl+R
        # shortcut is already wired to _create_rule_from_current.
        self.detail.ruleFromThis.connect(
            lambda req: self._create_rule_from_current())
        self.history_tab.ruleFromHistory.connect(
            self._create_rule_from_history)

        # Coalesce rapid signal bursts into at most one refresh() per
        # ~250ms. A compromised uid calling RequestPermission in a tight
        # loop would otherwise pin the UI thread rebuilding the model.
        # start() restarts the timer if it is already running, so a
        # continuous burst keeps pushing the refresh out until quiescence.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self.refresh)
        self.broker.requestPending.connect(lambda _rid: self._refresh_timer.start())
        self.broker.requestDecided.connect(lambda _rid, _decision: self._refresh_timer.start())
        # ApprovalRevoked is broker→subscriber for cache-row deletions
        # the admin app didn't initiate (CLI revoke, RevokeAllForUid,
        # another peer's Cache-tab Revoke). Refresh the Cache tab so
        # it doesn't render rows the broker no longer holds. Same 250ms
        # coalescer as Pending — a RevokeAllForUid burst collapses to
        # one refresh.
        self._cache_refresh_timer = QTimer(self)
        self._cache_refresh_timer.setSingleShot(True)
        self._cache_refresh_timer.setInterval(250)
        self._cache_refresh_timer.timeout.connect(self.cache_tab.refresh)
        self.broker.approvalRevoked.connect(
            lambda _uid, _action, _exe: self._cache_refresh_timer.start())

        # Tray integration — try StatusNotifierItem (Wayland-native)
        # first; fall back to QSystemTrayIcon (X11 / XEmbed).
        #
        # StatusNotifierItem is the Wayland-era tray protocol supported
        # by qdshell, KDE Plasma, and GNOME (via AppIndicator extensions).
        # QSystemTrayIcon.show() is a no-op on Wayland compositors that
        # do not provide the XEmbed compatibility shim.
        self._sni: StatusNotifierItemService | None = None
        self._sni = _try_status_notifier({
            "activate": self._show_from_tray,
            "toggle_mute": self._toggle_mute_from_tray,
            "quit": self._confirm_quit_from_tray,
        })

        # Always construct QSystemTrayIcon — NotificationManager and
        # the badge painter reference it; the SNI path uses it as the
        # fallback notification surface.
        self._tray_base_pixmap: QPixmap = QApplication.style().standardIcon(
            QStyle.StandardPixmap.SP_ComputerIcon).pixmap(32, 32)
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon(self._tray_base_pixmap))
        self.tray_icon.setToolTip("qdistro admin approvals")

        # Create tray menu — Quit gated by a confirmation so a stray
        # right-click+enter doesn't silently kill the only approval
        # surface on the desktop.
        tray_menu = QMenu()
        show_action = tray_menu.addAction("Show")
        self._mute_action = tray_menu.addAction("Mute 30m")
        quit_action = tray_menu.addAction("Quit")
        show_action.triggered.connect(self._show_from_tray)
        self._mute_action.triggered.connect(self._toggle_mute_from_tray)
        quit_action.triggered.connect(self._confirm_quit_from_tray)
        self.tray_icon.setContextMenu(tray_menu)

        # Connect tray icon click to show window.
        self.tray_icon.activated.connect(self._tray_icon_clicked)

        if self._sni is not None:
            # SNI path: the D-Bus tray integration handles the icon.
            # Still set QApplication to not quit on last window close
            # so close-to-tray works.
            QApplication.instance().setQuitOnLastWindowClosed(False)
            self._tray_available = True
            _log.info("Tray integration: StatusNotifierItem (D-Bus)")
        elif QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()
            QApplication.instance().setQuitOnLastWindowClosed(False)
            self._tray_available = True
            _log.info("Tray integration: QSystemTrayIcon (X11/XEmbed)")
        else:
            self._tray_available = False
            _log.info("Tray integration: none available")

        # Pulse-animation state for the tray badge. _tray_pulse_timer
        # toggles between two badge tints for ~3s after a new pending
        # request arrives, then settles back to the steady badge.
        self._tray_pulse_timer = QTimer(self)
        self._tray_pulse_timer.setInterval(300)
        self._tray_pulse_phase = False
        self._tray_pulse_remaining = 0
        self._tray_pulse_timer.timeout.connect(self._tray_pulse_tick)
        self._tray_last_count = 0

        # Notification manager — owns mute state, arrival timestamps,
        # desktop notifications, and the age-refresh timer.
        self.notifications = NotificationManager(self.tray_icon, parent=self)
        self.notifications.ageTickFired.connect(self._refresh_age_column)

        # Shortcuts. WindowShortcut context so they only fire while the
        # admin window itself is active — they can't be triggered from a
        # background window or while the app sits in the tray.
        #
        # Decision keys (approve/deny/rule, bulk, per-silo) act on the
        # *currently selected pending request*, so they are guarded
        # (guard=True, the default): pressed from any other tab, the
        # first keypress raises the Pending tab so the admin sees what
        # they're about to act on; a second keypress commits. This stops
        # a stray Ctrl+Y on the Rules/History tab from silently approving
        # a request the admin never looked at.
        #
        # The scope-picker keys (Ctrl+Shift+1..8) only tick a radio
        # button on the detail pane and commit nothing, so they are
        # exempt (guard=False) — guarding them would force a pointless
        # double-press when an admin pre-selects a scope from elsewhere.
        def _mk_shortcut(seq: str, slot, *, guard: bool = True):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)

            def _activated(_slot=slot):
                if guard and self._ensure_pending_tab():
                    return
                _slot()

            sc.activated.connect(_activated)
            return sc

        # doc/admin-approval.md: Ctrl+Y or Alt+A approves CURRENT.
        # We honour the doc here: Alt+A is an alias for Ctrl+Y.
        # Bulk approve-all moves to Ctrl+Shift+A (with confirmation).
        # Alt+Shift+A approves all in the currently-selected silo
        # (uid filter on pending) — also with confirmation.
        _mk_shortcut("Ctrl+Y", lambda: self.detail._emit("allow"))
        _mk_shortcut("Alt+A", lambda: self.detail._emit("allow"))
        _mk_shortcut("Ctrl+N", lambda: self.detail._emit("deny"))
        _mk_shortcut("Alt+D", lambda: self.detail._emit("deny"))
        _mk_shortcut("Ctrl+R", self._create_rule_from_current)
        _mk_shortcut("Ctrl+Shift+A", self._approve_all_pending)
        _mk_shortcut("Ctrl+Shift+D", self._deny_all_pending)
        _mk_shortcut("Alt+Shift+A",
                     lambda: self._approve_all_in_selected_silo("allow"))
        _mk_shortcut("Alt+Shift+D",
                     lambda: self._approve_all_in_selected_silo("deny"))

        # Scope shortcuts (Ctrl+Shift+1..8).
        for i, scope_key in enumerate(["once", "1h", "24h", "forever",
                                       "forever_exe", "forever_argv",
                                       "forever_basename", "forever_prefix"]):
            _mk_shortcut(f"Ctrl+Shift+{i+1}",
                         lambda s=scope_key: self._set_scope(s),
                         guard=False)

        # Help menu surfacing the documented shortcuts.
        self._install_help_menu()

        # Tray-icon refresh on signals. Lambda wraps drop the signal
        # args; signature drift in BrokerBridge won't silently break
        # the slot. requestPending also kicks off the pulse animation
        # so the admin sees a visual cue when a new request lands.
        self.broker.requestPending.connect(
            lambda _rid: self._on_new_pending(_rid))
        self.broker.requestDecided.connect(
            lambda _rid, _dec: self._on_request_decided(_rid))
        self.broker.approvalRevoked.connect(
            lambda _u, _a, _e: self._update_tray_icon())

        # Initial tray update
        QTimer.singleShot(0, self._update_tray_icon)

        self.refresh()

    def refresh(self):
        prev_rid = None
        sel = self.list.selectionModel().currentIndex()
        if sel.isValid():
            prev_rid = self.model.itemFromIndex(sel).data(Qt.ItemDataRole.UserRole + 1)["id"]

        # Refresh runs from the QTimer timeout slot and from direct calls
        # after a decide. The broker can be transiently unavailable (e.g.
        # admin ran `systemctl restart qdistro-admin-broker`); an
        # uncaught DBusException here would kill the whole Qt event loop
        # and take the admin app down with it. BrokerBridge._call already
        # rebuilds the proxy and retries once; if it still raises we
        # leave the list untouched and show a passive toast — next
        # refresh (driven by the next signal or user action) will retry.
        try:
            pending = self.broker.get_pending()
        except dbus.DBusException as e:
            title, _label = _friendly_broker_error(e)
            self.statusBar().showMessage(title, 8000)
            return

        # Sync NotificationManager arrival-time map with actual pending set.
        # Pass the full request list so metadata is backfilled for
        # requests discovered at startup / after missed signals.
        self.notifications.sync_pending(
            {int(r["id"]) for r in pending},
            pending_requests=pending,
        )

        self.model.clear()
        for req in pending:
            rid = int(req["id"])
            arrival = self.notifications.arrival_time(rid)
            age_str = _format_age(arrival) if arrival else "?"
            item = QStandardItem(f"uid={req['uid']}  {req['action']}  [{age_str}]")
            item.setData(req, Qt.ItemDataRole.UserRole + 1)
            item.setEditable(False)
            # Color the entire row foreground based on waiting-time
            # escalation so the admin can triage by age at a glance.
            if arrival is not None:
                item.setForeground(_age_color(arrival))
                item.setToolTip(f"Waiting: {age_str}")
            self.model.appendRow(item)

        self._update_window_title()

        if self.model.rowCount() == 0:
            self.detail.clear()
            return
        target_row = 0
        if prev_rid is not None:
            for r in range(self.model.rowCount()):
                if self.model.item(r).data(Qt.ItemDataRole.UserRole + 1)["id"] == prev_rid:
                    target_row = r
                    break
        idx = self.model.index(target_row, 0)
        self.list.setCurrentIndex(idx)
        # setCurrentIndex normally fires currentChanged → _on_selection
        # which rebinds the detail pane. After a model.clear() bounce,
        # though, Qt's selectionModel can short-circuit the signal when
        # the new "current" position matches the same row index a since-
        # removed item used to occupy. Force-rebind so the detail pane
        # never stays stale after a decide-removes-row transition.
        self._on_selection(idx, idx)
        if self.tabs.currentWidget() is self.pending_tab:
            self.list.setFocus(Qt.FocusReason.OtherFocusReason)

    def event(self, event):  # noqa: D102,N802 - Qt API
        handled = super().event(event)
        if event.type() == QEvent.Type.WindowActivate:
            self._focus_pending_list_after_activation()
        return handled

    def _focus_pending_list_after_activation(self) -> None:
        if self.tabs.currentWidget() is not self.pending_tab:
            return
        QTimer.singleShot(
            0, lambda: self.list.setFocus(Qt.FocusReason.ActiveWindowFocusReason))

    def _on_selection(self, current, _previous):
        if not current.isValid():
            self.detail.clear()
            return
        req = self.model.itemFromIndex(current).data(Qt.ItemDataRole.UserRole + 1)
        self.detail.show_request(req)

    def _on_decided(self, rid: int, decision: str, scope: str):
        try:
            self.broker.decide(rid, decision, scope)
        except dbus.DBusException as e:
            # The broker refused to record this decision. For
            # ScopeNotPermitted the pending request is still retryable;
            # for audit failures it has already failed closed. In both
            # cases the admin gets an explicit modal AND the refusal is
            # recorded against the request so it persists inline in the
            # detail view (with expandable raw detail) after refresh —
            # not only as a transient popup. (item 3)
            #
            # The modal shows a bounded, shoulder-safe label; the raw
            # D-Bus string is logged by _friendly_broker_error and kept
            # behind the detail pane's collapsible "Show Error Details"
            # affordance, never in the dialog body. (item 1)
            title, label = _friendly_broker_error(e)
            self.detail.set_broker_error(rid, label, str(e))
            # Tailor the modal copy: a policy refusal (e.g.
            # ScopeNotPermitted) leaves the request pending and retryable,
            # so don't claim it was "denied as a safety measure" — that
            # phrasing only fits fail-closed errors (e.g. audit failure).
            dbus_name = ""
            try:
                dbus_name = e.get_dbus_name() or ""
            except Exception:  # noqa: BLE001
                dbus_name = ""
            if dbus_name.endswith(".ScopeNotPermitted"):
                body = (
                    f"{title}: the broker rejected the chosen scope for "
                    f"this decision. The request is still pending — you "
                    f"can retry with a different scope.\n\n{label}"
                )
            else:
                body = (
                    f"{title}: the broker could not record this decision "
                    f"and denied the request as a safety measure.\n\n{label}"
                )
            QMessageBox.critical(self, "Decision not recorded", body)
        else:
            # Decision recorded — clear any stale refusal note for this rid.
            self.detail.clear_broker_error(rid)
        self.refresh()

    def _ensure_pending_tab(self) -> bool:
        """Guard for the pending-decision shortcuts. If the Pending tab
        is not the active tab, raise it (and focus the queue list) and
        return True so the caller aborts without committing — the admin
        then sees the request before a second keypress acts on it.
        Returns False when the Pending tab is already active, so the
        action proceeds immediately."""
        if self.tabs.currentWidget() is self.pending_tab:
            return False
        self.tabs.setCurrentWidget(self.pending_tab)
        self.list.setFocus()
        return True

    def _on_tab_changed(self, index: int) -> None:
        # Refresh the Cache / Silos / Rules / History tabs lazily so they're fresh when
        # the admin navigates to them; skipping when they're hidden
        # keeps Pending-tab interaction snappy.
        if self.tabs.widget(index) is self.cache_tab:
            self.cache_tab.refresh()
        elif self.rules_tab is not None and self.tabs.widget(index) is self.rules_tab:
            self.rules_tab.refresh()
        elif self.history_tab is not None and self.tabs.widget(index) is self.history_tab:
            self.history_tab.refresh()
        elif self.silos_tab is not None and self.tabs.widget(index) is self.silos_tab:
            self.silos_tab.refresh()

    def _tray_icon_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _confirm_quit_from_tray(self) -> None:
        """Quit is destructive — every queued approval blocks until the
        admin app comes back. Ask first."""
        confirm = QMessageBox.question(
            self, "Quit admin app?",
            "Quit the admin approval app?\n\n"
            "Pending permission requests will queue with no admin "
            "surface to decide them until you restart the app.")
        if confirm == QMessageBox.StandardButton.Yes:
            QApplication.instance().quit()

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt API
        """Close → minimise to tray when a tray is available; otherwise
        accept the close (preserves previous behavior on tray-less
        hosts)."""
        if self._tray_available and self.tray_icon.isVisible():
            event.ignore()
            self.hide()
        else:
            event.accept()

    def _on_new_pending(self, rid: int = 0) -> None:
        """RequestPending arrived — track it, kick off the pulse animation,
        refresh the tray badge, show desktop notification if appropriate,
        and update the window title."""
        self.notifications.on_request_pending(rid)
        self._tray_pulse_remaining = 10  # ~3 seconds at 300ms ticks
        if not self._tray_pulse_timer.isActive():
            self._tray_pulse_timer.start()
        self._update_tray_icon()
        # Desktop notification — fetch the request details so the popup
        # includes the action name and requesting uid. Also store the
        # metadata on NotificationManager so escalation checks have
        # access to it later.
        try:
            pending = self.broker.get_pending()
            for req in pending:
                if int(req["id"]) == rid:
                    action = req["action"]
                    uid = int(req["uid"])
                    # Update metadata for escalation tracking.
                    self.notifications.on_request_pending(
                        rid, action=action, uid=uid)
                    self.notifications.maybe_notify(
                        action, uid, self.isActiveWindow(),
                    )
                    break
        except dbus.DBusException:
            pass

    def _on_request_decided(self, rid: int) -> None:
        """RequestDecided arrived — update tracking and tray."""
        self.notifications.on_request_decided(rid)
        self._update_tray_icon()

    def _toggle_mute_from_tray(self) -> None:
        """Toggle mute state from the tray context menu."""
        if self.notifications.muted:
            self.notifications.unmute()
            self._mute_action.setText("Mute 30m")
            if self._sni is not None:
                self._sni.set_mute_label("Mute 30m")
        else:
            self.notifications.mute(MUTE_DURATION_S)
            self._mute_action.setText("Unmute")
            if self._sni is not None:
                self._sni.set_mute_label("Unmute")

    def _refresh_age_column(self) -> None:
        """Repaint the Waiting column in the pending list with updated ages.

        Called every ~10s by the NotificationManager.ageTickFired signal.
        Updates both the display text (uid + action + [age]) and the
        foreground color for age-based escalation so the admin can
        triage stale requests at a glance.
        """
        for row in range(self.model.rowCount()):
            item = self.model.item(row, 0)
            if item is None:
                continue
            req = item.data(Qt.ItemDataRole.UserRole + 1)
            if req is None:
                continue
            rid = int(req["id"])
            arrival = self.notifications.arrival_time(rid)
            if arrival is None:
                continue
            age_str = _format_age(arrival)
            # Rebuild the display text with the refreshed age.
            item.setText(f"uid={req['uid']}  {req['action']}  [{age_str}]")
            item.setForeground(_age_color(arrival))
            item.setToolTip(f"Waiting: {age_str}")
        # Also update window title with current count
        self._update_window_title()

    def _update_window_title(self) -> None:
        """Set window title including pending count when > 0."""
        count = self.notifications.pending_count
        if count > 0:
            self.setWindowTitle(f"admin approvals ({count} pending)")
        else:
            self.setWindowTitle("admin approvals")

    def _tray_pulse_tick(self) -> None:
        self._tray_pulse_phase = not self._tray_pulse_phase
        self._tray_pulse_remaining -= 1
        if self._tray_pulse_remaining <= 0:
            self._tray_pulse_phase = False
            self._tray_pulse_timer.stop()
        self._render_tray_badge(self._tray_last_count)

    def _render_tray_badge(self, count: int) -> None:
        """Composite the pending count over the base tray pixmap.

        Draws a small circle in the bottom-right corner with the count
        rendered in bold white. Pulse phase toggles the badge fill color
        between red and orange so the admin sees motion. Zero count
        renders the bare base pixmap (no overlay).
        """
        pm = QPixmap(self._tray_base_pixmap)
        if count > 0:
            painter = QPainter(pm)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                w, h = pm.width(), pm.height()
                # Badge in bottom-right corner; ~45% the icon side.
                d = int(min(w, h) * 0.55)
                x = w - d
                y = h - d
                fill = QColor("#e04040")
                if self._tray_pulse_phase:
                    fill = QColor("#ff8c1a")
                painter.setBrush(fill)
                painter.setPen(QPen(QColor("white"), 1))
                painter.drawEllipse(x, y, d, d)
                painter.setPen(QPen(QColor("white")))
                f = QFont()
                f.setBold(True)
                # Tune font size to badge diameter so 1–2 digits fit.
                f.setPixelSize(max(8, int(d * 0.7)))
                painter.setFont(f)
                label = str(count) if count < 100 else "99+"
                painter.drawText(x, y, d, d,
                                 Qt.AlignmentFlag.AlignCenter, label)
            finally:
                painter.end()
        self.tray_icon.setIcon(QIcon(pm))
        # Push the composited pixmap to the SNI so hosts that use
        # IconPixmap (instead of / in addition to IconName) see the badge.
        if self._sni is not None:
            self._sni.set_icon_pixmap(pm)

    def _update_tray_icon(self):
        # Get the current pending count
        try:
            pending = self.broker.get_pending()
            count = len(pending)
        except dbus.DBusException:
            # If broker is unavailable, just show the basic tooltip and
            # don't redraw the badge — keep the last-known count.
            tooltip = "qdistro admin approvals (broker unavailable)"
            self.tray_icon.setToolTip(tooltip)
            if self._sni is not None:
                self._sni.set_tooltip(tooltip)
            return
        # Sync NotificationManager's arrival-time map with actual pending.
        self.notifications.sync_pending(
            {int(r["id"]) for r in pending},
            pending_requests=pending,
        )
        self._tray_last_count = count
        if count > 0:
            tooltip = f"qdistro admin approvals ({count} pending)"
            self.tray_icon.setToolTip(tooltip)
        else:
            tooltip = "qdistro admin approvals"
            self.tray_icon.setToolTip(tooltip)
            # No pending → drop any in-flight pulse and clear the badge.
            self._tray_pulse_remaining = 0
            self._tray_pulse_timer.stop()
        self._render_tray_badge(count)
        # Update the StatusNotifierItem if active.
        if self._sni is not None:
            self._sni.set_tooltip(tooltip)
            self._sni.set_pending_count(count)
        self._update_window_title()

    def _install_help_menu(self) -> None:
        """Surface the documented shortcuts in a Help menu so admins
        discover them without grepping the docs."""
        from PyQt6.QtGui import QAction
        bar = self.menuBar()
        help_menu = bar.addMenu("&Help")
        act_keys = QAction("Keyboard &shortcuts…", self)
        act_keys.triggered.connect(self._show_keyboard_shortcuts)
        help_menu.addAction(act_keys)

    def _show_keyboard_shortcuts(self) -> None:
        text = (
            "Approve current request:\n"
            "  Ctrl+Y   or   Alt+A\n\n"
            "Deny current request:\n"
            "  Ctrl+N   or   Alt+D\n\n"
            "Rule from this request:\n"
            "  Ctrl+R\n\n"
            "Approve ALL pending (with confirmation):\n"
            "  Ctrl+Shift+A\n"
            "Deny ALL pending (with confirmation):\n"
            "  Ctrl+Shift+D\n\n"
            "Approve all pending in current silo only (with confirmation):\n"
            "  Alt+Shift+A\n"
            "Deny all pending in current silo only (with confirmation):\n"
            "  Alt+Shift+D\n\n"
            "The decision keys above act on the selected pending request\n"
            "and fire only while this window is active. Pressed from\n"
            "another tab, the first keypress switches to the Pending tab\n"
            "so you see the request; press again to act on it.\n\n"
            "Scope picker (sets DetailPane scope radio — takes effect from\n"
            "any tab, no confirmation):\n"
            "  Ctrl+Shift+1  once\n"
            "  Ctrl+Shift+2  1 hour\n"
            "  Ctrl+Shift+3  24 hours\n"
            "  Ctrl+Shift+4  forever (any command)\n"
            "  Ctrl+Shift+5  forever (this exact program)\n"
            "  Ctrl+Shift+6  forever (this exact argv tuple)\n"
            "  Ctrl+Shift+7  forever (argv basename anywhere)\n"
            "  Ctrl+Shift+8  forever (argv prefix + any trailing args)\n"
        )
        QMessageBox.information(self, "Keyboard shortcuts", text)

    def _get_active_scope(self) -> str:
        """Read the currently-checked scope radio button. Falls back to
        "once" when nothing is checked (shouldn't happen, but the
        default-deny posture wants the safest scope)."""
        for key, rb in self.detail._scope_buttons:
            if rb.isChecked():
                return key
        return "once"

    def _create_rule_from_current(self):
        # Create a rule based on the currently selected request.
        sel = self.list.selectionModel().currentIndex()
        if not sel.isValid():
            QMessageBox.information(
                self, "No selection", "Please select a request first.")
            return
        req = self.model.itemFromIndex(sel).data(
            Qt.ItemDataRole.UserRole + 1)

        # H1 security fix: rule_data uses the FLAT schema
        # _generate_yaml_from_rule expects (uid/action/exe at top
        # level). Previously the producer nested these under "match"
        # and the consumer never found them, generating an allow-all
        # `match: {}` rule — an admin-app side-channel for blanket
        # approvals. Use the scope picker's current value rather than
        # hard-coding "once".
        rule_data = {
            "name": f"Rule for {req['action']} from uid {req['uid']}",
            "decision": "allow",
            "uid": req["uid"],
            "action": req["action"],
            "exe": req["exe"],
            "scope": self._get_active_scope(),
            "rationale": f"Auto-generated rule from request {req['id']}",
        }

        # Show rule editor dialog
        dialog = RuleEditorDialog(self.broker, rule_data, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            filename = dialog.filename_line.text().strip()
            if not filename.endswith(".yaml"):
                filename += ".yaml"
            yaml_content = dialog.yaml_editor.toPlainText()
            # Defensive: reject an allow rule with empty match before
            # the broker even sees it. Belt-and-braces against H1
            # regressions — a future refactor that drops uid/action/
            # exe from the flat dict would otherwise regress to the
            # allow-all path.
            if self._yaml_is_allow_all(yaml_content):
                QMessageBox.warning(
                    self, "Refused (overly broad)",
                    "This rule has no match selectors and would apply "
                    "to every uid + action + exe. Add at least one of "
                    "uid / action / exe to the match block before "
                    "saving.")
                return
            result = self.broker.save_rule(filename, yaml_content)
            QMessageBox.information(
                self, "Success", f"Rule saved to {result}")
            if hasattr(self, "rules_tab"):
                self.rules_tab.refresh()
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(self, "Error saving rule", label)

    @staticmethod
    def _yaml_is_allow_all(yaml_body: str) -> bool:
        """Return True iff parsing the rule body yields an `allow`
        decision with an empty / absent match block. Used as a
        client-side guardrail; broker's `_rule_from_dict` allows empty
        match so this is the only place to catch it before commit."""
        try:
            import yaml
        except ImportError:
            return False
        try:
            entries = yaml.safe_load(yaml_body) or []
        except Exception:  # noqa: BLE001
            # Let the broker surface YAML errors with its own message.
            return False
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("decision") != "allow":
                continue
            match = entry.get("match") or {}
            if not isinstance(match, dict):
                continue
            # Any selector counts — uid, action, exe, app_id,
            # sandbox_engine, mime_type, argv_*.
            if not any(v not in (None, "", []) for v in match.values()):
                return True
        return False

    def _create_rule_from_history(self, entry: dict):
        """Create a rule pre-populated from a history (audit) entry."""
        uid = entry.get("caller_uid", -1)
        action = entry.get("action", "")
        exe = entry.get("caller_exe", "")
        decision = "allow" if entry.get("decision") else "deny"

        rule_data = {
            "name": f"Rule for {action} from uid {uid}",
            "decision": decision,
            "uid": uid,
            "action": action,
            "exe": exe,
            "scope": self._get_active_scope(),
            "rationale": "Auto-generated rule from history entry",
        }

        dialog = RuleEditorDialog(self.broker, rule_data, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            filename = dialog.filename_line.text().strip()
            if not filename.endswith(".yaml"):
                filename += ".yaml"
            yaml_content = dialog.yaml_editor.toPlainText()
            if self._yaml_is_allow_all(yaml_content):
                QMessageBox.warning(
                    self, "Refused (overly broad)",
                    "This rule has no match selectors and would apply "
                    "to every uid + action + exe. Add at least one of "
                    "uid / action / exe to the match block before "
                    "saving.")
                return
            result = self.broker.save_rule(filename, yaml_content)
            QMessageBox.information(
                self, "Success", f"Rule saved to {result}")
            if hasattr(self, "rules_tab"):
                self.rules_tab.refresh()
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(self, "Error saving rule", label)

    # ----- bulk decide -----------------------------------------------------
    #
    # H2 correctness fix: snapshot pending IDs once, decide per-ID with
    # try/except so a single broker failure doesn't strand the rest of
    # the queue, drive the loop off QTimer.singleShot(0, ...) so the
    # Qt main thread stays responsive, surface a QProgressDialog for
    # batches larger than 10, and report a summary at the end.
    # Scope honors the active radio button (H3 correctness / S3 ops).
    # Both bulk actions require explicit confirmation.

    def _approve_all_pending(self) -> None:
        self._bulk_decide("allow", scope_filter=None)

    def _deny_all_pending(self) -> None:
        self._bulk_decide("deny", scope_filter=None)

    def _approve_all_in_selected_silo(self, decision: str) -> None:
        sel = self.list.selectionModel().currentIndex()
        if not sel.isValid():
            QMessageBox.information(
                self, "No selection",
                "Select a request to pick the silo first.")
            return
        req = self.model.itemFromIndex(sel).data(
            Qt.ItemDataRole.UserRole + 1)
        uid = int(req["uid"])
        self._bulk_decide(decision, scope_filter=("uid", uid))

    def _bulk_decide(self, decision: str,
                     scope_filter: tuple | None) -> None:
        try:
            pending = self.broker.get_pending()
        except dbus.DBusException as e:
            title, label = _friendly_broker_error(e)
            QMessageBox.critical(
                self, title,
                f"Couldn't fetch pending requests.\n\n{label}")
            return
        if scope_filter is not None:
            kind, val = scope_filter
            pending = [r for r in pending if r.get(kind) == val]
        if not pending:
            QMessageBox.information(
                self, "Nothing to do",
                "No pending requests match the bulk-decide filter.")
            return
        scope = self._get_active_scope()
        verb = "Approve" if decision == "allow" else "Deny"
        scope_warn = ""
        if scope != "once":
            scope_warn = (
                f"\n\nWARNING: scope is '{scope}' — these decisions "
                f"may persist beyond the current request.")
        filter_chunk = ""
        if scope_filter is not None:
            kind, val = scope_filter
            filter_chunk = f"\nFilter: {kind}={val}"
        confirm = QMessageBox.question(
            self, f"{verb} all?",
            f"{verb} {len(pending)} pending request"
            f"{'s' if len(pending) != 1 else ''}?\n"
            f"Scope: {scope}{filter_chunk}{scope_warn}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # The chosen `scope` is already captured in the local above and the
        # async loop closes over it, so resetting the picker here is safe
        # and does not change which scope this batch applies. Reset so a
        # non-`once` scope does not silently carry forward to the next
        # (single or bulk) decision. (item 2)
        self.detail.reset_scope()

        rids = [int(r["id"]) for r in pending]
        total = len(rids)
        progress: QProgressDialog | None = None
        if total > 10:
            progress = QProgressDialog(
                f"{verb}ing {total} requests…", "Cancel", 0, total, self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.setValue(0)

        state = {
            "i": 0,
            "ok": 0,
            "failures": [],   # list of (rid, error_str)
            "cancelled": False,
        }

        def _step() -> None:
            if state["cancelled"] or state["i"] >= total:
                _finalize()
                return
            if progress is not None and progress.wasCanceled():
                state["cancelled"] = True
                _finalize()
                return
            rid = rids[state["i"]]
            try:
                self.broker.decide(rid, decision, scope)
                state["ok"] += 1
            except dbus.DBusException as e:
                # Bounded label for the inline summary; raw goes to stderr
                # (via _friendly_broker_error's WARNING log + the print
                # below) and is recorded against the request so it also
                # surfaces inline in the detail view while it stays
                # pending. (items 1 + 3)
                _title, label = _friendly_broker_error(e)
                state["failures"].append((rid, label))
                self.detail.set_broker_error(rid, label, str(e))
                print(
                    f"[admin_app] bulk-{decision} rid={rid} failed: {e}",
                    file=sys.stderr,
                )
            state["i"] += 1
            if progress is not None:
                progress.setValue(state["i"])
            QTimer.singleShot(0, _step)

        def _finalize() -> None:
            if progress is not None:
                progress.close()
            self.refresh()
            ok = state["ok"]
            fail = len(state["failures"])
            if state["cancelled"]:
                title = f"{verb} all — cancelled"
            else:
                title = f"{verb} all"
            msg = f"{verb}ed {ok} of {total} requests."
            if fail:
                # Summarize first few failures inline; full detail goes
                # to stderr above.
                preview = "\n".join(
                    f"  rid={rid}: {err}"
                    for rid, err in state["failures"][:3])
                more = (f"\n  …and {fail - 3} more"
                        if fail > 3 else "")
                msg += f"\nFailures ({fail}):\n{preview}{more}"
            QMessageBox.information(self, title, msg)

        QTimer.singleShot(0, _step)

    def _set_scope(self, scope: str):
        # Find the radio button for the given scope and select it
        for key, rb in self.detail._scope_buttons:
            if key == scope:
                rb.setChecked(True)
                break


class RulesTab(QWidget):
    """Tab for managing approval rules. Lists existing rules from broker's
    ListRules and allows adding/editing/deleting rules."""
    COLUMNS = ("name", "decision", "uid", "action", "exe", "scope", "rationale")

    def __init__(self, broker: BrokerBridge):
        super().__init__()
        self.broker = broker

        self.table = QTableView(self); self.table.setObjectName("rules_table")
        self.table.setSelectionBehavior(
            QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        # Let Tab advance OUT of the table to the row-action buttons; Qt's
        # default tabKeyNavigation traps Tab inside the view, and mouse is
        # unavailable on the labwc/XWayland GUI test template.
        self.table.setTabKeyNavigation(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setModel(self.model)

        feature_row = QHBoxLayout()
        self.firefox_uid_spin = QSpinBox()
        self.firefox_uid_spin.setObjectName("firefox_containers_uid")
        self.firefox_uid_spin.setRange(1, 999999)
        self.firefox_uid_spin.setValue(2000)
        self.firefox_containers_toggle = QCheckBox(
            "Firefox containers cross-user opt-in")
        self.firefox_containers_toggle.setObjectName(
            "firefox_containers_cross_user_toggle")
        self.firefox_containers_toggle.setToolTip(
            "Creates an allow rule for qdistro.browser.containers.cross_uid:containers.* "
            "for the selected uid.")
        feature_row.addWidget(QLabel("Target uid"))
        feature_row.addWidget(self.firefox_uid_spin)
        feature_row.addWidget(self.firefox_containers_toggle)
        feature_row.addStretch()
        self._rules_cache: list[dict] = []

        btns = QHBoxLayout()
        self.btn_add_rule = QPushButton("Add Rule"); self.btn_add_rule.setObjectName("btn_add_rule")
        self.btn_edit_rule = QPushButton("Edit Rule"); self.btn_edit_rule.setObjectName("btn_edit_rule")
        self.btn_delete_rule = QPushButton("Delete Rule"); self.btn_delete_rule.setObjectName("btn_delete_rule")
        self.btn_reload = QPushButton("Reload"); self.btn_reload.setObjectName("btn_reload_rules")
        self.btn_reload.setToolTip("Force broker to re-walk /etc/qdistro/rules.d/")
        self.btn_refresh = QPushButton("Refresh"); self.btn_refresh.setObjectName("btn_refresh_rules")
        for b in (self.btn_add_rule, self.btn_edit_rule, self.btn_delete_rule,
                  self.btn_reload, self.btn_refresh):
            btns.addWidget(b)
        btns.addStretch()

        lay = QVBoxLayout(self)
        lay.addLayout(feature_row)
        lay.addWidget(self.table)
        lay.addLayout(btns)

        self.btn_add_rule.clicked.connect(self._on_add_rule)
        self.btn_edit_rule.clicked.connect(self._on_edit_rule)
        self.btn_delete_rule.clicked.connect(self._on_delete_rule)
        self.btn_reload.clicked.connect(self._on_reload)
        self.btn_refresh.clicked.connect(self.refresh)
        self.firefox_uid_spin.valueChanged.connect(
            self._sync_firefox_containers_toggle)
        self.firefox_containers_toggle.toggled.connect(
            self._on_firefox_containers_toggled)

        # Live-update: subscribe to broker's RulesReloaded signal so
        # the table re-populates when rules change on disk (inotify,
        # SIGHUP, SaveRule, or another admin's ReloadRules call).
        # Coalesce rapid bursts into one refresh per ~250ms.
        self._reload_refresh_timer = QTimer(self)
        self._reload_refresh_timer.setSingleShot(True)
        self._reload_refresh_timer.setInterval(250)
        self._reload_refresh_timer.timeout.connect(self.refresh)
        self.broker.rulesReloaded.connect(
            lambda _count: self._reload_refresh_timer.start())

    def refresh(self) -> None:
        try:
            rules = self.broker.list_rules()
        except dbus.DBusException as e:
            title, label = _friendly_broker_error(e)
            QMessageBox.warning(self, title,
                                f"Couldn't list rules.\n\n{label}")
            return
        self.model.removeRows(0, self.model.rowCount())
        self._rules_cache = list(rules)
        for r in rules:
            # Convert None values to readable format
            uid_str = str(r["uid"]) if r["uid"] != -1 else "any"
            action_str = r["action"] if r["action"] else "any"
            exe_str = r["exe"] if r["exe"] else "any"
            scope_str = r["scope"] if r["scope"] else "inherit"
            items = [
                QStandardItem(str(r["name"])),
                QStandardItem(str(r["decision"])),
                QStandardItem(uid_str),
                QStandardItem(action_str),
                QStandardItem(exe_str),
                QStandardItem(scope_str),
                QStandardItem(str(r["rationale"])),
            ]
            # Stash the raw rule on column 0 for easy recovery
            items[0].setData(r, Qt.ItemDataRole.UserRole + 1)
            for it in items:
                it.setEditable(False)
            self.model.appendRow(items)
        self.table.resizeColumnsToContents()
        self._sync_firefox_containers_toggle()

    def _firefox_containers_rule_for_uid(self, uid: int) -> dict | None:
        for rule in self._rules_cache:
            if _is_firefox_containers_rule(rule, uid):
                return rule
        return None

    def _sync_firefox_containers_toggle(self) -> None:
        enabled = self._firefox_containers_rule_for_uid(
            self.firefox_uid_spin.value()) is not None
        old = self.firefox_containers_toggle.blockSignals(True)
        try:
            self.firefox_containers_toggle.setChecked(enabled)
        finally:
            self.firefox_containers_toggle.blockSignals(old)

    def _on_firefox_containers_toggled(self, checked: bool) -> None:
        uid = self.firefox_uid_spin.value()
        try:
            if checked:
                self.broker.save_rule(
                    _firefox_containers_rule_filename(uid),
                    _firefox_containers_rule_yaml(uid))
            else:
                rule = self._firefox_containers_rule_for_uid(uid)
                if rule is not None:
                    self.broker.delete_rule(
                        str(rule.get("source_path", "")),
                        str(rule.get("name", "")))
            self.refresh()
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(
                self, "Firefox containers opt-in failed", label)
            self.refresh()

    def _selected_row(self) -> dict | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        item = self.model.item(idx.row(), 0)
        return item.data(Qt.ItemDataRole.UserRole + 1)

    def _on_reload(self) -> None:
        """Force the broker to re-walk /etc/qdistro/rules.d/ and rebuild
        its rule set. Useful after hand-editing files on disk."""
        try:
            self.broker.reload_rules()
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(self, "Reload failed",
                                 f"ReloadRules failed.\n\n{label}")
            return
        # The RulesReloaded signal will trigger refresh() via the
        # coalesced timer, but do an immediate refresh too so the
        # admin sees instant feedback.
        self.refresh()

    def _on_add_rule(self) -> None:
        dialog = RuleEditorDialog(self.broker, {}, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save_dialog_rule(dialog, "Rule saved to")

    def _on_edit_rule(self) -> None:
        row = self._selected_row()
        if row is None:
            QMessageBox.warning(self, "No selection", "Please select a rule to edit.")
            return
        dialog = RuleEditorDialog(self.broker, row, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save_dialog_rule(dialog, "Rule updated at")

    def _save_dialog_rule(self, dialog, success_prefix: str) -> None:
        try:
            filename = dialog.filename_line.text().strip()
            if not filename.endswith('.yaml'):
                filename += '.yaml'
            yaml_content = dialog.yaml_editor.toPlainText()
            if MainWindow._yaml_is_allow_all(yaml_content):
                QMessageBox.warning(
                    self, "Refused (overly broad)",
                    "This rule has no match selectors and would apply "
                    "to every uid + action + exe. Add at least one "
                    "selector to the match block before saving.")
                return
            result = self.broker.save_rule(filename, yaml_content)
            QMessageBox.information(
                self, "Success", f"{success_prefix} {result}")
            self.refresh()
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.critical(self, "Error saving rule", label)

    def _on_delete_rule(self) -> None:
        row = self._selected_row()
        if row is None:
            QMessageBox.warning(
                self, "No selection", "Please select a rule to delete.")
            return
        name = str(row.get("name", ""))
        confirm = QMessageBox.question(
            self, "Delete rule",
            f"Delete rule {name!r}? This action cannot be undone.")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # Deletion goes through the broker's DeleteRule RPC so the
        # broker re-applies path-safety fail-closed and writes the
        # audit row (security-hardening-carryforward §"Broker and
        # rules"). The admin app no longer unlinks the file directly,
        # so it no longer needs write access to the root-owned rules.d
        # and there is now a proper broker-side audit trail.
        filepath = str(row.get("source_path", ""))
        if not filepath:
            QMessageBox.warning(
                self, "No source path",
                "Broker did not return a source_path for this rule; "
                "cannot delete from the admin app.")
            return
        try:
            _deleted, _count, errors = self.broker.delete_rule(filepath, name)
        except dbus.DBusException as e:
            # Always route through _friendly_broker_error so the raw D-Bus
            # diagnostic is logged (and never shown raw) on every branch,
            # including the UnknownMethod special-case below. (item 1)
            _title, label = _friendly_broker_error(e)
            dbus_name = ""
            try:
                dbus_name = e.get_dbus_name() or ""
            except Exception:  # noqa: BLE001
                dbus_name = ""
            if dbus_name.endswith(".UnknownMethod") or \
                    "UnknownMethod" in str(e):
                QMessageBox.critical(
                    self, "DeleteRule unavailable",
                    "This broker does not expose a DeleteRule RPC. "
                    "Upgrade the broker, or remove the rule file "
                    "manually as the admin user.")
                return
            QMessageBox.critical(
                self, "Error deleting rule",
                f"Broker refused or failed to delete {name!r}.\n\n{label}")
            self.refresh()
            return
        # The broker reloads as part of DeleteRule; surface any
        # post-reload errors so a typo'd YAML elsewhere isn't masked.
        if errors:
            QMessageBox.warning(
                self, "Deleted with reload warnings",
                f"Rule {name} deleted. Reload reported:\n\n"
                + "\n".join(errors))
        else:
            QMessageBox.information(
                self, "Success", f"Rule {name} deleted")
        self.refresh()


class RuleEditorDialog(QDialog):
    """Dialog for creating/editing rules with YAML editor."""
    
    def __init__(self, broker: BrokerBridge, rule_data: dict, parent=None):
        super().__init__(parent)
        self.broker = broker
        self.rule_data = rule_data
        self.setWindowTitle("Edit Rule")
        
        layout = QVBoxLayout(self)
        
        # Filename input
        filename_layout = QHBoxLayout()
        filename_layout.addWidget(QLabel("Filename:"))
        self.filename_line = QLineEdit(self)
        self.filename_line.setPlaceholderText("e.g., my-rule.yaml")
        if rule_data.get("source_path"):
            import os
            self.filename_line.setText(os.path.basename(rule_data["source_path"]))
        filename_layout.addWidget(self.filename_line)
        layout.addLayout(filename_layout)
        
        # YAML editor
        yaml_label = QLabel("Rule Definition (YAML):")
        layout.addWidget(yaml_label)
        
        self.yaml_editor = QTextEdit(self)
        self.yaml_editor.setMinimumHeight(300)
        self.yaml_editor.setMaximumHeight(500)
        layout.addWidget(self.yaml_editor)
        
        # Decide what to load into the editor:
        #   - Editing an existing rule (source_path set): fetch the RAW
        #     on-disk YAML via the broker so comments and any keys the
        #     projected ListRules view does not carry (future/unknown
        #     keys) are preserved on round-trip. Regenerating from the
        #     projected fields would silently drop them.
        #   - New rule (no source_path): fall back to a generated view
        #     from the seed fields (or the default template).
        warning_text = ""
        source_path = rule_data.get("source_path") if rule_data else None
        if source_path:
            yaml_content = ""
            try:
                yaml_content = self.broker.get_rule_source(source_path)
            except Exception as exc:  # noqa: BLE001 - degrade gracefully
                warning_text = (
                    "Could not fetch the on-disk rule source from the "
                    f"broker ({exc}). Showing a regenerated view; saving "
                    "may drop comments or unrecognized keys.")
            if not yaml_content:
                # RPC unavailable or returned empty: degrade to the
                # generated view rather than presenting a blank editor.
                if not warning_text:
                    warning_text = (
                        "The broker returned no source for this rule. "
                        "Showing a regenerated view; saving may drop "
                        "comments or unrecognized keys.")
                yaml_content = self._generate_yaml_from_rule(rule_data)
        elif rule_data:
            yaml_content = self._generate_yaml_from_rule(rule_data)
        else:
            # Default template
            yaml_content = """- name: "My Rule"
  decision: allow
  match:
    uid: 2000
    action: "qdistro.*"
    exe: "/usr/bin/example"
  scope: "once"
  rationale: "Example rule for demonstration"
"""

        if warning_text:
            warn_label = QLabel(warning_text)
            warn_label.setWordWrap(True)
            warn_label.setStyleSheet("color: #b58900;")
            layout.addWidget(warn_label)

        self.yaml_editor.setPlainText(yaml_content)
        
        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        btn_ok = QPushButton("Save")
        btn_cancel = QPushButton("Cancel")
        button_layout.addWidget(btn_ok)
        button_layout.addWidget(btn_cancel)
        layout.addLayout(button_layout)
        
        btn_ok.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
    
    def _generate_yaml_from_rule(self, rule_data: dict) -> str:
        """Generate YAML string from rule data dictionary.

        Accepts the rule_data in either of two shapes:
          - flat:   {"uid": ..., "action": ..., "exe": ...} (the shape
            ListRules returns and the shape `_create_rule_from_current`
            now produces after H1 fix)
          - nested: {"match": {"uid": ..., "action": ..., "exe": ...}}
            (legacy producers).
        Reading from both prevents the H1 regression where the
        producer wrote `match: {uid, action, exe}` but this consumer
        read those keys at top level and silently dropped them,
        yielding `match: {}` (allow-anything).
        """
        # Build the rule object
        rule_obj: dict = {
            "name": rule_data.get("name", ""),
            "decision": rule_data.get("decision", ""),
            "match": {},
        }

        if rule_data.get("rationale"):
            rule_obj["rationale"] = rule_data["rationale"]
        if rule_data.get("scope"):
            rule_obj["scope"] = rule_data["scope"]

        # Read match selectors from either shape. The nested form wins
        # if both are present (admin-app-internal callers that set
        # match explicitly are signaling intent).
        match_src = rule_data.get("match") if isinstance(
            rule_data.get("match"), dict) else rule_data

        if match_src.get("uid") is not None and match_src.get("uid") != -1:
            rule_obj["match"]["uid"] = match_src["uid"]
        if match_src.get("action"):
            rule_obj["match"]["action"] = match_src["action"]
        if match_src.get("exe"):
            rule_obj["match"]["exe"] = match_src["exe"]
        for key in ("app_id", "sandbox_engine", "mime_type",
                    "argv_basename"):
            if match_src.get(key):
                rule_obj["match"][key] = match_src[key]
        for key in ("argv_exact", "argv_prefix"):
            value = match_src.get(key)
            if value:
                rule_obj["match"][key] = list(value)

        # Convert to YAML string
        try:
            import yaml
            return yaml.dump([rule_obj], default_flow_style=False, indent=2)
        except ImportError:
            # If PyYAML is not available, create basic YAML manually
            yaml_lines = []
            yaml_lines.append(f"- name: {rule_obj['name']!r}")
            yaml_lines.append(f"  decision: {rule_obj['decision']!r}")
            yaml_lines.append("  match:")
            for key, value in rule_obj["match"].items():
                yaml_lines.append(f"    {key}: {value!r}")
            if rule_obj.get("scope"):
                yaml_lines.append(f"  scope: {rule_obj['scope']!r}")
            if rule_obj.get("rationale"):
                yaml_lines.append(f"  rationale: {rule_obj['rationale']!r}")
            return "\n".join(yaml_lines) + "\n"

    def get_rule_data(self) -> dict:
        """Return the edited rule as a dict by parsing the YAML buffer.

        Returns {} if the buffer can't be parsed (keep callers from
        racing the broker's validation pass — they get an empty dict
        and surface the error from the broker's SaveRule reply).
        """
        body = self.yaml_editor.toPlainText()
        filename = self.filename_line.text().strip()
        try:
            import yaml
            entries = yaml.safe_load(body) or []
        except ImportError:
            return {"filename": filename, "yaml_body": body}
        except Exception:  # noqa: BLE001
            return {"filename": filename, "yaml_body": body}
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            return {
                "filename": filename,
                "yaml_body": body,
                "entry": entries[0],
            }
        return {"filename": filename, "yaml_body": body}


class WorkflowsTab(QWidget):
    """Read-only view of loaded workflows and recent runs.

    Top table lists workflow definitions (ListWorkflows); bottom table
    lists recent runs (ListWorkflowRuns). Both refresh on demand, on the
    broker's RulesReloaded signal (the broker reloads workflows on the
    same trigger), and on WorkflowRunPending (a non-auto_run workflow fired
    and parked a run for approval). The "Approve selected run" button
    approves a PENDING run (ApproveWorkflowRun, admin-gated server-side);
    approval re-checks the workflow's conditions before executing.
    """
    WF_COLUMNS = ("name", "trigger", "steps", "needs", "description")
    RUN_COLUMNS = ("run_id", "workflow", "state", "started", "finished",
                   "error")

    def __init__(self, broker: BrokerBridge):
        super().__init__()
        self.broker = broker

        self.wf_table = QTableView(self)
        self.wf_table.setObjectName("workflows_table")
        self._wf_model = QStandardItemModel(self)
        self._wf_model.setHorizontalHeaderLabels(list(self.WF_COLUMNS))
        self._configure_table(self.wf_table, self._wf_model)

        self.runs_table = QTableView(self)
        self.runs_table.setObjectName("workflow_runs_table")
        self._runs_model = QStandardItemModel(self)
        self._runs_model.setHorizontalHeaderLabels(list(self.RUN_COLUMNS))
        self._configure_table(self.runs_table, self._runs_model)

        btns = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.setObjectName("btn_refresh_workflows")
        self.btn_refresh.clicked.connect(self.refresh)
        btns.addWidget(self.btn_refresh)
        self.btn_approve = QPushButton("Approve selected run")
        self.btn_approve.setObjectName("btn_approve_workflow_run")
        self.btn_approve.clicked.connect(self.approve_selected)
        btns.addWidget(self.btn_approve)
        btns.addStretch()

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Workflows"))
        lay.addWidget(self.wf_table)
        lay.addWidget(QLabel("Recent runs"))
        lay.addWidget(self.runs_table)
        lay.addLayout(btns)

        # Auto-refresh when the broker reloads (rules + workflows share
        # the same reload trigger) and when a run parks for approval.
        try:
            self.broker.bus.add_signal_receiver(
                self._on_reloaded, signal_name="RulesReloaded",
                dbus_interface=BUS_NAME, path=OBJ_PATH)
            self.broker.bus.add_signal_receiver(
                self._on_reloaded, signal_name="WorkflowRunPending",
                dbus_interface=BUS_NAME, path=OBJ_PATH)
        except Exception:  # noqa: BLE001
            pass

        self.refresh()

    def _selected_run(self) -> dict | None:
        """The run dict backing the selected row of the runs table."""
        sel = self.runs_table.selectionModel()
        if sel is None or not sel.hasSelection():
            return None
        idx = sel.selectedRows()
        if not idx:
            return None
        item = self._runs_model.item(idx[0].row(), 0)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole + 1)

    def approve_selected(self) -> None:
        run = self._selected_run()
        if not run:
            QMessageBox.information(self, "No run selected",
                                    "Select a pending run to approve.")
            return
        if str(run.get("state", "")) != "pending":
            QMessageBox.information(
                self, "Not pending",
                f"Run {run.get('run_id', '')} is "
                f"'{run.get('state', '')}', not pending; nothing to approve.")
            return
        run_id = str(run.get("run_id", ""))
        try:
            ok = self.broker.approve_workflow_run(run_id)
        except dbus.DBusException as e:
            _title, label = _friendly_broker_error(e)
            QMessageBox.warning(self, "Approve failed",
                                f"Couldn't approve run.\n\n{label}")
            return
        if not ok:
            QMessageBox.warning(
                self, "Approve failed",
                f"Run {run_id} could not be approved (already running, "
                f"gone, or queue saturated).")
        self.refresh()

    @staticmethod
    def _configure_table(table: QTableView, model: QStandardItemModel) -> None:
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        # Let Tab advance OUT of the table to the row-action buttons (the
        # runs table feeds "Approve selected run"); Qt's default
        # tabKeyNavigation traps Tab inside the view, and mouse is
        # unavailable on the labwc/XWayland GUI test template.
        table.setTabKeyNavigation(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        table.setModel(model)

    def _on_reloaded(self, *_args) -> None:
        self.refresh()

    def refresh(self) -> None:
        try:
            workflows = self.broker.list_workflows()
            runs = self.broker.list_workflow_runs(MAX_HISTORY_ENTRIES)
        except dbus.DBusException as e:
            title, label = _friendly_broker_error(e)
            QMessageBox.warning(self, title,
                                f"Couldn't list workflows.\n\n{label}")
            return
        self._populate_workflows(workflows)
        self._populate_runs(runs)

    def _populate_workflows(self, workflows: list[dict]) -> None:
        self._wf_model.removeRows(0, self._wf_model.rowCount())
        for wf in workflows:
            needs = wf.get("needs") or []
            items = [
                QStandardItem(str(wf.get("name", "-"))),
                QStandardItem(str(wf.get("trigger_type", "-"))),
                QStandardItem(str(wf.get("step_count", 0))),
                QStandardItem(", ".join(needs) if needs else "-"),
                QStandardItem(str(wf.get("description", ""))),
            ]
            items[0].setData(wf, Qt.ItemDataRole.UserRole + 1)
            for it in items:
                it.setEditable(False)
            self._wf_model.appendRow(items)
        self.wf_table.resizeColumnsToContents()

    def _populate_runs(self, runs: list[dict]) -> None:
        self._runs_model.removeRows(0, self._runs_model.rowCount())
        for r in runs:
            started = self._fmt_ts(r.get("started_at"))
            finished = self._fmt_ts(r.get("completed_at"))
            items = [
                QStandardItem(str(r.get("run_id", "-"))),
                QStandardItem(str(r.get("workflow_name", "-"))),
                QStandardItem(str(r.get("state", "-"))),
                QStandardItem(started),
                QStandardItem(finished),
                QStandardItem(str(r.get("error", "")) or "-"),
            ]
            items[0].setData(r, Qt.ItemDataRole.UserRole + 1)
            for it in items:
                it.setEditable(False)
            self._runs_model.appendRow(items)
        self.runs_table.resizeColumnsToContents()

    @staticmethod
    def _fmt_ts(ts) -> str:
        try:
            ts = float(ts or 0)
        except (TypeError, ValueError):
            return "-"
        if ts <= 0:
            return "-"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class HistoryTab(QWidget):
    """Tab showing historical approval/deny entries from admin-history.jsonl."""
    COLUMNS = ("timestamp", "uid", "action", "exe", "decision", "scope", "source", "argv")
    ruleFromHistory = pyqtSignal(dict)

    def __init__(self, broker: BrokerBridge):
        super().__init__()
        self.broker = broker
        # Local mirror so client-side filtering doesn't have to re-fetch.
        # Populated on every refresh; _apply_filter slices this in
        # memory rather than round-tripping through the broker.
        self._all_history: list[dict] = []

        self.table = QTableView(self); self.table.setObjectName("history_table")
        self.table.setSelectionBehavior(
            QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        # Let Tab advance OUT of the table to the row-action buttons; Qt's
        # default tabKeyNavigation traps Tab inside the view, and mouse is
        # unavailable on the labwc/XWayland GUI test template.
        self.table.setTabKeyNavigation(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setModel(self.model)

        # Filter controls
        filter_layout = QHBoxLayout()
        self.filter_silo_combo = QComboBox(self)
        self.filter_silo_combo.addItems(["All UIDs", "2000", "2001", "2002", "Other"])
        self.filter_action_combo = QComboBox(self)
        self.filter_action_combo.addItems(["All Actions", "Allow", "Deny"])
        self.filter_outcome_combo = QComboBox(self)
        self.filter_outcome_combo.addItems(["All Outcomes", "allow", "deny"])
        filter_layout.addWidget(QLabel("Filter UID:")); filter_layout.addWidget(self.filter_silo_combo)
        filter_layout.addWidget(QLabel("Action:")); filter_layout.addWidget(self.filter_action_combo)
        filter_layout.addWidget(QLabel("Outcome:")); filter_layout.addWidget(self.filter_outcome_combo)
        filter_btn = QPushButton("Apply Filter"); filter_btn.setObjectName("btn_apply_filter")
        filter_btn.clicked.connect(self._apply_filter)
        filter_layout.addWidget(filter_btn)
        filter_layout.addStretch()

        btns = QHBoxLayout()
        self.btn_rule_from_history = QPushButton("Rule from this")
        self.btn_rule_from_history.setObjectName("btn_rule_from_history")
        self.btn_rule_from_history.setToolTip(
            "Create a permanent rule from this history entry")
        self.btn_rule_from_history.clicked.connect(self._on_rule_from_history)
        self.btn_refresh = QPushButton("Refresh"); self.btn_refresh.setObjectName("btn_refresh_history")
        self.btn_clear = QPushButton("Clear History"); self.btn_clear.setObjectName("btn_clear_history")
        for b in (self.btn_rule_from_history, self.btn_refresh, self.btn_clear):
            btns.addWidget(b)
        btns.addStretch()

        lay = QVBoxLayout(self)
        lay.addLayout(filter_layout)
        lay.addWidget(self.table)
        lay.addLayout(btns)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_clear.clicked.connect(self._clear_history)

        # Initialize with data
        self.refresh()

    def refresh(self) -> None:
        try:
            # Get history from broker
            history = self.broker.list_history(MAX_HISTORY_ENTRIES)
        except dbus.DBusException as e:
            title, label = _friendly_broker_error(e)
            QMessageBox.warning(self, title,
                                f"Couldn't list history.\n\n{label}")
            return
        self._all_history = list(history)
        # Refresh UID dropdown from live rows (S2 operational) — keeps
        # the deployment-actual uids visible instead of the hardcoded
        # 2000/2001/2002 list.
        self._refresh_uid_dropdown()
        self._populate_model(self._filtered_rows())

    def _populate_model(self, rows: list[dict]) -> None:
        self.model.removeRows(0, self.model.rowCount())
        for h in rows:
            # Format timestamp
            ts = datetime.fromtimestamp(
                h.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
            decision_str = "Allow" if h.get("decision", False) else "Deny"
            # shlex.join so quoted/escaped argv elements render
            # visibly distinct (S1 security) — a silo can't make argv
            # impersonate a multi-command shell line in the history
            # display.
            argv = h.get("argv") or []
            argv_str = shlex.join(argv) if argv else "-"
            items = [
                QStandardItem(ts),
                QStandardItem(str(h.get("caller_uid", "-"))),
                QStandardItem(str(h.get("action", "-"))),
                QStandardItem(str(h.get("caller_exe", "-"))),
                QStandardItem(decision_str),
                QStandardItem(str(h.get("scope", "-"))),
                QStandardItem(str(h.get("source", "-"))),
                QStandardItem(argv_str),
            ]
            # Stash the raw history entry on column 0
            items[0].setData(h, Qt.ItemDataRole.UserRole + 1)
            for it in items:
                it.setEditable(False)
            self.model.appendRow(items)
        self.table.resizeColumnsToContents()

    def _refresh_uid_dropdown(self) -> None:
        """Replace the uid dropdown's contents with All + the uids
        actually present in history."""
        current = self.filter_silo_combo.currentText()
        uids = sorted({str(h.get("caller_uid"))
                       for h in self._all_history
                       if h.get("caller_uid") is not None})
        # Block signals so changing contents doesn't trigger an apply.
        self.filter_silo_combo.blockSignals(True)
        self.filter_silo_combo.clear()
        self.filter_silo_combo.addItem("All UIDs")
        for u in uids:
            self.filter_silo_combo.addItem(u)
        # Restore selection if still valid.
        idx = self.filter_silo_combo.findText(current)
        if idx >= 0:
            self.filter_silo_combo.setCurrentIndex(idx)
        self.filter_silo_combo.blockSignals(False)

    def _filtered_rows(self) -> list[dict]:
        rows = list(self._all_history)
        uid_sel = self.filter_silo_combo.currentText()
        outcome_sel = self.filter_outcome_combo.currentText()
        action_filter = self.filter_action_combo.currentText()
        if uid_sel and uid_sel != "All UIDs":
            try:
                target = int(uid_sel)
            except ValueError:
                target = None
            if target is not None:
                rows = [r for r in rows if int(r.get("caller_uid", -1)) == target]
        if outcome_sel and outcome_sel != "All Outcomes":
            want_allow = outcome_sel.lower() == "allow"
            rows = [r for r in rows if bool(r.get("decision")) == want_allow]
        if action_filter and action_filter not in ("All Actions", "Allow", "Deny"):
            # Free-form substring match on action.
            needle = action_filter.lower()
            rows = [r for r in rows
                    if needle in str(r.get("action", "")).lower()]
        return rows

    def _apply_filter(self) -> None:
        # If we never refreshed yet (broker offline at init time),
        # `_all_history` is empty — re-fetch.
        if not hasattr(self, "_all_history"):
            self.refresh()
            return
        self._populate_model(self._filtered_rows())

    def _on_rule_from_history(self) -> None:
        """Pre-populate a rule editor from the selected history row."""
        idx = self.table.currentIndex()
        if not idx.isValid():
            QMessageBox.information(
                self, "No selection",
                "Please select a history entry first.")
            return
        item = self.model.item(idx.row(), 0)
        entry = item.data(Qt.ItemDataRole.UserRole + 1)
        if entry is None:
            return
        self.ruleFromHistory.emit(entry)

    def _clear_history(self) -> None:
        # Broker has no ClearHistory RPC. Rather than show a confirm
        # dialog and then a "nothing happened" info popup, keep the
        # button visible but explicit about the limitation so admins
        # don't mistake the confirm-then-noop for a successful purge.
        QMessageBox.information(
            self, "Clear history not available",
            "The broker does not currently expose a ClearHistory RPC. "
            "History is read from the audit sqlite DB and rotates "
            "based on AUDIT_RETENTION_DAYS (default 90).")


class ErrorDetailsWidget(QWidget):
    """Expandable widget for displaying detailed error information.

    Two layers:
      * `guidance_label` — always-visible, bounded, shoulder-safe text
        (a short broker-error label + actionable guidance). Never the raw
        D-Bus string.
      * `error_text` — the raw diagnostic, hidden behind the "Show Error
        Details" toggle for the admin who explicitly wants it. Defaults
        collapsed so the raw string isn't shoulder-visible by default.
    """

    def __init__(self, error_message: str, parent=None):
        super().__init__(parent)
        self.error_message = error_message
        self.expanded = False

        layout = QVBoxLayout(self)

        # Bounded, always-visible guidance (item 1 / item 3): short label
        # the admin reads without exposing raw broker internals.
        self.guidance_label = QLabel("")
        self.guidance_label.setObjectName("broker_error_guidance")
        self.guidance_label.setTextFormat(Qt.TextFormat.PlainText)
        self.guidance_label.setWordWrap(True)
        self.guidance_label.setStyleSheet("color: #b00020; font-weight: bold;")
        self.guidance_label.hide()
        layout.addWidget(self.guidance_label)

        # Toggle button
        self.toggle_button = QPushButton("Show Error Details")
        self.toggle_button.setObjectName("btn_show_error_details")
        self.toggle_button.setCheckable(True)
        self.toggle_button.clicked.connect(self._toggle_visibility)
        layout.addWidget(self.toggle_button)

        # Error text area
        self.error_text = QTextEdit()
        self.error_text.setObjectName("broker_error_raw")
        self.error_text.setReadOnly(True)
        self.error_text.setMaximumHeight(150)
        self.error_text.hide()
        layout.addWidget(self.error_text)

        self.error_text.setPlainText(error_message)

    def set_guidance(self, guidance: str) -> None:
        """Set the always-visible bounded guidance text (no raw detail)."""
        if guidance:
            self.guidance_label.setText(guidance)
            self.guidance_label.show()
        else:
            self.guidance_label.clear()
            self.guidance_label.hide()

    def _toggle_visibility(self, checked):
        self.expanded = checked
        if checked:
            self.error_text.show()
            self.toggle_button.setText("Hide Error Details")
        else:
            self.error_text.hide()
            self.toggle_button.setText("Show Error Details")


def _maybe_session_bridge() -> SessionManagerBridge | None:
    """Construct a SessionManagerBridge if the daemon is reachable.

    On hosts without qdistro-session-manager (legacy bake, dev test
    machines), the Silos tab is simply not shown; the broker and
    Cache tabs keep working as before.
    """
    try:
        bus = dbus.SystemBus()
        bus.get_object(SESSION_MANAGER_BUS_NAME, SESSION_MANAGER_OBJ_PATH)
        # Touching get_object alone doesn't activate; do a tiny call.
        bridge = SessionManagerBridge()
        bridge.list_silos()
        return bridge
    except Exception as e:  # noqa: BLE001
        print(f"[admin_app] session manager unavailable: {e!r}",
              file=sys.stderr)
        return None


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[admin_app] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    app = QApplication(sys.argv)
    broker = BrokerBridge()
    session = _maybe_session_bridge()
    win = MainWindow(broker, session=session)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
