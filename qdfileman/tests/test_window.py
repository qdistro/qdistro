"""Tests for main window."""

import os
import time
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtWidgets import QListWidget, QMessageBox, QToolBar
from qfileman.window import FileManagerWindow


@pytest.fixture
def window(qapp, tmp_dir):
    """Create a file manager window for testing."""
    win = FileManagerWindow()
    yield win
    # Proper cleanup: close and deleteLater
    win.close()
    win.deleteLater()
    # Process events to ensure cleanup
    qapp.processEvents()
    qapp.processEvents()


def test_window_title(window):
    """Test window title."""
    assert window.windowTitle() == "QFileMan"


def test_window_default_size(window):
    """Test default window size."""
    assert window.width() == 900
    assert window.height() == 600


def test_window_has_file_list(window):
    """Test that file list widget exists."""
    assert window.file_list is not None


def test_window_has_tree_view(window):
    """Test that tree view exists."""
    assert window.tree_view is not None


def test_window_has_toolbar(window):
    """Test that toolbar exists and has expected controls."""
    # Find the toolbar
    toolbars = window.findChildren(QToolBar)
    assert len(toolbars) == 1, "Expected exactly one toolbar"

    toolbar = toolbars[0]
    # Check for navigation buttons (back, forward, up, refresh)
    # They are QPushButton widgets in the toolbar
    actions = toolbar.actions()
    assert len(actions) >= 6, f"Expected at least 6 toolbar actions, got {len(actions)}"

    # Check view and sort combos exist
    assert hasattr(window, 'view_combo'), "view_combo should exist"
    assert hasattr(window, 'sort_combo'), "sort_combo should exist"
    assert window.view_combo.count() == 2, "view_combo should have 2 items"
    assert window.sort_combo.count() == 4, "sort_combo should have 4 items"

    # Check hidden checkbox exists
    assert hasattr(window, 'hidden_check'), "hidden_check should exist"


def test_window_path_edit(window, tmp_dir):
    """Test path edit widget."""
    window._update_path(str(tmp_dir))
    assert window.path_edit.text() == str(tmp_dir)


def test_window_load_files(window, tmp_dir):
    """Test file loading shows expected files."""
    window._update_path(str(tmp_dir))
    count = window.file_list.count()

    # tmp_dir has: file1.txt, file2.txt, file3.md, subdir/, .hidden
    # Hidden is not shown by default, so we should see 4 items
    assert count == 4, f"Expected 4 visible files, got {count}"

    # Check specific file names are present
    names = [window.file_list.item(i).text() for i in range(count)]
    assert "file1.txt" in names
    assert "file2.txt" in names
    assert "file3.md" in names
    assert "subdir" in names
    assert ".hidden" not in names, "Hidden file should not be shown"


def test_window_navigation_up(window, nested_tmp_dir):
    """Test going up directory from a nested directory."""
    child_path = str(nested_tmp_dir / "child_dir")
    window._update_path(child_path)

    parent_path = str(nested_tmp_dir)
    window._go_up()

    assert window.current_path == parent_path, \
        f"Expected current_path to be {parent_path}, got {window.current_path}"


def test_window_navigation_home(window):
    """Test going to home directory."""
    window._go_home()
    expected = os.path.expanduser("~")
    assert window.current_path == expected, \
        f"Expected current_path to be {expected}, got {window.current_path}"


def test_window_refresh(window, tmp_dir):
    """Test refresh picks up new files."""
    window._update_path(str(tmp_dir))
    initial_count = window.file_list.count()

    # Create a new file
    new_file = tmp_dir / "newfile.txt"
    new_file.write_text("new content")

    # Refresh
    window._refresh()

    # Should now have one more file
    new_count = window.file_list.count()
    assert new_count == initial_count + 1, \
        f"Expected {initial_count + 1} files after refresh, got {new_count}"

    # Verify the new file is in the list
    names = [window.file_list.item(i).text() for i in range(new_count)]
    assert "newfile.txt" in names, "New file should appear after refresh"


