"""Unit tests for the standalone FilePane widget."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidget, QMessageBox
from qfileman.pane import FilePane


@pytest.fixture
def pane(qapp, tmp_dir):
    """A fresh FilePane rooted at home, ready for navigation."""
    p = FilePane()
    yield p
    p.close()
    p.deleteLater()
    qapp.processEvents()
    qapp.processEvents()


def test_pane_initial_path_is_home(pane):
    assert pane.current_path == os.path.expanduser("~")


def test_pane_update_path_navigates(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    assert pane.current_path == str(tmp_dir)
    assert pane.path_edit.text() == str(tmp_dir)


def test_pane_update_path_emits_path_changed(pane, tmp_dir):
    received = []
    pane.path_changed.connect(received.append)
    pane._update_path(str(tmp_dir))
    assert received == [str(tmp_dir)]


def test_pane_load_files_emits_status_changed(pane, tmp_dir):
    received = []
    pane.status_changed.connect(received.append)
    pane._update_path(str(tmp_dir))
    # First status update from _load_files inside _update_path.
    assert any("items" in s for s in received)


def test_pane_load_files_hides_dotfiles_by_default(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    names = [pane.file_list.item(i).text() for i in range(pane.file_list.count())]
    assert ".hidden" not in names


def test_pane_toggle_hidden_shows_dotfiles(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    pane._toggle_hidden(True)
    names = [pane.file_list.item(i).text() for i in range(pane.file_list.count())]
    assert ".hidden" in names


def test_pane_change_view_switches_view_mode(pane):
    pane._change_view("Grid")
    assert pane.file_list.viewMode() == QListWidget.ViewMode.IconMode
    pane._change_view("List")
    assert pane.file_list.viewMode() == QListWidget.ViewMode.ListMode


def test_pane_change_sort_persists_after_refresh(pane, tmp_path):
    test_dir = tmp_path / "s"
    test_dir.mkdir()
    (test_dir / "a.bin").write_text("x" * 100)
    (test_dir / "b.bin").write_text("x")  # smallest

    pane._update_path(str(test_dir))
    pane._change_sort("Size")
    assert pane.file_list.item(0).text() == "b.bin"
    pane._refresh()
    assert pane.file_list.item(0).text() == "b.bin"


def test_pane_go_back_and_forward(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    subdir = tmp_dir / "subdir"
    pane._update_path(str(subdir))
    pane._go_back()
    assert pane.current_path == str(tmp_dir)
    pane._go_forward()
    assert pane.current_path == str(subdir)


def test_pane_go_up_at_root_is_noop(pane):
    pane._update_path("/")
    pane._go_up()
    assert pane.current_path == "/"


def test_pane_invalid_path_via_path_edit_warns(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    pane.path_edit.setText("/nonexistent/zzzzz")
    with patch("qfileman.pane.QMessageBox.warning") as mock_warning:
        pane._on_path_enter()
    assert mock_warning.called
    assert pane.current_path == str(tmp_dir), "Path must not change on invalid input"


def test_pane_double_click_directory_navigates(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    # Find the subdir item
    item = None
    for i in range(pane.file_list.count()):
        if pane.file_list.item(i).text() == "subdir":
            item = pane.file_list.item(i)
            break
    assert item is not None
    pane._on_file_double_click(item)
    assert pane.current_path == str(tmp_dir / "subdir")


def test_pane_double_click_file_invokes_xdg_open(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    item = None
    for i in range(pane.file_list.count()):
        if pane.file_list.item(i).text() == "file1.txt":
            item = pane.file_list.item(i)
            break
    assert item is not None
    with patch("qfileman.pane.subprocess.Popen") as mock_popen:
        pane._on_file_double_click(item)
    mock_popen.assert_called_once_with(["xdg-open", str(tmp_dir / "file1.txt")])


def test_pane_delete_confirms_and_removes(pane, tmp_dir, qtbot):
    victim = tmp_dir / "delete_me.txt"
    victim.write_text("x")
    pane._update_path(str(tmp_dir))
    for i in range(pane.file_list.count()):
        if pane.file_list.item(i).text() == "delete_me.txt":
            pane.file_list.setCurrentRow(i)
            break
    def fake_run(argv, capture_output, text, timeout):
        victim.unlink()
        class Result:
            returncode = 0
            stderr = ""
            stdout = ""
        return Result()

    with patch(
        "qfileman.pane.QMessageBox.question",
        return_value=QMessageBox.StandardButton.Yes,
    ), patch(
        "qfileman.plugins.builtin.trash.trash_argv", return_value=["true"]
    ), patch(
        "qfileman.pane.subprocess.run", side_effect=fake_run
    ) as run:
        pane._delete()
        # The trash op now runs off the GUI thread and the list refresh is
        # queued back on the GUI thread on completion; wait for both the
        # filesystem side effect and the model-driven list update.
        qtbot.waitUntil(
            lambda: not victim.exists()
            and "delete_me.txt" not in [
                pane.file_list.item(i).text()
                for i in range(pane.file_list.count())
            ],
            timeout=5000,
        )
    assert not victim.exists()
    names = [pane.file_list.item(i).text() for i in range(pane.file_list.count())]
    assert "delete_me.txt" not in names
    run.assert_called_once()


def test_pane_rename_via_dialog(pane, tmp_dir):
    src = tmp_dir / "old.txt"
    src.write_text("x")
    pane._update_path(str(tmp_dir))
    for i in range(pane.file_list.count()):
        if pane.file_list.item(i).text() == "old.txt":
            pane.file_list.setCurrentRow(i)
            break
    with patch(
        "qfileman.pane.QInputDialog.getText", return_value=("new.txt", True)
    ):
        pane._rename()
    assert (tmp_dir / "new.txt").exists()
    assert not src.exists()


def test_pane_rename_cancel_is_noop(pane, tmp_dir):
    src = tmp_dir / "keep.txt"
    src.write_text("x")
    pane._update_path(str(tmp_dir))
    for i in range(pane.file_list.count()):
        if pane.file_list.item(i).text() == "keep.txt":
            pane.file_list.setCurrentRow(i)
            break
    # User clicks Cancel → ok=False → nothing changes on disk.
    with patch("qfileman.pane.QInputDialog.getText", return_value=("ignored", False)):
        pane._rename()
    assert src.exists()


def test_pane_rename_rejects_path_separator(pane, tmp_dir):
    src = tmp_dir / "keep.txt"
    src.write_text("x")
    pane._update_path(str(tmp_dir))
    for i in range(pane.file_list.count()):
        if pane.file_list.item(i).text() == "keep.txt":
            pane.file_list.setCurrentRow(i)
            break
    with patch(
        "qfileman.pane.QInputDialog.getText",
        return_value=("../escape.txt", True),
    ), patch("qfileman.pane.QMessageBox.warning") as warning:
        pane._rename()
    assert src.exists()
    assert not (tmp_dir.parent / "escape.txt").exists()
    warning.assert_called_once()


def test_pane_rename_prompts_before_overwrite_and_aborts_on_no(pane, tmp_dir):
    """Renaming onto an existing sibling must confirm; 'No' leaves both."""
    src = tmp_dir / "old.txt"
    src.write_text("source")
    victim = tmp_dir / "taken.txt"
    victim.write_text("victim")
    pane._update_path(str(tmp_dir))
    for i in range(pane.file_list.count()):
        if pane.file_list.item(i).text() == "old.txt":
            pane.file_list.setCurrentRow(i)
            break
    with patch(
        "qfileman.pane.QInputDialog.getText", return_value=("taken.txt", True)
    ), patch(
        "qfileman.pane.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    ) as question:
        pane._rename()
    question.assert_called_once()
    assert src.read_text() == "source"
    assert victim.read_text() == "victim"


def test_pane_rename_overwrite_on_yes(pane, tmp_dir):
    src = tmp_dir / "old.txt"
    src.write_text("source")
    victim = tmp_dir / "taken.txt"
    victim.write_text("victim")
    pane._update_path(str(tmp_dir))
    for i in range(pane.file_list.count()):
        if pane.file_list.item(i).text() == "old.txt":
            pane.file_list.setCurrentRow(i)
            break
    with patch(
        "qfileman.pane.QInputDialog.getText", return_value=("taken.txt", True)
    ), patch(
        "qfileman.pane.QMessageBox.question",
        return_value=QMessageBox.StandardButton.Yes,
    ):
        pane._rename()
    assert not src.exists()
    assert (tmp_dir / "taken.txt").read_text() == "source"


def test_pane_new_folder_creates_directory(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    with patch(
        "qfileman.pane.QInputDialog.getText", return_value=("fresh", True)
    ):
        pane._new_folder()
    assert (tmp_dir / "fresh").is_dir()


def test_pane_new_folder_cancel_is_noop(pane, tmp_dir):
    pane._update_path(str(tmp_dir))
    with patch("qfileman.pane.QInputDialog.getText", return_value=("", False)):
        pane._new_folder()
    assert not (tmp_dir / "should_not_exist").exists()


def test_pane_set_plugin_manager_pushes_file_filters(pane, tmp_dir):
    class OnlyMarkdown:
        name = "only_md"
        capabilities = ["file_filter"]

        def filter_files(self, paths):
            return [p for p in paths if p.endswith(".md")]

    mock_pm = MagicMock()
    mock_pm.get_file_filters.return_value = [OnlyMarkdown()]
    mock_pm.get_navigation_hooks.return_value = []
    pane.set_plugin_manager(mock_pm)
    pane._update_path(str(tmp_dir))
    names = [pane.file_list.item(i).text() for i in range(pane.file_list.count())]
    assert names == ["file3.md"]


def test_pane_navigation_hook_can_veto(pane, tmp_dir, caplog):
    class Vetoer:
        name = "v"
        capabilities = ["navigation_hook"]

        def on_enter_directory(self, path):
            return False

        def on_leave_directory(self, path):
            pass

    mock_pm = MagicMock()
    mock_pm.get_navigation_hooks.return_value = [Vetoer()]
    mock_pm.get_file_filters.return_value = []
    pane.set_plugin_manager(mock_pm)
    start = pane.current_path

    with caplog.at_level("INFO", logger="qfileman.pane"):
        pane._update_path(str(tmp_dir))

    assert pane.current_path == start, "Vetoed navigation must not change path"
    assert any("blocked navigation" in r.message for r in caplog.records)


def test_pane_mouse_press_emits_focused(pane, qapp):
    received = []
    pane.focused.connect(lambda: received.append(True))
    # Synthesize a mouse press on the pane widget. PyQt6's QMouseEvent
    # constructor wants QPointF positions, not QPoint.
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent

    pos = QPointF(5.0, 5.0)
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        pos,
        pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    pane.mousePressEvent(event)
    assert received == [True]
