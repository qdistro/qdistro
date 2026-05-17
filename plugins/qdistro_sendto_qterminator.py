"""qdistro send-to plugin for qterminator.

Dropped into qterminator's plugin discovery path (either
`qterminator/plugins/` bundled or `~/.config/qterminator/plugins/`
per-user). Hooks the running qterminator into qdistro's Phase-3
send-to protocol *without* modifying qterminator itself.

Two responsibilities:

1. **Receiver**: claims `org.qdistro.Qterminator.uid<N>` on the
   session bus and exposes `org.qdistro.App1.Receive(kind, payload)`.
   Incoming payloads are injected into the active terminal's PTY
   via `terminal.send_text()`. The SDK's `AppReceiver` also exposes
   `GetLastReceived()` so headless tests can assert delivery without
   screen-scraping the terminal.

2. **Sender**: adds a "Send selection to…" entry to the terminal
   context menu (via qterminator's `MenuProvider` API, category
   "Plugins"). Clicking it opens a submenu listing every running
   receiver discovered via `qdistro_app.list_receivers()`; picking
   one fires `qdistro_app.send_to()` on a QThread so the admin
   approval wait doesn't freeze the UI.

Lives in `plugins/` in the repo; bootstrap-vm.sh
installs it to `/usr/local/lib/qdistro/plugins/` and symlinks it
into each sending uid's qterminator plugin directory.
"""
from __future__ import annotations

import os
import sys

# qdistro_app is installed to the system site-packages by bootstrap;
# ImportError means the plugin is running outside a qdistro install,
# in which case we degrade to a no-op.
try:
    import dbus
    import dbus.mainloop.glib
    import qdistro_app
    _QDISTRO_OK = True
    _IMPORT_ERR: str | None = None
except Exception as e:  # pragma: no cover - defensive
    _QDISTRO_OK = False
    _IMPORT_ERR = f"{type(e).__name__}: {e}"

from PyQt6.QtCore import QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QCursor
from PyQt6.QtWidgets import QMenu, QMessageBox

from qterminator.plugin import MenuProvider


SERVICE_FMT = "org.qdistro.Qterminator.uid{uid}"

# Match qstub-sender: 120 s is long enough for a human admin to pick
# a scope and click, short enough that a forgotten prompt doesn't
# freeze a background QThread forever.
SEND_TIMEOUT_S = 120.0


class _RelayWorker(QThread):
    """Runs qdistro_app.send_to off the GUI thread."""
    done = pyqtSignal(bool, str)

    def __init__(self, target_uid: int, target_service: str,
                 kind: str, payload: str):
        super().__init__()
        self._target_uid = target_uid
        self._target_service = target_service
        self._kind = kind
        self._payload = payload

    def run(self) -> None:
        try:
            qdistro_app.send_to(
                self._target_uid, self._target_service,
                self._kind, self._payload,
                timeout=SEND_TIMEOUT_S)
            self.done.emit(True, f"delivered to uid={self._target_uid} "
                                 f"({self._target_service})")
        except Exception as e:  # noqa: BLE001
            name = getattr(e, "get_dbus_name", lambda: type(e).__name__)()
            msg = getattr(e, "get_dbus_message", lambda: str(e))()
            self.done.emit(False, f"{name}: {msg}")


