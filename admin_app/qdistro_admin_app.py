#!/usr/bin/env python3
"""qdistro admin approval app — Phase 1 prototype.

PyQt6 master/detail UI. Subscribes to broker's RequestPending signal;
shows queue on the left, detail+Approve/Deny on the right. Keyboard:
↑/↓ navigate, Ctrl+Y approve, Ctrl+N deny. No tray; window stays open.
"""
from __future__ import annotations

import sys

import dbus
import dbus.mainloop.glib
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QColor, QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QSpinBox,
    QSplitter, QStackedWidget, QTableView, QTabWidget, QVBoxLayout, QWidget,
)

BUS_NAME = "com.qdistro.AdminBroker1"
OBJ_PATH = "/com/qdistro/AdminBroker1"

SESSION_MANAGER_BUS_NAME = "com.qdistro.SessionManager1"
SESSION_MANAGER_OBJ_PATH = "/com/qdistro/SessionManager1"


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

        # Tab container. Pending first so it's the default on launch.
        self.tabs = QTabWidget(self); self.tabs.setObjectName("main_tabs")
        self.tabs.addTab(pending, "Pending")
        self.tabs.addTab(self.cache_tab, "Cache")
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

        # PyQt6's QShortcut constructor does not accept `activated=` as a
        # keyword arg — the signal must be connected after construction.
        sc_approve = QShortcut(QKeySequence("Ctrl+Y"), self)
        sc_approve.activated.connect(lambda: self.detail._emit("allow"))
        sc_deny = QShortcut(QKeySequence("Ctrl+N"), self)
        sc_deny.activated.connect(lambda: self.detail._emit("deny"))

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
            QMessageBox.critical(
                self, "Decision not recorded",
                f"The broker could not record this decision and denied "
                f"the request as a safety measure.\n\n{e}",
            )
        self.refresh()

    def _on_tab_changed(self, index: int) -> None:
        # Refresh the Cache / Silos tabs lazily so they're fresh when
        # the admin navigates to them; skipping when they're hidden
        # keeps Pending-tab interaction snappy.
        if self.tabs.widget(index) is self.cache_tab:
            self.cache_tab.refresh()
        elif self.silos_tab is not None and self.tabs.widget(index) is self.silos_tab:
            self.silos_tab.refresh()


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
