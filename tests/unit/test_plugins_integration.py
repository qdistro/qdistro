"""In-VM integration tests for the Phase-4 send-to plugins.

Sibling file to `test_plugins_sendto.py` (structural smoke tests).
These go a step further: mock the bus + a fake window, then drive
the plugin's internal callbacks and assert the *actual* delivery
and send paths worked.

Runs wherever PyQt6 + dbus are importable (inside the Phase-1 VM;
the repo's test venv on the host does not ship PyQt6, so the
whole file `importorskip`s there). The plugin modules are loaded
by absolute path the same way they are at runtime in qterminator
/ qnotebook discovery, so import-path regressions surface here
too.

What we cover:

- **qnotebook plugin** — `_deliver` inserts into the editor cursor
  and does NOT auto-save (qdoc→markdown round-trip is fragile);
  `_on_receive` marshals through `QTimer.singleShot` (we flush
  the timer synchronously via `QApplication.processEvents`);
  `_open_send_menu` calls `list_receivers` and builds a QMenu;
  `_RelayWorker` invokes `qdistro_app.send_to` with the right
  typed args; `setup()` gracefully no-ops when `_QDISTRO_OK` is
  False.

- **qterminator plugin** — `_deliver` calls `send_text` on the
  active terminal; `_deliver` is a silent no-op when no active
  terminal is set; `get_menu_items` returns one entry; the
  RelayWorker path mirrors qnotebook's.

What we don't cover here (belongs in gui-tests/):
- Actual dbus bus-name claiming (the SDK's `AppReceiver.__init__`
  is monkey-patched to skip the real BusName claim; we test the
  claiming surface in `test_sdk_sendto.py::TestAppReceiver`).
- Live admin approval flow (scenario 16/17).
- xhost + cross-uid GUI display (scenario 16).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("dbus")


_ROOT = Path(__file__).resolve().parents[2]
_SDK = _ROOT / "sdk"
_PLUGINS = _ROOT / "plugins"
sys.path.insert(0, str(_SDK))


# --- fake infrastructure ---------------------------------------------------

def _stub_qterminator_plugin_module():
    """Same stub as test_plugins_sendto.py — avoids a real qterminator
    install when loading the qterminator plugin file by path."""
    if "qterminator.plugin" in sys.modules:
        return
    pkg = types.ModuleType("qterminator")
    mod = types.ModuleType("qterminator.plugin")

    class Plugin:
        name = "unnamed"
        description = ""
        version = "0.0"
        capabilities: list[str] = []

        def activate(self, app_controller):
            pass

        def deactivate(self):
            pass

    class MenuProvider(Plugin):
        capabilities = ["menu_provider"]
        category = "Plugins"

        def get_menu_items(self, terminal):
            return []

    mod.Plugin = Plugin
    mod.MenuProvider = MenuProvider
    pkg.plugin = mod
    sys.modules["qterminator"] = pkg
    sys.modules["qterminator.plugin"] = mod


def _load_plugin(path: Path, name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _qapp():
    """Offscreen QApplication so QTextEdit / QTimer / QMessageBox
    classes can be constructed."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def fake_broker(monkeypatch):
    """Monkey-patch the SDK's SystemBus so list_receivers / send_to
    call into an in-memory fake and we can assert dispatches."""
    import qdistro_app

    class _Proxy:
        def __init__(self):
            self.calls: list[tuple[str, tuple, dict]] = []
            self.list_receivers_response = []
            self.relay_message_effect = None  # or a callable / exception

        def _rec(self, name, *a, **kw):
            self.calls.append((name, a, kw))

        def ListReceivers(self, *a, **kw):
            self._rec("ListReceivers", *a, **kw)
            return self.list_receivers_response

        def RelayMessage(self, *a, **kw):
            self._rec("RelayMessage", *a, **kw)
            if callable(self.relay_message_effect):
                self.relay_message_effect()
            elif isinstance(self.relay_message_effect, BaseException):
                raise self.relay_message_effect
            return None

    class _Bus:
        def __init__(self, proxy):
            self._proxy = proxy

        def get_object(self, name, path):
            return self._proxy

    proxy = _Proxy()
    monkeypatch.setattr(qdistro_app.dbus, "SystemBus",
                        lambda: _Bus(proxy))
    return proxy


