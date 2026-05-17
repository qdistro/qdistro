#!/usr/bin/env python3
"""qstub-notepad — Phase 3 stub receiver.

Claims a unique session-bus name of the form
`org.qdistro.StubNotepad.uid<N>`, exposes org.qdistro.App1.Receive,
and appends every received payload to a QPlainTextEdit. Also exposes
a GetDocument method so tests can assert the final document state
without driving the UI.

Phase 3 keeps this deliberately thin — one window, no right-click
send-back menu, no scope escalation. Phase 4 adds the rest of the
App1 surface (readonly/viewer/tier/handoff) plus a real editor.

Runs in either `QT_QPA_PLATFORM=offscreen` (for headless smoke
tests) or a real display. Same code path.
"""
from __future__ import annotations

import os
import sys

import dbus
import dbus.service
import dbus.mainloop.glib

# dbus-python's GLib mainloop is the simplest way to dispatch D-Bus
# methods; Qt's own event loop co-exists because dbus.mainloop.glib
# runs GLib inside the process and both wake on fds via the Qt
# platform plugin's internal integration. For Phase 3 the D-Bus
# thread touches Qt state only via QTimer.singleShot(0, ...).
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QPlainTextEdit

APP1_IFACE = "org.qdistro.App1"
APP1_OBJ_PATH = "/org/qdistro/App1"


class NotepadReceiver(dbus.service.Object):

    def __init__(self, bus, window: "Notepad"):
        super().__init__(bus, APP1_OBJ_PATH)
        self._window = window

    @dbus.service.method(APP1_IFACE, in_signature="ss", out_signature="")
    def Receive(self, kind: str, payload: str) -> None:
        # Hop to the Qt main thread. Touching QPlainTextEdit from the
        # GLib thread would be a data race and (on some platforms)
        # segfault.
        text = f"[{kind}] {payload}\n"
        QTimer.singleShot(0, lambda: self._window.append_text(text))

    @dbus.service.method(APP1_IFACE, in_signature="", out_signature="s")
    def GetDocument(self) -> str:
        # Used by headless GUI scenarios to assert delivery without
        # screen-scraping. Reading Qt state from the GLib thread is
        # technically racy, but toPlainText is read-only and Python
        # strings are immutable — good enough for tests.
        return self._window.document_text()


class Notepad(QMainWindow):
    def __init__(self, service_name: str):
        super().__init__()
        self.setWindowTitle(f"qstub-notepad ({service_name})")
        self._edit = QPlainTextEdit(self)
        self.setCentralWidget(self._edit)
        self.resize(600, 400)

    def append_text(self, text: str) -> None:
        self._edit.appendPlainText(text.rstrip("\n"))

    def document_text(self) -> str:
        return self._edit.toPlainText()


def main() -> int:
    uid = os.geteuid()
    service_name = os.environ.get("QSTUB_NOTEPAD_SERVICE",
                                   f"org.qdistro.StubNotepad.uid{uid}")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    app = QApplication(sys.argv)
    bus = dbus.SessionBus()
    try:
        # Keep a reference — dbus-python releases the name as soon as
        # the BusName object is garbage-collected.
        bus_name = dbus.service.BusName(service_name, bus, do_not_queue=True)
    except dbus.DBusException as e:
        print(f"[qstub-notepad] could not claim {service_name}: {e}",
              file=sys.stderr, flush=True)
        return 1
    window = Notepad(service_name)
    receiver = NotepadReceiver(bus, window)
    receiver._anchor_bus_name = bus_name  # noqa: SLF001
    print(f"[qstub-notepad] uid={uid} serving {service_name}", flush=True)

    # In offscreen mode we still want the window "shown" so Qt builds
    # the widget tree; it just doesn't composite anywhere.
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
