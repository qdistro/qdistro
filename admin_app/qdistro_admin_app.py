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

import os
import shlex
import sys
import time as _time
from datetime import datetime

import dbus
import dbus.mainloop.glib
from PyQt6.QtCore import QEvent, Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import (
    QColor, QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut,
    QStandardItem, QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox,
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

    def _call(self, name, *args):
        method = getattr(self._proxy, name)
        try:
            return method(*args, dbus_interface=BUS_NAME)
        except dbus.DBusException:
            # Broker may have restarted — rebuild the proxy and retry
            # once. A second failure is the caller's problem (propagate).
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
                "scope":        str(r["scope"]),
                "rationale":    str(r["rationale"]),
            })
        return out

    def save_rule(self, filename: str, yaml_body: str) -> str:
        return str(self._call("SaveRule", str(filename), str(yaml_body)))

    def reload_rules(self) -> None:
        self._call("ReloadRules")

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

    def show_request(self, req: dict):
        self._rid = req["id"]
        self._current_req = req
        self.lbl_user.setText(f"uid={req['uid']}  pid={req['pid']}")
        self.lbl_action.setText(f"Action: {req['action']}")
        self.lbl_exe.setText(req["exe"])
        details = req.get("details") or {}
        self.lbl_details.setText("Details: " + (", ".join(f"{k}={v}" for k, v in details.items()) or "(none)"))
        
        # Hide error widget when showing a new request
        self.error_widget.hide()
        
        # Check for error details in the request
        if "error" in details:
            self._show_error(details["error"])

    def _show_error(self, error_msg: str):
        self.error_widget.error_text.setPlainText(error_msg)
        self.error_widget.show()

    def clear(self):
        self._rid = None
        self._current_req = None
        self.lbl_user.setText("(no selection)")
        self.lbl_action.setText(""); self.lbl_exe.setText(""); self.lbl_details.setText("")

    def _emit(self, decision: str):
        if self._rid is None:
            return
        scope = "once"
        for key, rb in self._scope_buttons:
            if rb.isChecked():
                scope = key
                break
        self.decided.emit(self._rid, decision, scope)

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
            QMessageBox.warning(self, "Broker unavailable",
                                f"Couldn't list the cache:\n\n{e}")
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
            QMessageBox.critical(self, "Revoke failed", str(e))
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

    def _call(self, name, *args):
        method = getattr(self._proxy, name)
        try:
            return method(*args, dbus_interface=SESSION_MANAGER_BUS_NAME)
        except dbus.DBusException:
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
            QMessageBox.warning(self, "Session manager unavailable",
                                f"Couldn't list silos:\n\n{e}")
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
            QMessageBox.critical(self, "Create failed", str(e))
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
            QMessageBox.critical(self, f"{action.capitalize()} failed",
                                 str(e))
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
            QMessageBox.critical(self, "Delete failed", str(e))
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