@pytest.fixture
def fake_receiver(monkeypatch):
    """Monkey-patch AppReceiver so it doesn't try to claim a real bus
    name. The plugin cares about `service_name` (for its "drop self
    from receivers list" filter) and that `on_receive` is invokable —
    nothing more at this layer."""
    import qdistro_app

    class _FakeAppReceiver:
        def __init__(self, service_name, on_receive, bus=None):
            self.service_name = str(service_name)
            self._on_receive = on_receive
            self._last_received = None

        def Receive(self, kind, payload):
            self._last_received = (str(kind), str(payload))
            self._on_receive(str(kind), str(payload))

    monkeypatch.setattr(qdistro_app, "AppReceiver", _FakeAppReceiver)
    return _FakeAppReceiver


def _flush_qt():
    """Run pending queued Qt events — QTimer.singleShot(0, fn) handoffs
    complete after this returns. Needed because the plugin's
    `_on_receive` schedules delivery via singleShot and tests run
    outside an `app.exec()` loop."""
    from PyQt6.QtCore import QCoreApplication
    for _ in range(5):
        QCoreApplication.processEvents()


# --- qnotebook plugin -----------------------------------------------------

def _load_qnotebook_plugin():
    return _load_plugin(_PLUGINS / "qdistro_sendto_qnotebook.py",
                        "qdistro_sendto_qnotebook_int")


class _FakeQnotebookEditor:
    """Minimal stand-in for zim_qt.editor.MarkdownEditor: exposes the
    QTextEdit surface the plugin touches (textCursor, setTextCursor,
    toPlainText, insertPlainText via cursor.insertText)."""
    def __init__(self, qapp):
        from PyQt6.QtWidgets import QTextEdit
        self._edit = QTextEdit()

    # Delegate the few methods the plugin actually calls.
    def textCursor(self):  # noqa: N802
        return self._edit.textCursor()

    def setTextCursor(self, c):  # noqa: N802
        self._edit.setTextCursor(c)

    def toPlainText(self) -> str:  # noqa: N802
        return self._edit.toPlainText()


def _make_fake_qnotebook_window(qapp):
    """qnotebook's window is a QMainWindow in production; the plugin's
    send-menu path calls `QMenu(self._window)` so the "window" object
    must actually inherit from QWidget. Build a QMainWindow with the
    few attrs the plugin reads (editor, m_plugins, _current_page)
    stashed on it."""
    from PyQt6.QtWidgets import QMainWindow
    win = QMainWindow()
    win.editor = _FakeQnotebookEditor(qapp)
    win.m_plugins = None
    win._current_page = None
    win._saved = 0
    def _save_current(_self=win):
        _self._saved += 1
    win._save_current = _save_current
    return win


class TestQnotebookPluginReceive:
    def test_deliver_appends_payload_at_end_of_document(
            self, fake_receiver, _qapp):
        mod = _load_qnotebook_plugin()
        plugin = mod.Plugin()
        win = _make_fake_qnotebook_window(_qapp)
        plugin.setup(win)
        # on_receive → QTimer.singleShot → _deliver, all on the main
        # thread since the test has no separate GLib loop.
        plugin._on_receive("text/plain", "hello-world")
        _flush_qt()
        assert "hello-world" in win.editor.toPlainText()

    def test_deliver_missing_editor_no_crash(
            self, fake_receiver, _qapp):
        mod = _load_qnotebook_plugin()
        plugin = mod.Plugin()
        class _W: pass
        win = _W()
        plugin.setup(win)  # no editor attr; setup still runs
        # on_receive must NOT raise even without an editor. Probe
        # state captured in plugin module path.
        plugin._on_receive("text/plain", "ignored")
        _flush_qt()
        # No assertion on content — just that we didn't blow up.

    def test_deliver_does_not_autosave(
            self, fake_receiver, _qapp):
        # Autosave through qnotebook's _save_current round-trips
        # through qdoc_to_markdown which can mangle raw appended
        # text. The plugin must NOT call _save_current — tests
        # assert this is the case by observing the counter.
        mod = _load_qnotebook_plugin()
        plugin = mod.Plugin()
        win = _make_fake_qnotebook_window(_qapp)
        win._current_page = "/fake/page.md"
        plugin.setup(win)
        plugin._on_receive("text/plain", "payload")
        _flush_qt()
        assert win._saved == 0