def test_window_toggle_hidden(window, tmp_dir):
    """Test toggling hidden files shows/hides .hidden file."""
    # Initially hidden files should NOT be shown
    window._update_path(str(tmp_dir))
    names = [window.file_list.item(i).text() for i in range(window.file_list.count())]
    assert ".hidden" not in names, "Hidden file should not be shown when checkbox unchecked"

    # Enable hidden files
    window.hidden_check.setChecked(True)
    window._toggle_hidden(True)

    # Now hidden files should be shown
    names = [window.file_list.item(i).text() for i in range(window.file_list.count())]
    assert ".hidden" in names, "Hidden file should be shown when checkbox checked"

    # Disable hidden files again
    window.hidden_check.setChecked(False)
    window._toggle_hidden(False)

    # Hidden files should be hidden again
    names = [window.file_list.item(i).text() for i in range(window.file_list.count())]
    assert ".hidden" not in names, "Hidden file should not be shown after toggling off"


def test_window_change_view(window):
    """Test changing view mode."""
    window._change_view("List")
    assert window.file_list.viewMode() == QListWidget.ViewMode.ListMode

    window._change_view("Grid")
    assert window.file_list.viewMode() == QListWidget.ViewMode.IconMode


def test_window_change_sort(window, tmp_path):
    """Test changing sort order by size, date, and type."""
    # Create a fresh directory with files of different sizes/mtimes/types
    test_dir = tmp_path / "sort_test"
    test_dir.mkdir()

    # Create files with staggered sizes
    (test_dir / "small.aaa").write_text("a")           # 1 byte
    (test_dir / "medium.aaa").write_text("x" * 100)     # 100 bytes
    (test_dir / "large.zzz").write_text("x" * 10000)   # 10000 bytes

    # Set staggered mtimes (60 seconds apart)
    base_time = int(time.time())
    os.utime(test_dir / "small.aaa", (base_time, base_time))
    os.utime(test_dir / "medium.aaa", (base_time - 60, base_time - 60))
    os.utime(test_dir / "large.zzz", (base_time - 120, base_time - 120))

    window._update_path(str(test_dir))

    # Sort by size - small file should be first
    window._change_sort("Size")
    first_by_size = window.file_list.item(0).text()
    assert first_by_size == "small.aaa", f"Expected small.aaa first by size, got {first_by_size}"

    # Verify sort persists after refresh
    window._refresh()
    first_after_refresh = window.file_list.item(0).text()
    assert first_after_refresh == "small.aaa", f"Sort should persist after refresh, got {first_after_refresh}"

    # Sort by date - small.aaa should be first (newest mtime, file-manager convention)
    window._change_sort("Date")
    first_by_date = window.file_list.item(0).text()
    assert first_by_date == "small.aaa", f"Expected small.aaa first by date (newest), got {first_by_date}"

    # Sort by type - .aaa files should come before .zzz (alphabetically by extension)
    window._change_sort("Type")
    first_by_type = window.file_list.item(0).text()
    assert first_by_type.endswith(".aaa"), f"Expected .aaa extension first by type, got {first_by_type}"

    # Verify type sort persists after refresh
    window._refresh()
    first_type_after_refresh = window.file_list.item(0).text()
    assert first_type_after_refresh.endswith(".aaa"), "Type sort should persist after refresh"


def test_window_has_plugins_menu(window):
    """Test that plugins menu exists."""
    assert window.plugins_menu is not None


def test_window_context_menu(window, tmp_dir):
    """Test context menu shows expected actions for a file."""
    # Create a test file (not a directory)
    test_file = tmp_dir / "testfile.txt"
    test_file.write_text("test content")

    window._update_path(str(tmp_dir))

    # Find and select the file item
    for i in range(window.file_list.count()):
        if window.file_list.item(i).text() == "testfile.txt":
            window.file_list.setCurrentRow(i)
            break

    # Use visualItemRect to get the actual position over the item
    item = window.file_list.currentItem()
    rect = window.file_list.visualItemRect(item)

    # Mock QMenu to capture what actions are added
    with patch('qfileman.pane.QMenu') as MockMenu:
        mock_menu = MagicMock()
        MockMenu.return_value = mock_menu

        window._show_context_menu(rect.center())

        # Get all the action objects that were added
        added_actions = [call[0][0] for call in mock_menu.addAction.call_args_list]

        # Get the text from each action (they're QAction objects)
        added_texts = []
        for action in added_actions:
            if hasattr(action, 'text'):
                added_texts.append(action.text())
            elif isinstance(action, str):
                added_texts.append(action)

        # File item should have Rename, Move to Trash, Copy Path but NOT Open
        assert "Rename" in added_texts, f"Rename should be in menu, got {added_texts}"
        assert "Move to Trash" in added_texts, f"Move to Trash should be in menu, got {added_texts}"
        assert "Copy Path" in added_texts, f"Copy Path should be in menu, got {added_texts}"
        assert "Open" not in added_texts, f"Open should NOT be in menu for file, got {added_texts}"


