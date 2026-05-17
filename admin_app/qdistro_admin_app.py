#!/usr/bin/env python3
"""qdistro admin approval app — Phase 1 prototype.

PyQt6 master/detail UI. Subscribes to broker's RequestPending signal;
shows queue on the left, detail+Approve/Deny on the right. Keyboard:
↑/↓ navigate, Ctrl+Y approve, Ctrl+N deny. No tray; window stays open.
"""
from __future__ import annotations

import sys
from datetime import datetime

import dbus
import dbus.mainloop.glib
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QColor, QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QSpinBox,
    QSplitter, QStackedWidget, QTableView, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu

BUS_NAME = "com.qdistro.AdminBroker1"
OBJ_PATH = "/com/qdistro/AdminBroker1"

SESSION_MANAGER_BUS_NAME = "com.qdistro.SessionManager1"
SESSION_MANAGER_OBJ_PATH = "/com/qdistro/SessionManager1"

HISTORY_FILE = "/var/lib/qdistro/admin-history.jsonl"
MAX_HISTORY_ENTRIES = 100


class BrokerBridge(QObject):
    requestPending = pyqtSignal(int)
    requestDecided = pyqtSignal(int, str)
    approvalRevoked = pyqtSignal(int, str, str)

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

    def _on_pending(self, rid):
        self.requestPending.emit(int(rid))

    def _on_decided(self, rid, decision):
        self.requestDecided.emit(int(rid), str(decision))

    def _on_revoked(self, uid, action, exe):
        self.approvalRevoked.emit(int(uid), str(action), str(exe))

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
        self.btn_approve.clicked.connect(lambda: self._emit("allow"))
        self.btn_deny.clicked.connect(lambda: self._emit("deny"))
        btns.addWidget(self.btn_approve); btns.addWidget(self.btn_deny); btns.addStretch()
        lay.addLayout(btns)

        lay.addStretch()

    def show_request(self, req: dict):
        self._rid = req["id"]
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
    """Thin proxy around com.qdistro.SessionManager1 on the system bus.

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

        # System tray icon
        from PyQt6.QtGui import QStyle
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QApplication.style().standardIcon(
            QStyle.StandardPixmap.SP_ComputerIcon))  # Using computer icon as placeholder
        self.tray_icon.setToolTip("qdistro admin approvals")
        
        # Create tray menu
        tray_menu = QMenu()
        show_action = tray_menu.addAction("Show")
        quit_action = tray_menu.addAction("Quit")
        show_action.triggered.connect(self.show)
        quit_action.triggered.connect(QApplication.instance().quit)
        self.tray_icon.setContextMenu(tray_menu)
        
        # Connect tray icon click to show window
        self.tray_icon.activated.connect(self._tray_icon_clicked)
        self.tray_icon.show()
        
        # PyQt6's QShortcut constructor does not accept `activated=` as a
        # keyword arg — the signal must be connected after construction.
        sc_approve = QShortcut(QKeySequence("Ctrl+Y"), self)
        sc_approve.activated.connect(lambda: self.detail._emit("allow"))
        sc_deny = QShortcut(QKeySequence("Ctrl+N"), self)
        sc_deny.activated.connect(lambda: self.detail._emit("deny"))
        
        # Additional keyboard shortcuts
        sc_create_rule = QShortcut(QKeySequence("Ctrl+R"), self)
        sc_create_rule.activated.connect(self._create_rule_from_current)
        
        sc_approve_all = QShortcut(QKeySequence("Alt+A"), self)
        sc_approve_all.activated.connect(self._approve_all_pending)
        
        sc_deny_all = QShortcut(QKeySequence("Alt+D"), self)
        sc_deny_all.activated.connect(self._deny_all_pending)
        
        # Scope shortcuts (Ctrl+Shift+1..8)
        for i, scope_key in enumerate(["once", "1h", "24h", "forever", "forever_exe", "forever_argv", "forever_basename", "forever_prefix"]):
            if i < 8:  # Only bind first 8 scopes
                sc_scope = QShortcut(QKeySequence(f"Ctrl+Shift+{i+1}"), self)
                sc_scope.activated.connect(lambda s=scope_key: self._set_scope(s))
        
        # Update tray icon with pending count
        self.broker.requestPending.connect(self._update_tray_icon)
        self.broker.requestDecided.connect(self._update_tray_icon)
        
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

        self.model.clear()
        for req in pending:
            item = QStandardItem(f"uid={req['uid']}  {req['action']}")
            item.setData(req, Qt.ItemDataRole.UserRole + 1)
            item.setEditable(False)
            self.model.appendRow(item)

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
            # Broker fail-closed on audit failure surfaces here. The
            # request is already denied on the broker side; show the
            # admin why their Approve didn't take.
            # Also check if this is a ScopeNotPermitted error and show inline
            error_msg = str(e)
            if "ScopeNotPermitted" in error_msg:
                # Find the corresponding row in the list and show error inline
                for row in range(self.model.rowCount()):
                    item = self.model.item(row)
                    req = item.data(Qt.ItemDataRole.UserRole + 1)
                    if req["id"] == rid:
                        # Update the item to indicate error
                        item.setText(f"uid={req['uid']}  {req['action']} [ERROR]")
                        # Show error in detail pane if this is the selected item
                        if self.detail._rid == rid:
                            self.detail._show_error(error_msg)
                        break
            else:
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
            self.show()
            self.raise_()
            self.activateWindow()

    def _update_tray_icon(self):
        # Get the current pending count
        try:
            pending = self.broker.get_pending()
            count = len(pending)
            if count > 0:
                # Show icon with badge
                self.tray_icon.setToolTip(f"qdistro admin approvals ({count} pending)")
            else:
                self.tray_icon.setToolTip("qdistro admin approvals")
        except dbus.DBusException:
            # If broker is unavailable, just show the basic tooltip
            self.tray_icon.setToolTip("qdistro admin approvals (broker unavailable)")

    def _create_rule_from_current(self):
        # Create a rule based on the currently selected request
        sel = self.list.selectionModel().currentIndex()
        if not sel.isValid():
            QMessageBox.information(self, "No selection", "Please select a request first.")
            return
        req = self.model.itemFromIndex(sel).data(Qt.ItemDataRole.UserRole + 1)
        
        # Prepare rule data
        rule_data = {
            "name": f"Rule for {req['action']} from uid {req['uid']}",
            "decision": "allow",
            "match": {
                "uid": req['uid'],
                "action": req['action'],
                "exe": req['exe'],
            },
            "scope": "once",
            "rationale": f"Auto-generated rule from request {req['id']}"
        }
        
        # Show rule editor dialog
        dialog = RuleEditorDialog(self.broker, rule_data, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                filename = dialog.filename_line.text().strip()
                if not filename.endswith('.yaml'):
                    filename += '.yaml'
                yaml_content = dialog.yaml_editor.toPlainText()
                result = self.broker.save_rule(filename, yaml_content)
                QMessageBox.information(self, "Success", f"Rule saved to {result}")
                # Refresh rules tab
                if hasattr(self, 'rules_tab'):
                    self.rules_tab.refresh()
            except dbus.DBusException as e:
                QMessageBox.critical(self, "Error saving rule", str(e))

    def _approve_all_pending(self):
        try:
            pending = self.broker.get_pending()
            for req in pending:
                self.broker.decide(req['id'], "allow", "once")
            self.refresh()
            QMessageBox.information(self, "Approve All", f"Approved {len(pending)} requests")
        except dbus.DBusException as e:
            QMessageBox.critical(self, "Error approving all", str(e))

    def _deny_all_pending(self):
        try:
            pending = self.broker.get_pending()
            for req in pending:
                self.broker.decide(req['id'], "deny", "once")
            self.refresh()
            QMessageBox.information(self, "Deny All", f"Denied {len(pending)} requests")
        except dbus.DBusException as e:
            QMessageBox.critical(self, "Error denying all", str(e))

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
        self.btn_refresh = QPushButton("Refresh"); self.btn_refresh.setObjectName("btn_refresh_rules")
        for b in (self.btn_add_rule, self.btn_edit_rule, self.btn_delete_rule, self.btn_refresh):
            btns.addWidget(b)
        btns.addStretch()

        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(btns)

        self.btn_add_rule.clicked.connect(self._on_add_rule)
        self.btn_edit_rule.clicked.connect(self._on_edit_rule)
        self.btn_delete_rule.clicked.connect(self._on_delete_rule)
        self.btn_refresh.clicked.connect(self.refresh)

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

    def _on_add_rule(self) -> None:
        dialog = RuleEditorDialog(self.broker, {}, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                filename = dialog.filename_line.text().strip()
                if not filename.endswith('.yaml'):
                    filename += '.yaml'
                yaml_content = dialog.yaml_editor.toPlainText()
                result = self.broker.save_rule(filename, yaml_content)
                QMessageBox.information(self, "Success", f"Rule saved to {result}")
                self.refresh()
            except dbus.DBusException as e:
                QMessageBox.critical(self, "Error saving rule", str(e))

    def _on_edit_rule(self) -> None:
        row = self._selected_row()
        if row is None:
            QMessageBox.warning(self, "No selection", "Please select a rule to edit.")
            return
        dialog = RuleEditorDialog(self.broker, row, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                filename = dialog.filename_line.text().strip()
                if not filename.endswith('.yaml'):
                    filename += '.yaml'
                yaml_content = dialog.yaml_editor.toPlainText()
                result = self.broker.save_rule(filename, yaml_content)
                QMessageBox.information(self, "Success", f"Rule updated at {result}")
                self.refresh()
            except dbus.DBusException as e:
                QMessageBox.critical(self, "Error updating rule", str(e))

    def _on_delete_rule(self) -> None:
        row = self._selected_row()
        if row is None:
            QMessageBox.warning(self, "No selection", "Please select a rule to delete.")
            return
        name = str(row.get("name", ""))
        confirm = QMessageBox.question(
            self, "Delete rule",
            f"Delete rule {name!r}? This action cannot be undone.")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # For deletion, we need to delete the file and reload
        try:
            import os
            filepath = row.get("source_path", "")
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
                # Reload rules after deletion
                self.broker.reload_rules()
                QMessageBox.information(self, "Success", f"Rule {name} deleted")
                self.refresh()
            else:
                QMessageBox.warning(self, "File not found", f"Rule file {filepath} not found")
        except Exception as e:
            QMessageBox.critical(self, "Error deleting rule", str(e))


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
        """Generate YAML string from rule data dictionary."""
        # Build the rule object
        rule_obj = {
            "name": rule_data.get("name", ""),
            "decision": rule_data.get("decision", ""),
            "match": {},
        }
        
        if rule_data.get("rationale"):
            rule_obj["rationale"] = rule_data["rationale"]
        
        if rule_data.get("scope"):
            rule_obj["scope"] = rule_data["scope"]
        
        # Add match conditions
        if rule_data.get("uid") and rule_data["uid"] != -1:
            rule_obj["match"]["uid"] = rule_data["uid"]
        if rule_data.get("action"):
            rule_obj["match"]["action"] = rule_data["action"]
        if rule_data.get("exe"):
            rule_obj["match"]["exe"] = rule_data["exe"]
        
        # Convert to YAML string
        import yaml
        try:
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
        return {}


class HistoryTab(QWidget):
    """Tab showing historical approval/deny entries from admin-history.jsonl."""
    COLUMNS = ("timestamp", "uid", "action", "exe", "decision", "scope", "source", "argv")

    def __init__(self, broker: BrokerBridge):
        super().__init__()
        self.broker = broker

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
        self.btn_refresh = QPushButton("Refresh"); self.btn_refresh.setObjectName("btn_refresh_history")
        self.btn_clear = QPushButton("Clear History"); self.btn_clear.setObjectName("btn_clear_history")
        for b in (self.btn_refresh, self.btn_clear):
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
        self.model.removeRows(0, self.model.rowCount())
        for h in history:
            # Format timestamp
            ts = datetime.fromtimestamp(h.get("ts", 0)).strftime('%Y-%m-%d %H:%M:%S')
            decision_str = "Allow" if h.get("decision", False) else "Deny"
            argv_str = " ".join(h.get("argv", [])) if h.get("argv") else "-"
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

    def _apply_filter(self) -> None:
        # For now, just refresh the data
        self.refresh()

    def _clear_history(self) -> None:
        confirm = QMessageBox.question(
            self, "Clear history",
            "Clear history? This will delete all history entries.")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        # Currently we can't clear history via broker, so warn user
        QMessageBox.information(self, "Info", "History clearing requires broker support")


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