class TestQnotebookPluginSend:
    def test_open_send_menu_no_selection_early_returns(
            self, fake_receiver, fake_broker, _qapp, monkeypatch):
        # Selection is empty (default) → the plugin shows an info
        # QMessageBox and does NOT hit the broker. We stub the
        # QMessageBox.information to avoid an event-loop call.
        mod = _load_qnotebook_plugin()
        plugin = mod.Plugin()
        win = _make_fake_qnotebook_window(_qapp)
        plugin.setup(win)

        shown: list[str] = []
        monkeypatch.setattr(mod.QMessageBox, "information",
                            lambda *a, **k: shown.append(a[2] if len(a) > 2 else ""))
        plugin._open_send_menu()
        assert fake_broker.calls == []  # broker never reached
        assert shown  # user was told

    def test_open_send_menu_lists_receivers(
            self, fake_receiver, fake_broker, _qapp, monkeypatch):
        mod = _load_qnotebook_plugin()
        plugin = mod.Plugin()
        win = _make_fake_qnotebook_window(_qapp)
        # Give the editor a selection.
        from PyQt6.QtGui import QTextCursor
        cur = win.editor.textCursor()
        cur.insertText("hello selection")
        cur.movePosition(QTextCursor.MoveOperation.Start,
                         QTextCursor.MoveMode.KeepAnchor,
                         n=len("hello selection"))
        win.editor.setTextCursor(cur)
        plugin.setup(win)

        fake_broker.list_receivers_response = [
            (2000, "org.qdistro.Qterminator.uid2000", "Qterminator"),
            (3000, "org.qdistro.Qnotebook.uid3000", "Qnotebook"),
        ]

        # Stub QMenu.exec so the menu doesn't try to enter a nested
        # event loop; we just assert the menu's actions.
        captured: dict = {}
        original_exec = mod.QMenu.exec
        def _fake_exec(self, *a, **k):
            captured["actions"] = [a.text() for a in self.actions()]
        monkeypatch.setattr(mod.QMenu, "exec", _fake_exec)

        plugin._open_send_menu()
        assert ("ListReceivers", (), {"dbus_interface": "org.qdistro.AdminBroker1"}) \
            in fake_broker.calls
        assert any("Qterminator" in a for a in captured.get("actions", []))
        assert any("Qnotebook" in a for a in captured.get("actions", []))

    def test_open_send_menu_excludes_self(
            self, fake_receiver, fake_broker, _qapp, monkeypatch):
        mod = _load_qnotebook_plugin()
        plugin = mod.Plugin()
        win = _make_fake_qnotebook_window(_qapp)
        from PyQt6.QtGui import QTextCursor
        cur = win.editor.textCursor()
        cur.insertText("selection-text")
        cur.movePosition(QTextCursor.MoveOperation.Start,
                         QTextCursor.MoveMode.KeepAnchor,
                         n=len("selection-text"))
        win.editor.setTextCursor(cur)
        plugin.setup(win)
        my_svc = plugin._receiver.service_name
        # List contains self + a peer; we should only see peer.
        fake_broker.list_receivers_response = [
            (3000, my_svc, "Qnotebook"),  # self
            (2000, "org.qdistro.Qterminator.uid2000", "Qterminator"),
        ]
        captured: dict = {}
        monkeypatch.setattr(mod.QMenu, "exec",
                            lambda self, *a, **k: captured.setdefault(
                                "actions", [x.text() for x in self.actions()]))
        plugin._open_send_menu()
        actions = captured.get("actions", [])
        assert any("Qterminator" in a for a in actions)
        assert not any(my_svc in a for a in actions)


class TestQnotebookPluginRelayWorker:
    def test_relay_worker_calls_send_to(self, fake_broker):
        mod = _load_qnotebook_plugin()
        import qdistro_app
        w = mod._RelayWorker(3000, "org.qdistro.Qnotebook.uid3000",
                             "text/plain", "payload")
        w.run()  # sync: don't spin Qt thread machinery
        # The SDK's send_to dispatches a RelayMessage on the system bus.
        names = [c[0] for c in fake_broker.calls]
        assert "RelayMessage" in names
        # Args: target_uid, service, kind, payload.
        _name, args, _ = [c for c in fake_broker.calls
                          if c[0] == "RelayMessage"][0]
        assert int(args[0]) == 3000
        assert str(args[1]) == "org.qdistro.Qnotebook.uid3000"
        assert str(args[2]) == "text/plain"
        assert str(args[3]) == "payload"

    def test_relay_worker_emits_failure_on_dbus_error(self, fake_broker):
        mod = _load_qnotebook_plugin()
        import dbus
        fake_broker.relay_message_effect = dbus.DBusException(
            "denied", name="org.qdistro.AdminBroker1.Denied")
        w = mod._RelayWorker(3000, "org.qdistro.Qnotebook.uid3000",
                             "text/plain", "payload")
        results: list[tuple[bool, str]] = []
        w.done.connect(lambda ok, msg: results.append((ok, msg)))
        w.run()
        assert results and results[0][0] is False
        assert "Denied" in results[0][1]


