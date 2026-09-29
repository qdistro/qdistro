"""Sender text fidelity and visible transport outcomes, without live D-Bus."""
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QListWidgetItem, QMessageBox
from qfileman import qdistro_integration as qi
from qfileman.text_transfer import MAX_TEXT_BYTES, read_selected_text
from qfileman.window import FileManagerWindow


@pytest.mark.parametrize("data", [b"x" * MAX_TEXT_BYTES,
                                  ("é" * (MAX_TEXT_BYTES // 2)).encode("utf-8"),
                                  b"hello\r\nworld\n"])
def test_exact_text_bytes(tmp_path, data):
    path = tmp_path / "text"
    path.write_bytes(data)
    # ensures: a valid transfer preserves all UTF-8 bytes, including newlines.
    assert read_selected_text([str(path)]).encode("utf-8") == data


@pytest.mark.parametrize("data, message", [
    (b"x" * (MAX_TEXT_BYTES + 1), "sending limit"),
    (("é" * (MAX_TEXT_BYTES // 2 + 1)).encode("utf-8"), "sending limit"),
    (b"", "empty"), (b"\xff", "not valid UTF-8"),
    (b"hello\x00world", "NUL"),
])
def test_unrepresentable_text_refused(tmp_path, data, message):
    path = tmp_path / "text"
    path.write_bytes(data)
    # ensures: the sender cannot silently truncate or transform selected bytes.
    with pytest.raises(ValueError, match=message):
        read_selected_text([str(path)])
    assert path.read_bytes() == data


@pytest.mark.parametrize("paths", [[], ["one", "two"]])
def test_exactly_one_selection(paths):
    with pytest.raises(ValueError, match="exactly one"):
        read_selected_text(paths)


def test_file_growth_after_stat_is_not_truncated(tmp_path, monkeypatch):
    import os

    path = tmp_path / "growing"
    data = b"x" * (MAX_TEXT_BYTES + 1)
    path.write_bytes(data)
    real_fstat = os.fstat
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(
        st_mode=real_fstat(fd).st_mode, st_size=MAX_TEXT_BYTES))
    with pytest.raises(ValueError, match="sending limit"):
        read_selected_text([str(path)])
    assert path.read_bytes() == data


def test_regular_file_required(tmp_path):
    import os

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular text file"):
        read_selected_text([str(fifo)])
    with pytest.raises(ValueError, match="regular text file"):
        read_selected_text([str(tmp_path)])
    with pytest.raises(ValueError, match="Cannot read"):
        read_selected_text([str(tmp_path / "missing")])


@pytest.fixture
def sender(qapp, monkeypatch, tmp_path):
    win = FileManagerWindow()
    path = tmp_path / "text"
    path.write_text("original", encoding="utf-8")
    file_list = win._active_pane.file_list
    file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    def select(paths):
        file_list.clear()
        for selected_path in paths:
            item = QListWidgetItem(str(selected_path), file_list)
            item.setData(Qt.ItemDataRole.UserRole, {"path": str(selected_path)})
            item.setSelected(True)
    select([path])
    messages = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda _parent, title, text: messages.append((title, text)))
    yield SimpleNamespace(window=win, path=path, select=select, messages=messages)
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_menu_reads_selection_and_contents_at_dispatch(sender, monkeypatch):
    sent = []
    monkeypatch.setattr(qi, "send_to_targets", lambda **_: [
        {"uid": 1000, "service": "org.qdistro.Notebook.uid1000", "name": "Notebook"}])
    monkeypatch.setattr(qi, "send_payload", lambda *args, **kwargs: sent.append((args, kwargs)) or True)
    sender.window._populate_send_to_menu()
    assert sent == []
    changed = sender.path.with_name("changed")
    changed.write_bytes(b"current\r\ntext")
    sender.select([changed])
    sender.window._send_to_menu.actions()[0].trigger()
    assert sent == [((1000, "org.qdistro.Notebook.uid1000", "current\r\ntext"),
                     {"kind": "text/plain"})]
    assert sender.messages == []
    assert sender.window.statusBar().currentMessage() == "Text arrived at receiver; acceptance is unknown."


def test_menu_does_not_capture_stale_contents(sender, monkeypatch):
    sent = []
    monkeypatch.setattr(qi, "send_to_targets", lambda **_: [
        {"uid": 1000, "service": "org.qdistro.Notebook.uid1000", "name": "Notebook"}])
    monkeypatch.setattr(qi, "send_payload", lambda *args, **kwargs: sent.append(args[2]) or True)
    sender.window._populate_send_to_menu()
    sender.path.write_text("edited after opening menu", encoding="utf-8")
    sender.window._send_to_menu.actions()[0].trigger()
    assert sent == ["edited after opening menu"]


@pytest.mark.parametrize("invalid", ["empty", "multiple", "invalid", "oversized"])
def test_invalid_selection_never_dispatched(sender, monkeypatch, invalid):
    sent = []
    monkeypatch.setattr(qi, "send_payload", lambda *args, **kwargs: sent.append(args))
    if invalid == "multiple":
        sender.select([sender.path, sender.path])
    else:
        sender.path.write_bytes({"empty": b"", "invalid": b"\xff",
                                 "oversized": b"x" * (MAX_TEXT_BYTES + 1)}[invalid])
    sender.window._send_selected_text(1000, "org.qdistro.Notebook.uid1000")
    assert sent == []
    assert len(sender.messages) == 1
    assert sender.messages[0][0] == "Send Text To"


@pytest.mark.parametrize("outcome", [False, RuntimeError("relay failed"), TimeoutError("late reply")])
def test_unconfirmed_transport_visible_and_acceptance_unknown(sender, monkeypatch, outcome):
    def send(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(qi, "send_payload", send)
    sender.window._send_selected_text(1000, "org.qdistro.Notebook.uid1000")
    assert sender.messages == [("Send Text To",
        "Text transfer was not confirmed. Receiver acceptance is unknown; "
        "the receiver may have received the text.")]
    assert sender.window.statusBar().currentMessage() != "Text arrived at receiver; acceptance is unknown."


@pytest.mark.parametrize("next_outcome", ["refused", False,
                                         RuntimeError("relay failed"), TimeoutError("late reply")])
def test_new_attempt_clears_previous_arrival_status(sender, monkeypatch, next_outcome):
    sent = []
    def send(*args, **kwargs):
        # The old success must already be gone while the new send is in progress.
        assert sender.window.statusBar().currentMessage() == ""
        sent.append(args)
        if len(sent) == 1:
            return True
        if isinstance(next_outcome, Exception):
            raise next_outcome
        return next_outcome

    monkeypatch.setattr(qi, "send_payload", send)
    sender.window._send_selected_text(1000, "org.qdistro.Notebook.uid1000")
    assert sender.window.statusBar().currentMessage() == "Text arrived at receiver; acceptance is unknown."
    assert sender.messages == []
    if next_outcome == "refused":
        sender.path.write_bytes(b"")
    sender.window._send_selected_text(1000, "org.qdistro.Notebook.uid1000")
    # No timer wait: a refusal or unknown outcome replaces the prior send immediately.
    assert sender.window.statusBar().currentMessage() == ""
    if next_outcome == "refused":
        assert len(sent) == 1, "validation refusal dispatched the empty file"
        assert sender.messages == [("Send Text To", "The selected file is empty; there is no text to send.")]
    else:
        assert len(sent) == 2
        assert sender.messages == [("Send Text To",
            "Text transfer was not confirmed. Receiver acceptance is unknown; "
            "the receiver may have received the text.")]
