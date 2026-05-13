#!/usr/bin/env python3
"""qstub-sender — Phase 3 stub sender.

One window, one button. Right-click the button to build a "Send to…"
menu listing every running receiver (enumerated via
broker.ListReceivers); clicking a menu item calls broker.RelayMessage
to ship a fixed payload over admin-approval to that receiver.

The payload is fixed in Phase 3 ("hello from <user>") — senders in
Phase 4 will let the user type their own. Here we just validate the
wire path.
"""
from __future__ import annotations

import os
import pwd
import sys

import dbus
import dbus.mainloop.glib

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QMenu, QPushButton,
)

BROKER_BUS = "com.qdistro.AdminBroker1"
BROKER_PATH = "/com/qdistro/AdminBroker1"
BROKER_IFACE = "com.qdistro.AdminBroker1"

# WaitForDecision's default is 10 minutes (matches SDK). A sender
# that locks its UI for that long isn't usable; Phase 3 picks 120s
# so the admin has time to click but a forgotten request doesn't
# pin the button forever.
SEND_TIMEOUT_S = 120.0


class RelayWorker(QThread):
    """Runs broker.RelayMessage off the GUI thread.

    dbus-python calls are blocking from the caller's perspective;
    running them on the Qt event thread freezes the window until
    admin clicks. A QThread lets the spinner-equivalent stay alive.
    """
    done = pyqtSignal(bool, str)  # (ok, message)

    def __init__(self, target_uid: int, service: str,
                 kind: str, payload: str):
        super().__init__()
        self._target_uid = target_uid
        self._service = service
        self._kind = kind
        self._payload = payload

    def run(self) -> None:
        try:
            bus = dbus.SystemBus()
            proxy = bus.get_object(BROKER_BUS, BROKER_PATH)
            proxy.RelayMessage(
                dbus.Int32(self._target_uid),
                dbus.String(self._service),
                dbus.String(self._kind),
                dbus.String(self._payload),
                dbus_interface=BROKER_IFACE,
                timeout=SEND_TIMEOUT_S)
            self.done.emit(True, f"delivered to uid={self._target_uid} "
                                 f"service={self._service}")
        except dbus.DBusException as e:
            self.done.emit(False, f"{e.get_dbus_name()}: {e.get_dbus_message()}")
        except Exception as e:  # noqa: BLE001
            self.done.emit(False, f"{type(e).__name__}: {e}")


class Sender(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("qstub-sender")
        self._btn = QPushButton("Send to…", self)
        self._btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._btn.customContextMenuRequested.connect(self._on_right_click)
        # Left-click also opens the menu so the app is discoverable
        # without needing to right-click, which matters when driving
        # via xdotool where right-click on XWayland under labwc is
        # flakier than left-click.
        self._btn.clicked.connect(
            lambda: self._open_menu(self._btn.rect().bottomLeft()))
        self.setCentralWidget(self._btn)
        self.resize(380, 120)
        self._worker: RelayWorker | None = None
        self._username = _current_username()

    def _on_right_click(self, pos) -> None:
        self._open_menu(pos)

    def _open_menu(self, pos) -> None:
        menu = QMenu(self)
        try:
            receivers = _list_receivers()
        except dbus.DBusException as e:
            QMessageBox.critical(self, "qstub-sender",
                                 f"ListReceivers failed:\n{e}")
            return
        # Drop ourselves from the list if a same-name receiver happens
        # to be running (the prefix matcher in UserRelay catches them).
        me_uid = os.geteuid()
        visible = [r for r in receivers
                   if not (r[0] == me_uid and r[1].startswith(
                       "com.qdistro.StubSender"))]
        if not visible:
            a = QAction("(no receivers running)", menu)
            a.setEnabled(False)
            menu.addAction(a)
        else:
            for uid, service, friendly in visible:
                label = f"uid={uid}: {friendly}  [{service}]"
                act = QAction(label, menu)
                act.triggered.connect(
                    lambda _checked=False, u=uid, s=service: self._send(u, s))
                menu.addAction(act)
        menu.exec(self._btn.mapToGlobal(pos))

    def _send(self, target_uid: int, service: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "qstub-sender",
                                    "already sending; wait for the previous "
                                    "request to finish")
            return
        payload = f"hello from {self._username}"
        self._btn.setEnabled(False)
        self._btn.setText(f"sending to {service}…")
        self._worker = RelayWorker(target_uid, service, "text/plain", payload)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, message: str) -> None:
        self._btn.setEnabled(True)
        self._btn.setText("Send to…")
        if ok:
            QMessageBox.information(self, "qstub-sender", message)
        else:
            QMessageBox.warning(self, "qstub-sender", f"failed: {message}")


def _current_username() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return f"uid{os.geteuid()}"


def _list_receivers() -> list[tuple[int, str, str]]:
    bus = dbus.SystemBus()
    proxy = bus.get_object(BROKER_BUS, BROKER_PATH)
    rows = proxy.ListReceivers(dbus_interface=BROKER_IFACE)
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    app = QApplication(sys.argv)
    window = Sender()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
