"""Known receiver outcomes reach Qt without blocking or resending content."""
import threading
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidgetItem
from qfileman import qdistro_integration as qi
from qfileman.transfer_jobs import TransferSender
from qfileman.window import FileManagerWindow


def capabilities(**changes):
    return {"version": 1, "instance_id": "receiver-a", "kinds": ["text/plain"],
            "max_bytes": 256 * 1024, "encoding": "utf-8", "available": True,
            "confirmation_required": True, "reason": "", **changes}


def receipt(state, **changes):
    return {"version": 1, "instance_id": "receiver-a", "transfer_id": "private-handle",
            "state": state, "reason": "", **changes}


@pytest.fixture
def sender(qapp, tmp_path):
    window = FileManagerWindow()
    path = tmp_path / "text"
    path.write_text("selected text", encoding="utf-8")
    window.file_list.clear()
    item = QListWidgetItem("text", window.file_list)
    item.setData(Qt.ItemDataRole.UserRole, {"path": str(path)})
    item.setSelected(True)
    yield window, path
    window.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("state,message", [
    ("applied", "Text inserted into receiver editor; saving is separate."),
    ("declined", "Receiver declined the text; it was not inserted."),
    ("rejected", "Receiver rejected the text; it was not inserted."),
    ("failed", "Receiver failed to insert the text."),
    ("unknown", "Transfer outcome unknown; text may have arrived. Nothing was resent."),
])
def test_known_outcomes_reach_visible_status(sender, monkeypatch, qtbot, state, message):
    window, path = sender
    sent = []
    monkeypatch.setattr(qi, "get_transfer_capabilities", lambda *args: capabilities())
    monkeypatch.setattr(qi, "send_transfer", lambda *args, **kwargs: sent.append((args, kwargs)) or receipt(state))
    window._send_selected_text(1000, "org.qdistro.Notebook.uid1000", capabilities=capabilities())
    qtbot.waitUntil(lambda: not window._transfer_sender._busy)
    assert window.statusBar().currentMessage() == message
    assert sent == [((1000, "org.qdistro.Notebook.uid1000", "receiver-a", "text/plain", "selected text"), {"timeout": 60})]


