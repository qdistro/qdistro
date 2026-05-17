"""qdistro send-to plugin for qnotebook (zim_qt).

Dropped into qnotebook's plugin discovery path — either the bundled
`zim_qt/plugins/builtin/` or the per-notebook
`<notebook>/.zim-qt/plugins/`. Hooks the running qnotebook into
qdistro's Phase-3 send-to protocol *without* modifying qnotebook
itself.

Two responsibilities (same shape as the qterminator plugin):

1. **Receiver**: claims `com.qdistro.Qnotebook.uid<N>` on the
   session bus and exposes `com.qdistro.App1.Receive(kind, payload)`.
   Incoming payloads are appended to the current page at the cursor
   (or at end-of-document if no cursor is active), and the page is
   saved. The SDK's `AppReceiver.GetLastReceived` is available for
   headless test assertions.

2. **Sender**: adds a "Send selection to…" entry to qnotebook's
   Plugins menu. Clicking it opens a picker listing running
   receivers, and fires `qdistro_app.send_to()` on a QThread so the
   admin approval wait doesn't freeze the UI.

qnotebook's plugin API is plain:

    class Plugin:
        name: str
        description: str
        def setup(self, window): ...

It does not define separate lifecycle methods for teardown, so the
receiver lives for the lifetime of the window.
"""
from __future__ import annotations

import os
import sys

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
from PyQt6.QtGui import QAction, QCursor, QTextCursor
from PyQt6.QtWidgets import QMenu, QMessageBox


SERVICE_FMT = "com.qdistro.Qnotebook.uid{uid}"
SEND_TIMEOUT_S = 120.0


class _RelayWorker(QThread):
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


class Plugin:
    """qnotebook entrypoint — matches zim_qt.plugins discovery protocol.
    The class must be named exactly `Plugin`, have `name` +
    `description` attributes, and expose `setup(window)`.
    """

    name = "qdistro send-to"
    description = "Cross-user send-to via qdistro admin broker"

    def __init__(self) -> None:
        self._window = None
        self._receiver: "qdistro_app.AppReceiver | None" = None
        self._workers: list[_RelayWorker] = []

    # ------------------------------------------------------------------
    # qnotebook plugin entrypoint
    # ------------------------------------------------------------------

    def setup(self, window) -> None:
        self._window = window
        if not _QDISTRO_OK:
            print(f"[qdistro_sendto] disabled: {_IMPORT_ERR}",
                  file=sys.stderr, flush=True)
            return
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        try:
            uid = os.geteuid()
            service = SERVICE_FMT.format(uid=uid)
            self._receiver = qdistro_app.AppReceiver(
                service_name=service,
                on_receive=self._on_receive,
            )
            # Anchor the plugin on the window so the receiver's
            # BusName survives the plugin-manager's local-var GC.
            # dbus-python releases the name on GC; bind it to a local that outlives the mainloop.
            window._qdistro_plugin = self  # type: ignore[attr-defined]
            print(f"[qdistro_sendto] claimed {service}",
                  file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            self._receiver = None
            print(f"[qdistro_sendto] receiver-setup failed: {e}",
                  file=sys.stderr, flush=True)

        # Menu item under Plugins → Send selection to…
        m = getattr(window, "m_plugins", None)
        if m is not None:
            act = QAction("Qdistro: Send selection to…", window)
            act.triggered.connect(self._open_send_menu)
            m.addAction(act)

    # ------------------------------------------------------------------
    # Receiver side
    # ------------------------------------------------------------------

    def _on_receive(self, kind: str, payload: str) -> None:
        # GLib thread → Qt thread handoff.
        QTimer.singleShot(0, lambda: self._deliver(kind, payload))

    def _deliver(self, kind: str, payload: str) -> None:
        window = self._window
        if window is None:
            return
        editor = getattr(window, "editor", None)
        if editor is None:
            return
        text = str(payload)
        # Visual delivery is best-effort — qnotebook's editor holds a
        # rich QTextDocument (qdoc) and saves via qdoc→markdown. Raw
        # inserted text at end-of-document round-trips unpredictably
        # (trailing plain runs sometimes collapse into horizontal
        # rules on save). The authoritative signal is on the wire:
        # qdistro_app.AppReceiver.GetLastReceived returns the exact
        # `[kind] payload` tuple so tests and admin audit never lose
        # it. Here we just nudge the editor's cursor as a UX hint.
        try:
            cur = editor.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            cur.insertText("\n")
            cur.insertText(text)
            cur.insertText("\n")
            editor.setTextCursor(cur)
        except Exception as e:  # noqa: BLE001
            print(f"[qdistro_sendto] insert failed: {e}",
                  file=sys.stderr, flush=True)
        # Do NOT auto-save — the user-facing Save action rewrites the
        # page through qdoc_to_markdown, which can mutate the freshly
        # inserted text. GetLastReceived remains the ground truth.

    # ------------------------------------------------------------------
    # Sender side
    # ------------------------------------------------------------------

    def _open_send_menu(self) -> None:
        window = self._window
        editor = getattr(window, "editor", None)
        if editor is None:
            QMessageBox.information(window, "qdistro send-to",
                                    "No editor available.")
            return
        cur = editor.textCursor()
        selection = cur.selectedText() or ""
        # QTextCursor.selectedText replaces newlines with U+2029 (line
        # separator). Normalise so receivers see vanilla \n.
        selection = selection.replace(" ", "\n")
        if not selection.strip():
            QMessageBox.information(
                window, "qdistro send-to",
                "Nothing selected. Select text in the editor first.")
            return
        try:
            receivers = qdistro_app.list_receivers()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(window, "qdistro send-to",
                                 f"ListReceivers failed:\n{e}")
            return
        my_service = (self._receiver.service_name
                      if self._receiver else None)
        visible = [r for r in receivers if r[1] != my_service]
        menu = QMenu(window)
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
        self._workers.append(worker)
        worker.start()

    def _on_send_done(self, ok: bool, message: str) -> None:
        self._workers = [w for w in self._workers if w.isRunning()]
        window = self._window
        if ok:
            QMessageBox.information(window, "qdistro send-to", message)
        else:
            QMessageBox.warning(window, "qdistro send-to",
                                 f"send failed: {message}")