class TestQnotebookPluginDegradation:
    def test_setup_with_qdistro_missing_is_noop(self, monkeypatch):
        mod = _load_qnotebook_plugin()
        monkeypatch.setattr(mod, "_QDISTRO_OK", False)
        monkeypatch.setattr(mod, "_IMPORT_ERR", "simulated")
        plugin = mod.Plugin()
        class _W: pass
        plugin.setup(_W())
        assert plugin._receiver is None


# --- qterminator plugin ----------------------------------------------------

def _load_qterminator_plugin():
    _stub_qterminator_plugin_module()
    return _load_plugin(_PLUGINS / "qdistro_sendto_qterminator.py",
                        "qdistro_sendto_qterminator_int")


class _FakeTerminal:
    def __init__(self):
        self.sent: list[str] = []

    def send_text(self, text: str) -> None:
        self.sent.append(str(text))

    def selected_text(self) -> str:
        return self._selection

    _selection = ""


def _make_fake_terminator_window():
    """qterminator's window is a QMainWindow in production. The plugin
    calls QMenu(self._window) so the "window" must be a real QWidget.
    Stash _active_terminal on it."""
    from PyQt6.QtWidgets import QMainWindow
    win = QMainWindow()
    win._active_terminal = _FakeTerminal()
    return win


class TestQterminatorPluginReceive:
    def test_deliver_calls_send_text(self, fake_receiver, _qapp):
        mod = _load_qterminator_plugin()
        plugin = mod.QdistroSendToPlugin()
        win = _make_fake_terminator_window()
        plugin.activate(win)
        plugin._on_receive("text/plain", "ls -la\n")
        _flush_qt()
        assert win._active_terminal.sent == ["ls -la\n"]

    def test_deliver_no_active_terminal_silent(
            self, fake_receiver, _qapp):
        mod = _load_qterminator_plugin()
        plugin = mod.QdistroSendToPlugin()
        class _Empty:
            _active_terminal = None
        plugin.activate(_Empty())
        plugin._on_receive("text/plain", "ignored")
        _flush_qt()
        # No crash; nothing to assert on the window side.


class TestQterminatorPluginMenu:
    def test_get_menu_items_returns_one_entry(
            self, fake_receiver, _qapp):
        mod = _load_qterminator_plugin()
        plugin = mod.QdistroSendToPlugin()
        plugin.activate(_make_fake_terminator_window())
        items = plugin.get_menu_items(terminal=_FakeTerminal())
        assert len(items) == 1
        label, cb = items[0]
        assert "Send" in label
        assert callable(cb)

    def test_open_send_menu_no_selection_shows_info(
            self, fake_receiver, fake_broker, _qapp, monkeypatch):
        mod = _load_qterminator_plugin()
        plugin = mod.QdistroSendToPlugin()
        plugin.activate(_make_fake_terminator_window())
        shown: list[str] = []
        monkeypatch.setattr(mod.QMessageBox, "information",
                            lambda *a, **k: shown.append("x"))
        term = _FakeTerminal()
        term._selection = ""
        plugin._open_send_menu(term)
        assert fake_broker.calls == []
        assert shown

    def test_open_send_menu_with_selection_lists_receivers(
            self, fake_receiver, fake_broker, _qapp, monkeypatch):
        mod = _load_qterminator_plugin()
        plugin = mod.QdistroSendToPlugin()
        plugin.activate(_make_fake_terminator_window())
        fake_broker.list_receivers_response = [
            (3000, "org.qdistro.Qnotebook.uid3000", "Qnotebook"),
        ]
        captured: dict = {}
        monkeypatch.setattr(mod.QMenu, "exec",
                            lambda self, *a, **k: captured.setdefault(
                                "actions", [x.text() for x in self.actions()]))
        term = _FakeTerminal()
        term._selection = "some selected text"
        plugin._open_send_menu(term)
        assert ("ListReceivers", (),
                {"dbus_interface": "org.qdistro.AdminBroker1"}) \
            in fake_broker.calls
        assert any("Qnotebook" in a for a in captured.get("actions", []))


class TestQterminatorPluginRelayWorker:
    def test_relay_worker_calls_send_to(self, fake_broker):
        mod = _load_qterminator_plugin()
        w = mod._RelayWorker(3000, "org.qdistro.Qnotebook.uid3000",
                             "text/plain", "payload")
        w.run()
        assert any(c[0] == "RelayMessage" for c in fake_broker.calls)