def test_staged_poll_is_scoped_and_gui_stays_responsive(sender, monkeypatch, qtbot):
    window, path = sender
    entered, release = threading.Event(), threading.Event()
    sent, polls, messages = [], [], []
    window.statusBar().messageChanged.connect(messages.append)
    monkeypatch.setattr(TransferSender, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(qi, "get_transfer_capabilities", lambda *args: capabilities())
    def send(*args, **kwargs):
        sent.append(args)
        entered.set()
        assert release.wait(2)
        return receipt("staged")
    monkeypatch.setattr(qi, "send_transfer", send)
    monkeypatch.setattr(qi, "get_transfer_status", lambda *args, **kwargs: polls.append((args, kwargs)) or receipt("applied"))
    window._send_selected_text(1000, "org.qdistro.Notebook.uid1000", capabilities=capabilities())
    assert entered.wait(1)
    # Queued Qt event runs while the SDK call is still waiting on another thread.
    heartbeat = []
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    qtbot.waitUntil(lambda: heartbeat == [True])
    assert not release.is_set()
    release.set()
    qtbot.waitUntil(lambda: not window._transfer_sender._busy)
    assert "Text staged at receiver; awaiting confirmation." in messages
    assert window.statusBar().currentMessage() == "Text inserted into receiver editor; saving is separate."
    assert len(sent) == 1
    assert polls == [(("private-handle",), {"timeout": 3})]


@pytest.mark.parametrize("caps", [capabilities(available=False), capabilities(max_bytes=2),
    capabilities(instance_id="restarted"), {"version": 0}, capabilities(kinds=["text/markdown"])])
def test_dispatch_revalidates_capabilities_without_sending(sender, monkeypatch, qtbot, caps):
    window, path = sender
    sent = []
    monkeypatch.setattr(qi, "get_transfer_capabilities", lambda *args: caps)
    monkeypatch.setattr(qi, "send_transfer", lambda *args, **kwargs: sent.append(args))
    window._send_selected_text(1000, "org.qdistro.Notebook.uid1000", capabilities=capabilities())
    qtbot.waitUntil(lambda: not window._transfer_sender._busy)
    assert sent == []
    assert "unknown" in window.statusBar().currentMessage().lower() or "rejected" in window.statusBar().currentMessage().lower()


@pytest.mark.parametrize("failure", ["send_timeout", "poll_timeout", "restart", "waiting_limit"])
def test_unknown_outcomes_never_resend(sender, monkeypatch, qtbot, failure):
    window, path = sender
    sends = []
    monkeypatch.setattr(TransferSender, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(qi, "get_transfer_capabilities", lambda *args: capabilities())
    def send(*args, **kwargs):
        sends.append(args)
        if failure == "send_timeout":
            raise TimeoutError("reply late")
        return receipt("staged")
    def poll(*args, **kwargs):
        if failure == "poll_timeout":
            raise TimeoutError("query late")
        if failure == "restart":
            return receipt("applied", instance_id="restarted")
        return receipt("staged")
    if failure == "waiting_limit":
        monkeypatch.setattr(TransferSender, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(qi, "send_transfer", send)
    monkeypatch.setattr(qi, "get_transfer_status", poll)
    window._send_selected_text(1000, "org.qdistro.Notebook.uid1000", capabilities=capabilities())
    qtbot.waitUntil(lambda: not window._transfer_sender._busy)
    assert len(sends) == 1
    assert window.statusBar().currentMessage().startswith("Transfer outcome unknown; text may have arrived. Nothing was resent.")


def test_closed_window_and_stale_results_are_ignored(sender, monkeypatch, qtbot):
    window, path = sender
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    def send(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        ended.set()
        return receipt("applied")
    monkeypatch.setattr(qi, "get_transfer_capabilities", lambda *args: capabilities())
    monkeypatch.setattr(qi, "send_transfer", send)
    window._send_selected_text(1000, "org.qdistro.Notebook.uid1000", capabilities=capabilities())
    assert entered.wait(1)
    controller = window._transfer_sender
    messages = []
    controller.changed.connect(messages.append)
    controller.result.emit(controller._generation - 1, receipt("applied"), True)
    qtbot.waitUntil(lambda: controller._busy)
    assert messages == []
    window.close()
    release.set()
    assert ended.wait(1)
    qtbot.waitUntil(lambda: controller._closed)
    controller.result.emit(controller._generation, receipt("applied"), True)
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()
    assert messages == []


def test_menu_shows_capabilities_and_disables_full_receiver(sender, monkeypatch):
    window, path = sender
    monkeypatch.setattr(qi, "send_to_targets", lambda **kwargs: [
        {"name": "Notebook", "uid": 1000, "service": "org.qdistro.Notebook.uid1000",
         "capabilities": capabilities(available=False, reason="Inbox full")}])
    window._populate_send_to_menu()
    action = window._send_to_menu.actions()[0]
    assert action.text() == "Notebook (262144 bytes; confirmation required)"
    assert action.toolTip() == "UTF-8 text. Inbox full"
    assert not action.isEnabled()


def test_global_and_per_window_jobs_are_bounded(qapp, qtbot):
    entered, release = threading.Event(), threading.Event()
    def caps(*args):
        entered.set()
        assert release.wait(2)
        return capabilities(available=False)
    integration = SimpleNamespace(get_transfer_capabilities=caps)
    parents = [FileManagerWindow() for _ in range(3)]
    controllers = [TransferSender(parent, integration) for parent in parents]
    try:
        assert controllers[0].start(1000, "service", "receiver-a", "text")
        assert entered.wait(1)
        assert not controllers[0].start(1000, "service", "receiver-a", "text")
        assert controllers[1].start(1000, "service", "receiver-a", "text")
        assert not controllers[2].start(1000, "service", "receiver-a", "text")
        release.set()
        qtbot.waitUntil(lambda: not any(c._busy for c in controllers[:2]))
    finally:
        release.set()
        for controller, parent in zip(controllers, parents, strict=True):
            controller.close()
            parent.close()


def test_async_menu_enrichment_and_dispatch_use_current_content(sender, monkeypatch, qtbot):
    window, path = sender
    entered, release = threading.Event(), threading.Event()
    calls, sent = [], []
    monkeypatch.setattr(qi, "send_to_targets", lambda **kwargs: [
        {"name": "Notebook", "uid": 1000, "service": "org.qdistro.Notebook.uid1000", "capabilities": {"version": 0}}])
    def caps(*args):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return capabilities()
    monkeypatch.setattr(qi, "get_transfer_capabilities", caps)
    monkeypatch.setattr(qi, "send_transfer", lambda *args, **kwargs: sent.append(args) or receipt("applied"))
    window._populate_send_to_menu()
    action = window._send_to_menu.actions()[0]
    assert entered.wait(1)
    assert action.text() == "Notebook (acceptance unknown)"
    heartbeat = []
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    qtbot.waitUntil(lambda: heartbeat == [True])
    release.set()
    qtbot.waitUntil(lambda: "confirmation required" in action.text())
    path.write_text("edited after discovery", encoding="utf-8")
    action.trigger()
    qtbot.waitUntil(lambda: hasattr(window, "_transfer_sender") and not window._transfer_sender._busy)
    assert len(calls) == 2  # Discovery and separate dispatch-time revalidation.
    assert sent == [(1000, "org.qdistro.Notebook.uid1000", "receiver-a", "text/plain", "edited after discovery")]
    assert window.statusBar().currentMessage() == "Text inserted into receiver editor; saving is separate."


def test_reopened_menu_ignores_stale_capability_result(sender, monkeypatch, qtbot):
    window, path = sender
    entered, release = threading.Event(), threading.Event()
    rows = [{"name": "Notebook", "uid": 1000, "service": "org.qdistro.Notebook.uid1000", "capabilities": {"version": 0}}]
    monkeypatch.setattr(qi, "send_to_targets", lambda **kwargs: rows)
    def caps(*args):
        entered.set()
        assert release.wait(2)
        return capabilities()
    monkeypatch.setattr(qi, "get_transfer_capabilities", caps)
    window._populate_send_to_menu()
    assert entered.wait(1)
    rows[0] = {**rows[0], "capabilities": capabilities(instance_id="new-instance", available=False, reason="Inbox full")}
    window._populate_send_to_menu()
    action = window._send_to_menu.actions()[0]
    assert not action.isEnabled()
    release.set()
    qtbot.waitUntil(lambda: not window._capability_discovery._busy)
    assert not action.isEnabled()
    assert action.toolTip() == "UTF-8 text. Inbox full"
    assert window._receiver_actions[(1000, "org.qdistro.Notebook.uid1000")][1]["instance_id"] == "new-instance"


def test_capability_discovery_is_bounded_and_closed_results_ignored(sender, monkeypatch, qtbot):
    from qfileman.transfer_jobs import CapabilityDiscovery

    window, path = sender
    calls, enriched = [], []
    discovery = CapabilityDiscovery(window, SimpleNamespace(
        get_transfer_capabilities=lambda *args: calls.append(args) or capabilities()))
    monkeypatch.setattr(discovery, "MAX_TARGETS", 2)
    discovery.enriched.connect(lambda *args: enriched.append(args))
    discovery.start([(1000, str(n)) for n in range(5)])
    qtbot.waitUntil(lambda: not discovery._busy)
    assert calls == [(1000, "0"), (1000, "1")]
    assert len(enriched) == 2
    discovery.close()
    discovery.result.emit(discovery._generation, (1000, "0", capabilities()), False)
    from PyQt6.QtWidgets import QApplication
    QApplication.processEvents()
    assert len(enriched) == 2