class NotificationManager(QObject):
    """Owns mute state, pending-request timestamps, desktop notifications,
    and the age-display refresh timer.

    Separated from MainWindow so the notification logic is testable
    without constructing the full window hierarchy.
    """
    # Emitted every ~10 seconds so the UI can repaint age columns.
    ageTickFired = pyqtSignal()

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

        # Age-display refresh: tick every 10 seconds.
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(10_000)
        self._age_timer.timeout.connect(self.ageTickFired.emit)
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

    def on_request_pending(self, rid: int) -> None:
        """Record arrival time for a new pending request."""
        self._arrival_times.setdefault(rid, _time.time())

    def on_request_decided(self, rid: int) -> None:
        """Remove arrival time for a decided request."""
        self._arrival_times.pop(rid, None)

    def arrival_time(self, rid: int) -> float | None:
        return self._arrival_times.get(rid)

    def sync_pending(self, pending_rids: set[int]) -> None:
        """Synchronize arrival-time map with the actual pending set.

        Called after a full refresh so stale entries (requests decided
        between signal and refresh) are cleaned up and new entries
        (requests that arrived before the app connected) get a synthetic
        'now' timestamp.
        """
        # Remove entries no longer pending.
        stale = set(self._arrival_times) - pending_rids
        for rid in stale:
            del self._arrival_times[rid]
        # Add entries we haven't seen yet.
        now = _time.time()
        for rid in pending_rids - set(self._arrival_times):
            self._arrival_times[rid] = now

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

        # Rules and History tabs
        self.rules_tab = RulesTab(broker)
        self.history_tab = HistoryTab(broker)

        # Tab container. Pending first so it's the default on launch.
        self.tabs = QTabWidget(self); self.tabs.setObjectName("main_tabs")
        self.tabs.addTab(pending, "Pending")
        self.tabs.addTab(self.cache_tab, "Cache")
        self.tabs.addTab(self.rules_tab, "Rules")
        self.tabs.addTab(self.history_tab, "History")
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

        # System tray icon — only if the platform actually has a tray.
        # On Wayland compositors without StatusNotifier, the constructor
        # succeeds but show() is a noop and signals fire pointlessly.
        # We still construct the object so _update_tray_icon doesn't
        # AttributeError, but we gate show() + closeEvent behavior on
        # isSystemTrayAvailable. The base pixmap is cached so the badge
        # painter can redraw without re-fetching the system icon on
        # every refresh.
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

        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon.show()
            # Close-button minimises to tray instead of quitting; user
            # must invoke Quit from the tray menu (which is confirmed).
            QApplication.instance().setQuitOnLastWindowClosed(False)
            self._tray_available = True
        else:
            self._tray_available = False

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

        # Shortcuts. ApplicationShortcut context so Ctrl+Y/N/R and
        # Ctrl+Shift+A/D fire from any tab — the admin can be reading
        # the Rules tab when a request arrives and approve without
        # switching focus.
        def _mk_shortcut(seq: str, slot):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(slot)
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
                         lambda s=scope_key: self._set_scope(s))

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
            self.statusBar().showMessage(f"broker unavailable: {e}", 8000)
            return

        # Sync NotificationManager arrival-time map with actual pending set.
        self.notifications.sync_pending({int(r["id"]) for r in pending})

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
            # cases the admin must get an explicit modal instead of a
            # silent no-op or an easy-to-miss inline detail.
            QMessageBox.critical(
                self, "Decision not recorded",
                f"The broker could not record this decision and denied "
                f"the request as a safety measure.\n\n{e}",
            )
        self.refresh()

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
        # includes the action name and requesting uid.
        try:
            pending = self.broker.get_pending()
            for req in pending:
                if int(req["id"]) == rid:
                    self.notifications.maybe_notify(
                        req["action"], int(req["uid"]),
                        self.isActiveWindow(),
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
        else:
            self.notifications.mute(MUTE_DURATION_S)
            self._mute_action.setText("Unmute")

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

    def _update_tray_icon(self):
        # Get the current pending count
        try:
            pending = self.broker.get_pending()
            count = len(pending)
        except dbus.DBusException:
            # If broker is unavailable, just show the basic tooltip and
            # don't redraw the badge — keep the last-known count.
            self.tray_icon.setToolTip(
                "qdistro admin approvals (broker unavailable)")
            return
        # Sync NotificationManager's arrival-time map with actual pending.
        self.notifications.sync_pending({int(r["id"]) for r in pending})
        self._tray_last_count = count
        if count > 0:
            self.tray_icon.setToolTip(
                f"qdistro admin approvals ({count} pending)")
        else:
            self.tray_icon.setToolTip("qdistro admin approvals")
            # No pending → drop any in-flight pulse and clear the badge.
            self._tray_pulse_remaining = 0
            self._tray_pulse_timer.stop()
        self._render_tray_badge(count)
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
            "Scope picker (sets DetailPane scope radio):\n"
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
            QMessageBox.critical(self, "Error saving rule", str(e))

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
            "rationale": f"Auto-generated rule from history entry",
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
            QMessageBox.critical(self, "Error saving rule", str(e))

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
            QMessageBox.critical(
                self, "Broker unavailable",
                f"Couldn't fetch pending requests:\n\n{e}")
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
                state["failures"].append((rid, str(e)))
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
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setModel(self.model)

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
        lay.addWidget(self.table)
        lay.addLayout(btns)

        self.btn_add_rule.clicked.connect(self._on_add_rule)
        self.btn_edit_rule.clicked.connect(self._on_edit_rule)
        self.btn_delete_rule.clicked.connect(self._on_delete_rule)
        self.btn_reload.clicked.connect(self._on_reload)
        self.btn_refresh.clicked.connect(self.refresh)

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
            QMessageBox.warning(self, "Broker unavailable",
                                f"Couldn't list rules:\n\n{e}")
            return
        self.model.removeRows(0, self.model.rowCount())
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
            QMessageBox.critical(self, "Reload failed",
                                 f"ReloadRules failed:\n\n{e}")
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
            QMessageBox.critical(self, "Error saving rule", str(e))

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
        # The broker does not yet expose a DeleteRule RPC (tracked as
        # SHOULD-FIX). Until it does, the admin app deletes the YAML
        # file directly. Validate the path is inside the rules.d
        # directory and is not a symlink before unlinking; refuse any
        # path that escapes. Audit the deletion to stderr so a
        # post-hoc journalctl pass can see who deleted which rule.
        filepath = str(row.get("source_path", ""))
        if not filepath:
            QMessageBox.warning(
                self, "No source path",
                "Broker did not return a source_path for this rule; "
                "cannot delete from the admin app.")
            return
        try:
            real = os.path.realpath(filepath)
            rules_real = os.path.realpath(RULES_DIR)
            # The file's *containing directory* must be the rules dir
            # (rejects /etc/qdistro/rules.d/../passwd) and the file
            # itself must not be a symlink (rejects ln -s).
            if os.path.islink(filepath):
                QMessageBox.warning(
                    self, "Refused",
                    f"Refusing to delete a symlink: {filepath}")
                return
            if os.path.dirname(real) != rules_real:
                QMessageBox.warning(
                    self, "Refused",
                    f"Refusing to delete a file outside {RULES_DIR}: "
                    f"{real}")
                return
            if not os.path.exists(filepath):
                QMessageBox.warning(
                    self, "File not found",
                    f"Rule file {filepath} not found")
                return
            os.remove(filepath)
            # Local audit trail — broker doesn't see this delete so
            # there's no broker-side audit row. Stderr → journalctl
            # gives ops a forensic record at least.
            print(
                f"[admin_app] deleted rule file {real} "
                f"(name={name!r}, uid={os.getuid()})",
                file=sys.stderr,
            )
            # Reload rules; the broker returns (count, errors). Surface
            # any errors so a typo'd YAML elsewhere isn't masked.
            errors: list[str] = []
            try:
                ret = self.broker.reload_rules()
                if isinstance(ret, (tuple, list)) and len(ret) == 2:
                    _count, errors = ret
                    errors = [str(e) for e in (errors or [])]
            except dbus.DBusException as e:
                QMessageBox.warning(
                    self, "Deleted but reload failed",
                    f"Rule {name} deleted from disk but "
                    f"ReloadRules failed:\n\n{e}")
                self.refresh()
                return
            if errors:
                QMessageBox.warning(
                    self, "Deleted with reload warnings",
                    f"Rule {name} deleted. ReloadRules reported:\n\n"
                    + "\n".join(errors))
            else:
                QMessageBox.information(
                    self, "Success", f"Rule {name} deleted")
            self.refresh()
        except PermissionError as e:
            QMessageBox.critical(
                self, "Permission denied",
                f"Cannot delete {filepath!r} as uid {os.getuid()}. "
                f"The rules.d directory is typically root-owned; "
                f"run the admin app as the admin user, or add a "
                f"DeleteRule RPC to the broker.\n\n"
                f"{e.__class__.__name__}")
        except OSError as e:
            QMessageBox.critical(
                self, "Error deleting rule",
                f"{e.__class__.__name__}: {e.strerror or ''}")


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
        
        # Generate YAML from rule data if provided
        if rule_data:
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
            QMessageBox.warning(self, "Broker unavailable",
                                f"Couldn't list history:\n\n{e}")
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
    """Expandable widget for displaying detailed error information."""
    
    def __init__(self, error_message: str, parent=None):
        super().__init__(parent)
        self.error_message = error_message
        self.expanded = False
        
        layout = QVBoxLayout(self)
        
        # Toggle button
        self.toggle_button = QPushButton("Show Error Details")
        self.toggle_button.setCheckable(True)
        self.toggle_button.clicked.connect(self._toggle_visibility)
        layout.addWidget(self.toggle_button)
        
        # Error text area
        self.error_text = QTextEdit()
        self.error_text.setReadOnly(True)
        self.error_text.setMaximumHeight(150)
        self.error_text.hide()
        layout.addWidget(self.error_text)
        
        self.error_text.setPlainText(error_message)

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
    app = QApplication(sys.argv)
    broker = BrokerBridge()
    session = _maybe_session_bridge()
    win = MainWindow(broker, session=session)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