def test_window_context_menu_directory(window, tmp_dir):
    """Test context menu shows Open action for a directory."""
    # Use the existing subdir
    window._update_path(str(tmp_dir))

    # Find and select the directory item
    for i in range(window.file_list.count()):
        if window.file_list.item(i).text() == "subdir":
            window.file_list.setCurrentRow(i)
            break

    # Use visualItemRect to get the actual position over the item
    item = window.file_list.currentItem()
    rect = window.file_list.visualItemRect(item)

    # Mock QMenu to capture what actions are added
    with patch('qfileman.pane.QMenu') as MockMenu:
        mock_menu = MagicMock()
        MockMenu.return_value = mock_menu

        window._show_context_menu(rect.center())

        # Get all the action objects that were added
        added_actions = [call[0][0] for call in mock_menu.addAction.call_args_list]

        # Get the text from each action
        added_texts = []
        for action in added_actions:
            if hasattr(action, 'text'):
                added_texts.append(action.text())
            elif isinstance(action, str):
                added_texts.append(action)

        # Directory item should have Open in addition to Rename, Move to Trash, Copy Path
        assert "Open" in added_texts, f"Open should be in menu for directory, got {added_texts}"
        assert "Rename" in added_texts, f"Rename should be in menu, got {added_texts}"
        assert "Move to Trash" in added_texts, f"Move to Trash should be in menu, got {added_texts}"
        assert "Copy Path" in added_texts, f"Copy Path should be in menu, got {added_texts}"