class QdistroSendToPlugin(MenuProvider):
    """Single plugin class — MenuProvider is a Plugin subclass, so one
    class gives us both `activate()` (called once at startup) and
    `get_menu_items()` (called on every context-menu open).
    qterminator's PluginManager instantiates every concrete Plugin
    subclass in the module and enable() calls activate on the first
    instance found."""

    name = "qdistro_sendto"
    description = "Cross-user send-to via qdistro admin broker"
    version = "0.1"
    category = "Plugins"

    def __init__(self):
        super().__init__()
        self._window = None
        self._receiver: "qdistro_app.AppReceiver | None" = None
        self._workers: list[_RelayWorker] = []  # keep alive

    # ------------------------------------------------------------------
    # Plugin lifecycle
    # ------------------------------------------------------------------

    def activate(self, app_controller):
        # qterminator passes `self` (the MainWindow) as app_controller.
        self._window = app_controller
        if not _QDISTRO_OK:
            print(f"[qdistro_sendto] disabled: {_IMPORT_ERR}",
                  file=sys.stderr, flush=True)
            return
        # Attach dbus-python to the default GLib context. Must happen
        # before the first bus() call in this process; qterminator's
        # own code doesn't touch dbus, so we're first.
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        try:
            uid = os.geteuid()
            service = SERVICE_FMT.format(uid=uid)
            self._receiver = qdistro_app.AppReceiver(
                service_name=service,
                on_receive=self._on_receive,
            )
            print(f"[qdistro_sendto] claimed {service}",
                  file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            self._receiver = None
            print(f"[qdistro_sendto] receiver-setup failed: {e}",
                  file=sys.stderr, flush=True)

    def deactivate(self):
        # Dropping the reference releases the BusName on GC; see
        # memory busname_anchor.md for the failure mode when this is
        # accidental.
        self._receiver = None

    # ------------------------------------------------------------------
    # Receiver side
    # ------------------------------------------------------------------

    def _on_receive(self, kind: str, payload: str) -> None:
        # Runs on the GLib thread; hop to Qt before touching the
        # widget tree. QTimer.singleShot(0, ...) is the canonical
        # cross-thread handoff in dbus-python+PyQt integrations and
        # is the same pattern Phase-3 stubs use.
        QTimer.singleShot(0, lambda: self._deliver(kind, payload))

    def _deliver(self, kind: str, payload: str) -> None:
        term = getattr(self._window, "_active_terminal", None)
        if term is None:
            # No active terminal — the window exists but no tab yet.
            # Drop silently; GetLastReceived still reflects what
            # landed on the bus, so tests can still assert delivery.
            return
        # Strict "just the bytes" injection. We do NOT append a \n;
        # send-to is a delivery mechanism, not a "press enter for me"
        # mechanism. If the sender wanted a newline they included one.
        try:
            term.send_text(str(payload))
        except Exception as e:  # noqa: BLE001
            print(f"[qdistro_sendto] send_text failed: {e}",
                  file=sys.stderr, flush=True)

    # ------------------------------------------------------------------
    # MenuProvider API — the Sender side
    # ------------------------------------------------------------------

    def get_menu_items(self, terminal):
        """Return one entry that opens the Send-to submenu on click."""
        if not _QDISTRO_OK:
            return []
        return [("Send selection to…",
                 lambda t=terminal: self._open_send_menu(t))]

    def _open_send_menu(self, terminal) -> None:
        try:
            selection = terminal.selected_text() or ""
        except Exception:  # noqa: BLE001
            selection = ""
        if not selection.strip():
            QMessageBox.information(
                self._window, "qdistro send-to",
                "Nothing selected. Select text in the terminal first.")
            return
        try:
            receivers = qdistro_app.list_receivers()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self._window, "qdistro send-to",
                f"ListReceivers failed:\n{e}")
            return
        # Drop ourselves so we don't offer self-send.
        my_service = (self._receiver.service_name
                      if self._receiver else None)
        visible = [r for r in receivers if r[1] != my_service]
        menu = QMenu(self._window)
        if not visible:
            a = QAction("(no receivers running)", menu)
            a.setEnabled(False)
            menu.addAction(a)
        else:
            for uid, service, friendly in visible:
                label = f"uid={uid}: {friendly}"
                act = QAction(label, menu)
                act.triggered.connect(
                    lambda _checked=False, u=uid, s=service, p=selection:
                        self._send(u, s, p))
                menu.addAction(act)
        menu.exec(QCursor.pos())

    def _send(self, target_uid: int, target_service: str,
              payload: str) -> None:
        worker = _RelayWorker(target_uid, target_service,
                              "text/plain", payload)
        worker.done.connect(self._on_send_done)
        # Stash so the QThread isn't GC'd mid-run.
        self._workers.append(worker)
        worker.start()

    def _on_send_done(self, ok: bool, message: str) -> None:
        # Drop finished workers; done signal fires just before run()
        # returns so isRunning() is False by the next UI tick.
        self._workers = [w for w in self._workers if w.isRunning()]
        if ok:
            QMessageBox.information(self._window, "qdistro send-to", message)
        else:
            QMessageBox.warning(self._window, "qdistro send-to",
                                 f"send failed: {message}")
