"""Threaded App1 admission and consent produce actual editor dispositions."""
import threading

import pytest
from PyQt6.QtCore import QEventLoop, QThread, QTimer
from PyQt6.QtWidgets import QMainWindow, QTextEdit

from qnotebook import qdistro_integration as qi


@pytest.fixture
def receiver(qapp):
    window = QMainWindow()
    window.editor = QTextEdit(window)
    window.editor.setPlainText("Existing note")
    window._qdistro_autoconfirm = True
    controller = qi.NotebookTransfers(window)
    yield window, controller
    controller.shutdown()
    qapp.processEvents()
    window.close()
    window.deleteLater()


def test_threaded_stage_is_queued_and_applied_only_after_insertion(receiver, qtbot):
    window, controller = receiver
    results, receipts, threads = [], [], []
    before = window.editor.toPlainText()
    def complete(state, reason):
        receipts.append((state, reason))
        threads.append(QThread.currentThread())
    thread = threading.Thread(target=lambda: results.append(controller.stage("text/plain", "Transferred text", complete)))
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert results == [{"state": "staged", "reason": "Awaiting notebook confirmation"}]
    drop = window._qdistro_inbox[0]
    assert window.editor.toPlainText() == before
    assert receipts == []
    qtbot.waitUntil(lambda: len(receipts) == 1)
    assert receipts == [("applied", "Inserted into notebook editor; saving is separate")]
    assert window.editor.toPlainText().endswith("Transferred text")
    assert threads == [window.thread()]
    assert drop.payload == ""
    assert drop.complete is None
    assert window._qdistro_inbox == []


@pytest.mark.parametrize("failure,expected", [("decline", "declined"), ("confirm", "failed"),
                                              ("append", "failed"), ("readonly", "failed"),
                                              ("no_page", "failed")])
def test_terminal_dispositions_do_not_claim_noop_success(receiver, qtbot, monkeypatch, failure, expected):
    window, controller = receiver
    before = window.editor.toPlainText()
    receipts = []
    if failure == "decline":
        window._qdistro_autoconfirm = False
    elif failure == "confirm":
        monkeypatch.setattr(qi, "_confirm_drop", lambda *args: (_ for _ in ()).throw(RuntimeError("dialog failed")))
    elif failure == "append":
        monkeypatch.setattr(qi, "_append_confirmed", lambda *args: (_ for _ in ()).throw(RuntimeError("insert failed")))
    elif failure == "readonly":
        window.editor.setReadOnly(True)
    else:
        window._current_page = None
    assert controller.stage("text/plain", "secret", lambda state, reason: receipts.append(state))["state"] == "staged"
    qtbot.waitUntil(lambda: len(receipts) == 1)
    assert receipts == [expected]
    assert window.editor.toPlainText() == before
    assert window._qdistro_inbox == []


@pytest.mark.parametrize("payload,expected", [("x" * qi.MAX_PAYLOAD_BYTES, "staged"),
    ("é" * (qi.MAX_PAYLOAD_BYTES // 2), "staged"),
    ("é" * (qi.MAX_PAYLOAD_BYTES // 2 + 1), "rejected"),
    ("x" * (qi.MAX_PAYLOAD_BYTES + 1), "rejected"), ("\ud800", "rejected"), ("nul\x00", "rejected")])
def test_strict_encoded_limits(receiver, payload, expected):
    window, controller = receiver
    window._qdistro_autoconfirm = False
    result = controller.stage("text/plain", payload, lambda *args: None)
    assert result["state"] == expected
    assert len(window._qdistro_inbox) == (1 if expected == "staged" else 0)


def test_current_capacity_is_shared_with_legacy_and_revalidated(receiver, qtbot):
    window, controller = receiver
    window._qdistro_autoconfirm = False
    controller.receive_legacy("text/plain", "legacy")
    receipts = []
    for _ in range(qi.MAX_PENDING_DROPS - 1):
        assert controller.stage("text/plain", "new", lambda state, reason: receipts.append(state))["state"] == "staged"
    assert controller.capabilities()["available"] is False
    assert controller.stage("text/plain", "overflow", lambda *args: pytest.fail("Rejected drop cannot complete")) == {
        "state": "rejected", "reason": "Notebook inbox full"}
    assert len(window._qdistro_inbox) == qi.MAX_PENDING_DROPS
    qtbot.waitUntil(lambda: not window._qdistro_inbox)
    assert receipts == ["declined"] * (qi.MAX_PENDING_DROPS - 1)
    assert controller.capabilities()["available"] is True
    assert window.editor.toPlainText() == "Existing note"


def test_nested_modal_loop_keeps_one_confirmation_and_receipt_per_drop(receiver, qtbot, monkeypatch):
    window, controller = receiver
    depth = 0
    depths, receipts = [], []
    def confirm(win, drop):
        nonlocal depth
        depth += 1
        depths.append(depth)
        if drop.payload == "outer":
            thread = threading.Thread(target=lambda: controller.stage(
                "text/plain", "inner", lambda state, reason: receipts.append(("inner", state))))
            thread.start()
            thread.join(1)
            assert not thread.is_alive()
            loop = QEventLoop()
            QTimer.singleShot(0, loop.quit)
            loop.exec()
        depth -= 1
        return False
    monkeypatch.setattr(qi, "_confirm_drop", confirm)
    controller.stage("text/plain", "outer", lambda state, reason: receipts.append(("outer", state)))
    qtbot.waitUntil(lambda: len(receipts) == 2)
    assert depths == [1, 1]
    assert receipts == [("outer", "declined"), ("inner", "declined")]
    assert window._qdistro_inbox == []


def test_closed_receiver_rejects_and_disposes_staged_content(receiver, qtbot):
    window, controller = receiver
    receipts = []
    controller.stage("text/plain", "pending", lambda state, reason: receipts.append(state))
    drop = window._qdistro_inbox[0]
    controller.shutdown()
    qtbot.waitUntil(lambda: receipts == ["failed"])
    assert drop.payload == ""
    assert controller.capabilities()["available"] is False
    assert controller.stage("text/plain", "later", lambda *args: None)["state"] == "rejected"
    assert window.editor.toPlainText() == "Existing note"