def test_delete_file(window, tmp_dir, qtbot):
    """Test moving a file to trash."""
    test_file = tmp_dir / "to_delete.txt"
    test_file.write_text("delete me")

    window._update_path(str(tmp_dir))

    # Find and select the file by name
    for i in range(window.file_list.count()):
        if window.file_list.item(i).text() == "to_delete.txt":
            window.file_list.setCurrentRow(i)
            break

    def fake_run(argv, capture_output, text, timeout):
        test_file.unlink()
        class Result:
            returncode = 0
            stderr = ""
            stdout = ""
        return Result()

    with patch('qfileman.pane.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
            patch('qfileman.plugins.builtin.trash.trash_argv', return_value=["true"]), \
            patch('qfileman.pane.subprocess.run', side_effect=fake_run):
        window._delete()
        # The trash op now runs off the GUI thread and the list refresh is
        # queued back on the GUI thread on completion; wait for both.
        qtbot.waitUntil(
            lambda: "to_delete.txt" not in [
                window.file_list.item(i).text()
                for i in range(window.file_list.count())
            ],
            timeout=5000,
        )

    # File should be gone
    assert not test_file.exists(), "File should be deleted"

    # File should be gone from list
    names = [window.file_list.item(i).text() for i in range(window.file_list.count())]
    assert "to_delete.txt" not in names


def test_delete_file_warns_when_no_trash_backend(window, tmp_dir):
    """Default delete should not fall back to permanent deletion."""
    test_file = tmp_dir / "to_delete.txt"
    test_file.write_text("delete me")

    window._update_path(str(tmp_dir))
    for i in range(window.file_list.count()):
        if window.file_list.item(i).text() == "to_delete.txt":
            window.file_list.setCurrentRow(i)
            break

    with patch('qfileman.pane.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
            patch('qfileman.plugins.builtin.trash.trash_argv', return_value=None), \
            patch('qfileman.pane.QMessageBox.warning') as warn, \
            patch('qfileman.pane.subprocess.run') as run:
        window._delete()

    assert test_file.exists()
    run.assert_not_called()
    warn.assert_called_once()
    assert "No system trash backend" in warn.call_args.args[2]


def test_delete_directory(window, tmp_dir, qtbot):
    """Test moving a directory to trash."""
    test_dir = tmp_dir / "to_delete_dir"
    test_dir.mkdir()

    window._update_path(str(tmp_dir))

    # Find and select the directory by name
    for i in range(window.file_list.count()):
        if window.file_list.item(i).text() == "to_delete_dir":
            window.file_list.setCurrentRow(i)
            break

    def fake_run(argv, capture_output, text, timeout):
        test_dir.rmdir()
        class Result:
            returncode = 0
            stderr = ""
            stdout = ""
        return Result()

    with patch('qfileman.pane.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
            patch('qfileman.plugins.builtin.trash.trash_argv', return_value=["true"]), \
            patch('qfileman.pane.subprocess.run', side_effect=fake_run):
        window._delete()
        # The trash op now runs off the GUI thread and the list refresh is
        # queued back on the GUI thread on completion; wait for both.
        qtbot.waitUntil(
            lambda: not test_dir.exists()
            and "to_delete_dir" not in [
                window.file_list.item(i).text()
                for i in range(window.file_list.count())
            ],
            timeout=5000,
        )

    # Directory should be gone
    assert not test_dir.exists(), "Directory should be deleted"
    names = [window.file_list.item(i).text() for i in range(window.file_list.count())]
    assert "to_delete_dir" not in names


def test_rename(window, tmp_dir):
    """Test renaming a file."""
    test_file = tmp_dir / "old_name.txt"
    test_file.write_text("rename me")

    window._update_path(str(tmp_dir))
    # Select the file
    for i in range(window.file_list.count()):
        if window.file_list.item(i).text() == "old_name.txt":
            window.file_list.setCurrentRow(i)
            break

    # Monkeypatch the input dialog
    with patch(
        'qfileman.pane.QInputDialog.getText', return_value=("new_name.txt", True)
    ):
        window._rename()

    # File should be renamed
    assert (tmp_dir / "new_name.txt").exists(), "New file should exist"
    assert not (tmp_dir / "old_name.txt").exists(), "Old file should not exist"


def test_path_edit_invalid(window):
    """Test entering invalid path shows warning."""
    invalid_path = "/nonexistent/path/xyz123"

    # Set the path_edit to invalid path
    window.path_edit.setText(invalid_path)

    # Monkeypatch QMessageBox.warning
    with patch('qfileman.pane.QMessageBox.warning') as mock_warning:
        window._on_path_enter()

        # Warning should have been called
        assert mock_warning.called, "Warning should be shown for invalid path"

    # current_path should not have changed to invalid path
    assert window.current_path != invalid_path


def test_open_file_invokes_xdg_open(window, tmp_dir):
    """Test double-clicking a file invokes xdg-open."""
    test_file = tmp_dir / "test.txt"
    test_file.write_text("test")

    window._update_path(str(tmp_dir))

    # Find and click the test file
    for i in range(window.file_list.count()):
        item = window.file_list.item(i)
        if item.text() == "test.txt":
            with patch('qfileman.pane.subprocess.Popen') as mock_popen:
                window._on_file_double_click(item)
                mock_popen.assert_called_once_with(["xdg-open", str(test_file)])
            break


def test_closeevent_disables_plugins(window):
    """Test close event disables plugins."""
    # Create a mock plugin manager
    mock_pm = MagicMock()
    mock_pm.enabled_plugins.return_value = ["plugin1", "plugin2"]
    mock_pm.disable = MagicMock()

    window._plugin_manager = mock_pm
    window.close()

    # Disable should be called for each enabled plugin
    assert mock_pm.disable.call_count == 2


def test_go_back_and_forward(window, tmp_dir):
    """Test back/forward navigation."""
    # Navigate to tmp_dir
    window._update_path(str(tmp_dir))

    # Navigate to subdir
    subdir = tmp_dir / "subdir"
    window._update_path(str(subdir))

    # Go back
    window._go_back()
    assert window.current_path == str(tmp_dir), "Should go back to parent"

    # Go forward
    window._go_forward()
    assert window.current_path == str(subdir), "Should go forward to subdir"


def test_set_plugin_manager_pushes_file_filters(window, tmp_dir):
    """File filters from the plugin manager should drop files from the view."""
    class OnlyMarkdown:
        name = "only_md"
        capabilities = ["file_filter"]

        def filter_files(self, paths):
            return [p for p in paths if p.endswith(".md")]

    mock_pm = MagicMock()
    mock_pm.get_file_filters.return_value = [OnlyMarkdown()]
    # Navigation hooks not relevant here.
    mock_pm.get_navigation_hooks.return_value = []

    window.set_plugin_manager(mock_pm)
    window._update_path(str(tmp_dir))

    names = [window.file_list.item(i).text() for i in range(window.file_list.count())]
    assert names == ["file3.md"], f"Filter should leave only the .md file; got {names}"


def test_set_plugin_manager_replaces_prior_filters(window, tmp_dir):
    """Calling set_plugin_manager twice should not stack filters."""
    class DropAll:
        name = "drop_all"
        capabilities = ["file_filter"]

        def filter_files(self, paths):
            return []

    pm1 = MagicMock()
    pm1.get_file_filters.return_value = [DropAll()]
    pm1.get_navigation_hooks.return_value = []
    window.set_plugin_manager(pm1)
    window._update_path(str(tmp_dir))
    assert window.file_list.count() == 0

    pm2 = MagicMock()
    pm2.get_file_filters.return_value = []
    pm2.get_navigation_hooks.return_value = []
    window.set_plugin_manager(pm2)
    window._update_path(str(tmp_dir))
    assert window.file_list.count() > 0, "Filters from the prior manager should be cleared"


def test_navigation_hook_can_veto(window, tmp_dir, caplog):
    """A NavigationHook returning False from on_enter_directory blocks the move."""
    class Vetoer:
        name = "vetoer"
        capabilities = ["navigation_hook"]
        called_with = []

        def on_enter_directory(self, path):
            self.called_with.append(path)
            return False

        def on_leave_directory(self, path):
            pass

    hook = Vetoer()
    mock_pm = MagicMock()
    mock_pm.get_navigation_hooks.return_value = [hook]
    mock_pm.get_file_filters.return_value = []
    window.set_plugin_manager(mock_pm)

    starting_path = window.current_path
    target = str(tmp_dir / "subdir")

    with caplog.at_level("INFO", logger="qfileman.pane"):
        window._update_path(target)

    assert hook.called_with == [target]
    assert window.current_path == starting_path, "Vetoed navigation must not change current_path"
    assert any("blocked navigation" in r.message for r in caplog.records)


def test_navigation_hook_leave_called_on_successful_move(window, tmp_dir):
    """on_leave_directory should fire with the previous path after a successful move."""
    class Tracker:
        name = "tracker"
        capabilities = ["navigation_hook"]
        leaves = []
        enters = []

        def on_enter_directory(self, path):
            self.enters.append(path)
            return True

        def on_leave_directory(self, path):
            self.leaves.append(path)

    hook = Tracker()
    mock_pm = MagicMock()
    mock_pm.get_navigation_hooks.return_value = [hook]
    mock_pm.get_file_filters.return_value = []
    window.set_plugin_manager(mock_pm)

    starting_path = window.current_path
    window._update_path(str(tmp_dir))
    window._update_path(str(tmp_dir / "subdir"))

    # First leave: from the starting path (home), second: from tmp_dir.
    assert hook.leaves == [starting_path, str(tmp_dir)]
    assert hook.enters == [str(tmp_dir), str(tmp_dir / "subdir")]


def test_apply_preferences_propagates_to_model(window, tmp_dir, monkeypatch, tmp_path):
    """After Config is mutated, _apply_preferences pushes settings into model+UI."""
    from qfileman import config as config_mod
    from qfileman.config import Config

    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    Config._data = None
    cfg = Config()
    cfg.set("general", "show_hidden", True)
    cfg.set("general", "sort_by", "size")
    cfg.set("general", "sort_order", "desc")
    cfg.set("general", "default_view", "grid")

    window._update_path(str(tmp_dir))
    window._apply_preferences()

    # Hidden checkbox now reflects the config value.
    assert window.hidden_check.isChecked() is True
    # Hidden file should be visible in the model after the toggle.
    names = [window.file_list.item(i).text() for i in range(window.file_list.count())]
    assert ".hidden" in names
    # View mode should have been switched to icon (grid).
    from PyQt6.QtWidgets import QListWidget
    assert window.file_list.viewMode() == QListWidget.ViewMode.IconMode

    Config._instance = None
    Config._data = None


def test_navigate_to_result_selects_basename(window, tmp_dir):
    """_navigate_to_result navigates to the parent and highlights the file."""
    target_file = tmp_dir / "file1.txt"
    window._navigate_to_result(str(target_file))
    assert window.current_path == str(tmp_dir)
    current = window.file_list.currentItem()
    assert current is not None
    assert current.text() == "file1.txt"


def test_window_starts_with_single_pane(window):
    """A fresh window has exactly one FilePane and Close Pane disabled."""
    panes = window._split_root.find_panes()
    assert len(panes) == 1
    assert window._active_pane is panes[0]
    assert window.close_pane_action.isEnabled() is False


def test_split_right_creates_second_pane(window, tmp_dir):
    """Split Right adds a sibling pane rooted at the active pane's path."""
    window._update_path(str(tmp_dir))
    new_pane = window._split_right()
    panes = window._split_root.find_panes()
    assert new_pane is not None
    assert len(panes) == 2
    # The new pane should start where the source pane was.
    assert new_pane.current_path == str(tmp_dir)
    # Close Pane should be enabled now.
    assert window.close_pane_action.isEnabled() is True
    # Active pane should be the new one after split.
    assert window._active_pane is new_pane


def test_split_down_creates_vertical_split(window, tmp_dir):
    """Split Down nests if needed to produce a vertical orientation."""
    from PyQt6.QtCore import Qt

    window._update_path(str(tmp_dir))
    window._split_right()  # 2 horizontal panes
    new = window._split_down()
    panes = window._split_root.find_panes()
    assert new is not None
    assert len(panes) == 3
    # At least one SplitContainer in the tree must be vertical.
    def has_vertical(node):
        from qfileman.split_container import SplitContainer

        if node.orientation() == Qt.Orientation.Vertical:
            return True
        for i in range(node.count()):
            child = node.widget(i)
            if isinstance(child, SplitContainer) and has_vertical(child):
                return True
        return False

    assert has_vertical(window._split_root)


def test_close_pane_removes_active(window, tmp_dir):
    window._update_path(str(tmp_dir))
    new_pane = window._split_right()
    assert len(window._split_root.find_panes()) == 2
    window._close_pane()
    remaining = window._split_root.find_panes()
    assert len(remaining) == 1
    assert new_pane not in remaining
    # Close Pane back to disabled.
    assert window.close_pane_action.isEnabled() is False


def test_focus_on_toolbar_does_not_change_active_pane(window, tmp_dir, qapp):
    """Focusing a window-level widget (e.g. the toolbar combo) must
    preserve whichever pane was last active. This locks in the model
    described in the FileManagerWindow docstring."""
    window._update_path(str(tmp_dir))
    new_pane = window._split_right()
    assert window._active_pane is new_pane

    # Simulate focus moving to the toolbar's view_combo. The window
    # listens to QApplication.focusChanged; firing the slot directly is
    # the deterministic way to test routing without a real event loop.
    window._on_focus_changed(new_pane.file_list, window.view_combo)
    assert window._active_pane is new_pane, (
        "Active pane must not change when a non-pane widget gets focus"
    )


def test_close_pane_disconnects_signals(window, tmp_dir):
    """Closing a pane must disconnect its signals so the window doesn't keep
    stale lambdas alive."""
    window._update_path(str(tmp_dir))
    new_pane = window._split_right()
    # All three signals should be live before close.
    assert new_pane.receivers(new_pane.focused) > 0
    assert new_pane.receivers(new_pane.path_changed) > 0
    assert new_pane.receivers(new_pane.status_changed) > 0
    window._close_pane()
    # After close the pane object may linger briefly under deleteLater,
    # but its signal connections must be gone.
    assert new_pane.receivers(new_pane.focused) == 0
    assert new_pane.receivers(new_pane.path_changed) == 0
    assert new_pane.receivers(new_pane.status_changed) == 0


def test_close_pane_with_only_one_is_noop(window):
    """The last pane is never closeable."""
    panes_before = window._split_root.find_panes()
    window._close_pane()
    panes_after = window._split_root.find_panes()
    assert panes_before == panes_after


def test_toolbar_actions_route_to_active_pane(window, tmp_dir, tmp_path):
    """Toolbar widgets operate on whichever pane is active."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "marker.txt").write_text("x")

    window._update_path(str(tmp_dir))
    new_pane = window._split_right()
    # Active pane is the new one; navigate it elsewhere.
    new_pane._update_path(str(other))
    # The other (original) pane stays at tmp_dir.
    panes = window._split_root.find_panes()
    paths = {p.current_path for p in panes}
    assert paths == {str(tmp_dir), str(other)}

    # Toolbar hidden-check applied to the active pane only.
    window.hidden_check.setChecked(True)
    window._toggle_hidden(True)
    names = [
        new_pane.file_list.item(i).text() for i in range(new_pane.file_list.count())
    ]
    # `other` has only marker.txt and no hidden file; toggle just shouldn't break.
    assert "marker.txt" in names


def test_split_broadcasts_plugin_manager(window, tmp_dir):
    """Plugins attached to the window must apply to panes created by splits."""

    class OnlyMarkdown:
        name = "only_md"
        capabilities = ["file_filter"]

        def filter_files(self, paths):
            return [p for p in paths if p.endswith(".md")]

    from unittest.mock import MagicMock

    mock_pm = MagicMock()
    mock_pm.get_file_filters.return_value = [OnlyMarkdown()]
    mock_pm.get_navigation_hooks.return_value = []
    window.set_plugin_manager(mock_pm)
    window._update_path(str(tmp_dir))

    new_pane = window._split_right()
    new_pane._update_path(str(tmp_dir))
    names = [
        new_pane.file_list.item(i).text() for i in range(new_pane.file_list.count())
    ]
    assert names == ["file3.md"], f"Filter not applied to new pane; got {names}"


def test_apply_preferences_applies_to_all_panes(window, tmp_dir, tmp_path, monkeypatch):
    from qfileman import config as config_mod
    from qfileman.config import Config

    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    Config._data = None
    cfg = Config()
    cfg.set("general", "show_hidden", True)
    cfg.set("general", "default_view", "grid")

    window._update_path(str(tmp_dir))
    new_pane = window._split_right()
    new_pane._update_path(str(tmp_dir))

    window._apply_preferences()

    for pane in window._split_root.find_panes():
        names = [pane.file_list.item(i).text() for i in range(pane.file_list.count())]
        assert ".hidden" in names, f"hidden toggle not applied to pane {pane}"
        from PyQt6.QtWidgets import QListWidget

        assert pane.file_list.viewMode() == QListWidget.ViewMode.IconMode

    Config._instance = None
    Config._data = None


def test_history_truncation(window, tmp_path):
    """Test history is truncated when navigating after going back."""
    # Create a path structure
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir3 = tmp_path / "dir3"
    dir1.mkdir()
    dir2.mkdir()
    dir3.mkdir()

    # Navigate: dir1 -> dir2 -> dir3
    window._update_path(str(dir1))
    window._update_path(str(dir2))
    window._update_path(str(dir3))

    # Go back to dir2
    window._go_back()

    # Navigate to a new directory
    window._update_path(str(dir1))

    # Forward history to dir3 should be truncated
    # Now at dir1, can't go forward to dir2 or dir3
    window._go_forward()
    # Should stay at dir1 since forward history was truncated
    assert window.current_path == str(dir1)


# --- copy/move destination clobber guards ------------------------------------


def _select(window, name):
    window._update_path(window.current_path)
    for i in range(window.file_list.count()):
        if window.file_list.item(i).text() == name:
            window.file_list.setCurrentRow(i)
            return
    raise AssertionError(f"{name!r} not in list")


def test_copy_fallback_prompts_and_aborts_on_no(window, tmp_dir):
    """No rsync: copy onto an existing file must confirm; 'No' preserves it."""
    src = tmp_dir / "src.txt"
    src.write_text("source")
    victim = tmp_dir / "dst.txt"
    victim.write_text("victim")
    window._update_path(str(tmp_dir))
    _select(window, "src.txt")
    with patch("shutil.which", return_value=None), \
            patch("PyQt6.QtWidgets.QInputDialog.getText",
                  return_value=(str(victim), True)), \
            patch("qfileman.pane.QMessageBox.question",
                  return_value=QMessageBox.StandardButton.No) as q:
        window._copy_or_move(move=False)
    q.assert_called_once()
    assert victim.read_text() == "victim"
    assert src.read_text() == "source"


def test_copy_fallback_overwrites_on_yes(window, tmp_dir, qtbot):
    src = tmp_dir / "src.txt"
    src.write_text("source")
    victim = tmp_dir / "dst.txt"
    victim.write_text("victim")
    window._update_path(str(tmp_dir))
    _select(window, "src.txt")
    # Spy on the launching source pane's _refresh: an overwrite doesn't change
    # the visible item names, so the only proof the queued GUI-thread refresh
    # callback fired is that _refresh was actually invoked after completion.
    source_pane = window._active_pane
    real_refresh = source_pane._refresh
    refresh_calls = {"n": 0}

    def counting_refresh():
        refresh_calls["n"] += 1
        return real_refresh()

    source_pane._refresh = counting_refresh
    calls_before = refresh_calls["n"]
    with patch("shutil.which", return_value=None), \
            patch("PyQt6.QtWidgets.QInputDialog.getText",
                  return_value=(str(victim), True)), \
            patch("qfileman.pane.QMessageBox.question",
                  return_value=QMessageBox.StandardButton.Yes):
        window._copy_or_move(move=False)
        # The shutil fallback now runs off the GUI thread and the source pane
        # refresh is queued back on completion. Wait for the filesystem side
        # effect, then for the queued GUI-thread refresh callback to fire.
        qtbot.waitUntil(lambda: victim.read_text() == "source", timeout=5000)
        qtbot.waitUntil(
            lambda: refresh_calls["n"] > calls_before, timeout=5000
        )
    assert refresh_calls["n"] > calls_before, \
        "completion refresh callback did not run on the source pane"
    assert victim.read_text() == "source"
    names = {window.file_list.item(i).text() for i in range(window.file_list.count())}
    assert {"src.txt", "dst.txt"}.issubset(names)


def test_move_fallback_prompts_and_aborts_on_no(window, tmp_dir):
    src = tmp_dir / "src.txt"
    src.write_text("source")
    victim = tmp_dir / "dst.txt"
    victim.write_text("victim")
    window._update_path(str(tmp_dir))
    _select(window, "src.txt")
    with patch("shutil.which", return_value=None), \
            patch("PyQt6.QtWidgets.QInputDialog.getText",
                  return_value=(str(victim), True)), \
            patch("qfileman.pane.QMessageBox.question",
                  return_value=QMessageBox.StandardButton.No):
        window._copy_or_move(move=True)
    assert src.read_text() == "source"
    assert victim.read_text() == "victim"


def test_copy_to_new_dest_does_not_prompt(window, tmp_dir, qtbot):
    """No existing destination → no overwrite prompt, copy proceeds."""
    src = tmp_dir / "src.txt"
    src.write_text("source")
    dest = tmp_dir / "fresh.txt"
    window._update_path(str(tmp_dir))
    _select(window, "src.txt")
    with patch("shutil.which", return_value=None), \
            patch("PyQt6.QtWidgets.QInputDialog.getText",
                  return_value=(str(dest), True)), \
            patch("qfileman.pane.QMessageBox.question") as q:
        window._copy_or_move(move=False)
        # The shutil fallback now runs off the GUI thread and the source pane
        # refresh is queued back on completion; wait for the filesystem side
        # effect *and* the new entry to surface in the (refreshed) list.
        qtbot.waitUntil(
            lambda: dest.exists() and dest.read_text() == "source"
            and "fresh.txt" in [
                window.file_list.item(i).text()
                for i in range(window.file_list.count())
            ],
            timeout=5000,
        )
    q.assert_not_called()
    assert dest.read_text() == "source"
    names = [window.file_list.item(i).text() for i in range(window.file_list.count())]
    assert "fresh.txt" in names


def test_copy_rsync_path_blocked_by_decline(window, tmp_dir):
    """With rsync available, declining the overwrite prompt must not invoke
    the rsync runner at all."""
    src = tmp_dir / "src.txt"
    src.write_text("source")
    victim = tmp_dir / "dst.txt"
    victim.write_text("victim")
    window._update_path(str(tmp_dir))
    _select(window, "src.txt")
    with patch("shutil.which", return_value="/usr/bin/rsync"), \
            patch("PyQt6.QtWidgets.QInputDialog.getText",
                  return_value=(str(victim), True)), \
            patch("qfileman.pane.QMessageBox.question",
                  return_value=QMessageBox.StandardButton.No), \
            patch("qfileman.plugins.builtin._runner.run_command_dialog") as run:
        window._copy_or_move(move=False)
    run.assert_not_called()
    assert victim.read_text() == "victim"


def test_copy_rsync_dir_child_collision_prompts(window, tmp_dir):
    """rsync copy of a directory whose child collides with an existing entry
    in the destination must prompt, even though dest/srcdir doesn't exist."""
    srcdir = tmp_dir / "srcdir"
    srcdir.mkdir()
    (srcdir / "shared.txt").write_text("new")
    dest = tmp_dir / "dest"
    dest.mkdir()
    (dest / "shared.txt").write_text("victim")
    window._update_path(str(tmp_dir))
    _select(window, "srcdir")
    with patch("shutil.which", return_value="/usr/bin/rsync"), \
            patch("PyQt6.QtWidgets.QInputDialog.getText",
                  return_value=(str(dest), True)), \
            patch("qfileman.pane.QMessageBox.question",
                  return_value=QMessageBox.StandardButton.No) as q, \
            patch("qfileman.plugins.builtin._runner.run_command_dialog") as run:
        window._copy_or_move(move=False)
    q.assert_called_once()
    run.assert_not_called()
    assert (dest / "shared.txt").read_text() == "victim"
