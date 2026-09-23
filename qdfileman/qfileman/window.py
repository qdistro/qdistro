"""Main window for QFileMan.

The window is a thin shell around an active :class:`FilePane`:

  - Menus + toolbar + sidebar tree + status bar live here.
  - The central area is a horizontal splitter:
    ``[ tree_view | SplitContainer( FilePane ... ) ]``.
  - At any moment exactly one pane is "active"; toolbar and menu
    actions operate on it.
  - The active pane changes only when focus moves *into* a FilePane (or
    one of its descendants). Focusing the window's toolbar, menu bar,
    tree view, or status bar does **not** re-elect a different pane —
    it preserves the last-focused one. That's intentional so the user
    can click a toolbar control without worrying about which pane it
    will affect.
  - For backwards compatibility with the pre-split test suite, the
    common pane accessors (``file_list``, ``path_edit``, ``current_path``,
    ``hidden_check``, ``_model``, history, etc.) are exposed as properties
    that read from / write through the active pane.
"""

from __future__ import annotations

import logging
import os

from PyQt6.QtCore import QDir, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QFileSystemModel
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from qfileman.pane import FilePane
from qfileman.split_container import SplitContainer

log = logging.getLogger(__name__)


class FileManagerWindow(QMainWindow):
    """Main file manager window with optional split panes."""

    active_pane_changed = pyqtSignal(object)  # FilePane | None

    def __init__(self) -> None:
        super().__init__()
        self._plugin_manager = None
        self._active_pane: FilePane | None = None
        self._init_ui()
        # Track focus app-wide so we can re-elect the active pane.
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)

    # ---------------------------------------------------------------- UI
    def _init_ui(self) -> None:
        self.setWindowTitle("QFileMan")
        self.setGeometry(100, 100, 900, 600)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self._create_toolbar()

        # Status bar — created before any pane so pane signals have a target.
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Ready")
        self.status_bar.addWidget(self.status_label)

        self._create_menu_bar()

        # Central horizontal splitter: [ tree | panes ]
        h_split = QSplitter(Qt.Orientation.Horizontal)
        self.tree_view = QTreeView()
        self.tree_view.header().setVisible(True)
        self.tree_view.doubleClicked.connect(self._on_tree_double_click)
        h_split.addWidget(self.tree_view)

        self._init_file_system_tree()

        self._split_root = SplitContainer(Qt.Orientation.Horizontal)
        initial_pane = self._make_pane()
        self._split_root.add_pane(initial_pane)
        h_split.addWidget(self._split_root)

        h_split.setStretchFactor(0, 1)
        h_split.setStretchFactor(1, 3)
        layout.addWidget(h_split)

        self._set_active_pane(initial_pane)

    def _create_toolbar(self) -> None:
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        back_btn = QPushButton("←")
        back_btn.setToolTip("Go back")
        back_btn.clicked.connect(self._go_back)
        toolbar.addWidget(back_btn)

        forward_btn = QPushButton("→")
        forward_btn.setToolTip("Go forward")
        forward_btn.clicked.connect(self._go_forward)
        toolbar.addWidget(forward_btn)

        up_btn = QPushButton("↑")
        up_btn.setToolTip("Go up one level")
        up_btn.clicked.connect(self._go_up)
        toolbar.addWidget(up_btn)

        refresh_btn = QPushButton("↻")
        refresh_btn.setToolTip("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        toolbar.addWidget(refresh_btn)

        toolbar.addSeparator()

        self.view_combo = QComboBox()
        self.view_combo.addItems(["List", "Grid"])
        self.view_combo.currentTextChanged.connect(self._change_view)
        toolbar.addWidget(self.view_combo)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Name", "Size", "Date", "Type"])
        self.sort_combo.currentTextChanged.connect(self._change_sort)
        toolbar.addWidget(self.sort_combo)

        toolbar.addSeparator()

        self.hidden_check = QCheckBox("Hidden")
        self.hidden_check.stateChanged.connect(self._toggle_hidden)
        toolbar.addWidget(self.hidden_check)

    def _create_menu_bar(self) -> None:
        """Populate the menubar and wire the Norton/Total-Commander key map.

        The function-key bindings follow the long-standing Norton →
        Total Commander → Krusader convention so muscle memory carries
        over: F3=View, F4=Edit, F5=Copy, F6=Move, F7=NewDir, F8=Delete,
        Alt+F5=Pack, Alt+F6=Unpack, Alt+F7=Find, Tab=switch pane,
        Backspace=parent. Modern Qt/GTK conventions (Ctrl+Q quit,
        Ctrl+F find, Ctrl+W close, Ctrl+, preferences, F2 rename)
        coexist as additional shortcuts on the same actions.
        """
        menubar = self.menuBar()

        # ---------------------------- File menu ----------------------------
        file_menu = menubar.addMenu("&File")

        new_folder_action = QAction("New &Folder", self)
        new_folder_action.setShortcuts(["F7", "Ctrl+Shift+N"])
        new_folder_action.triggered.connect(self._new_folder)
        file_menu.addAction(new_folder_action)

        new_file_action = QAction("New &Text File…", self)
        new_file_action.setShortcut("Shift+F4")
        new_file_action.triggered.connect(self._new_file)
        file_menu.addAction(new_file_action)

        open_action = QAction("&Open", self)
        open_action.setShortcut("Return")
        open_action.triggered.connect(self._open_selected)
        file_menu.addAction(open_action)

        view_action = QAction("&View", self)
        view_action.setShortcut("F3")
        view_action.triggered.connect(self._quick_view)
        file_menu.addAction(view_action)

        edit_action = QAction("&Edit", self)
        edit_action.setShortcut("F4")
        edit_action.triggered.connect(self._edit_selected)
        file_menu.addAction(edit_action)

        file_menu.addSeparator()

        copy_action = QAction("&Copy…", self)
        copy_action.setShortcut("F5")
        copy_action.triggered.connect(self._copy_selected)
        file_menu.addAction(copy_action)

        move_action = QAction("&Move…", self)
        move_action.setShortcut("F6")
        move_action.triggered.connect(self._move_selected)
        file_menu.addAction(move_action)

        rename_action = QAction("&Rename", self)
        # F2 = modern convention; Shift+F6 = Total Commander.
        rename_action.setShortcuts(["F2", "Shift+F6"])
        rename_action.triggered.connect(self._rename)
        file_menu.addAction(rename_action)

        delete_action = QAction("&Delete", self)
        delete_action.setShortcuts(["Delete", "F8"])
        delete_action.triggered.connect(self._delete)
        file_menu.addAction(delete_action)

        file_menu.addSeparator()

        pack_action = QAction("&Pack… (archive)", self)
        pack_action.setShortcut("Alt+F5")
        pack_action.triggered.connect(self._pack_selected)
        file_menu.addAction(pack_action)

        unpack_action = QAction("&Unpack… (extract)", self)
        unpack_action.setShortcut("Alt+F6")
        unpack_action.triggered.connect(self._unpack_selected)
        file_menu.addAction(unpack_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        # F10 mirrors Norton/TC; Ctrl+Q is the GTK/Qt convention.
        quit_action.setShortcuts(["Ctrl+Q", "F10"])
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # ---------------------------- Edit menu ----------------------------
        edit_menu = menubar.addMenu("&Edit")

        find_action = QAction("&Find…", self)
        find_action.setShortcuts(["Ctrl+F", "Alt+F7"])
        find_action.triggered.connect(self._open_search_dialog)
        edit_menu.addAction(find_action)

        edit_menu.addSeparator()

        # qdistro Send-To — lazy-populated so the menu reflects the
        # live broker view of registered peer apps.
        self._send_to_menu = edit_menu.addMenu("Send &To")
        self._send_to_menu.aboutToShow.connect(self._populate_send_to_menu)

        edit_menu.addSeparator()

        prefs_action = QAction("&Preferences…", self)
        prefs_action.setShortcut("Ctrl+,")
        prefs_action.triggered.connect(self._open_preferences)
        edit_menu.addAction(prefs_action)

        # ---------------------------- View menu ----------------------------
        # The "Show hidden" toggle is owned by the toolbar checkbox;
        # duplicating it as a checkable menu action led to drift between
        # the two and was removed.
        view_menu = menubar.addMenu("&View")

        refresh_action = QAction("&Refresh", self)
        # Ctrl+R = Total Commander's "re-read source"; F2 was the
        # Norton refresh key but we use F2 for Rename per Windows
        # convention.
        refresh_action.setShortcuts(["Ctrl+R", "Shift+F5"])
        refresh_action.triggered.connect(self._refresh)
        view_menu.addAction(refresh_action)

        size_action = QAction("Folder &Size…", self)
        size_action.setShortcut("Ctrl+L")
        size_action.triggered.connect(self._show_folder_size)
        view_menu.addAction(size_action)

        view_menu.addSeparator()

        swap_action = QAction("S&wap Panes", self)
        swap_action.setShortcut("Ctrl+U")
        swap_action.triggered.connect(self._swap_panes)
        view_menu.addAction(swap_action)

        self.split_right_action = QAction("Split Right", self)
        self.split_right_action.setShortcut("Ctrl+Shift+L")
        self.split_right_action.triggered.connect(self._split_right)
        view_menu.addAction(self.split_right_action)
        self.split_down_action = QAction("Split Down", self)
        self.split_down_action.setShortcut("Ctrl+Shift+D")
        self.split_down_action.triggered.connect(self._split_down)
        view_menu.addAction(self.split_down_action)
        self.close_pane_action = QAction("Close Pane", self)
        self.close_pane_action.setShortcut("Ctrl+W")
        self.close_pane_action.triggered.connect(self._close_pane)
        view_menu.addAction(self.close_pane_action)

        # ---------------------------- Go menu ------------------------------
        go_menu = menubar.addMenu("&Go")
        home_action = QAction("&Home", self)
        home_action.setShortcut("Alt+Home")
        home_action.triggered.connect(self._go_home)
        go_menu.addAction(home_action)
        up_action = QAction("&Parent Directory", self)
        # Alt+Up keeps modern muscle memory; Backspace matches Norton/TC.
        up_action.setShortcuts(["Alt+Up", "Backspace"])
        up_action.triggered.connect(self._go_up)
        go_menu.addAction(up_action)
        switch_action = QAction("Switch &Pane", self)
        switch_action.setShortcut("Tab")
        switch_action.triggered.connect(self._switch_pane)
        go_menu.addAction(switch_action)

        # ---------------------------- Plugins menu --------------------------
        self.plugins_menu = menubar.addMenu("&Plugins")

        # ---------------------------- Help menu -----------------------------
        help_menu = menubar.addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.setShortcut("F1")
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _init_file_system_tree(self) -> None:
        self.fs_model = QFileSystemModel()
        self.fs_model.setRootPath("")
        self.tree_view.setModel(self.fs_model)
        self.tree_view.setRootIndex(self.fs_model.index(QDir.homePath()))
        self.tree_view.hideColumn(1)
        self.tree_view.hideColumn(2)
        self.tree_view.hideColumn(3)

    # ---------------------------------------------------- pane factory
    def _make_pane(self) -> FilePane:
        """Create a pane wired to this window's plugin manager and signals."""
        pane = FilePane()
        if self._plugin_manager is not None:
            pane.set_plugin_manager(self._plugin_manager)
        pane.status_changed.connect(self.status_label.setText)
        pane.path_changed.connect(self._on_pane_path_changed)
        pane.focused.connect(lambda p=pane: self._set_active_pane(p))
        return pane

    # ---------------------------------------------- active-pane tracking
    def _set_active_pane(self, pane: FilePane | None) -> None:
        """Make ``pane`` the active pane and sync toolbar widgets to it."""
        if pane is None or pane is self._active_pane:
            self._update_close_pane_enabled()
            return
        self._active_pane = pane
        # Sync toolbar widgets without re-triggering their slots.
        self._sync_toolbar_from_pane(pane)
        self._update_close_pane_enabled()
        # Echo current pane's path into the tree view's selection.
        if pane.current_path:
            idx = self.fs_model.index(pane.current_path)
            if idx.isValid():
                self.tree_view.setCurrentIndex(idx)
        self.active_pane_changed.emit(pane)

    def _sync_toolbar_from_pane(self, pane: FilePane) -> None:
        """Reflect ``pane``'s view-mode / sort / hidden state in the toolbar."""
        from PyQt6.QtWidgets import QListWidget

        # Block signals to avoid bouncing changes back into the pane.
        for w in (self.view_combo, self.sort_combo, self.hidden_check):
            w.blockSignals(True)
        try:
            view = (
                "List"
                if pane.file_list.viewMode() == QListWidget.ViewMode.ListMode
                else "Grid"
            )
            self.view_combo.setCurrentText(view)
            label = pane._model.sort_by.capitalize()
            if self.sort_combo.findText(label) >= 0:
                self.sort_combo.setCurrentText(label)
            self.hidden_check.setChecked(pane._model.show_hidden)
        finally:
            for w in (self.view_combo, self.sort_combo, self.hidden_check):
                w.blockSignals(False)

    def _update_close_pane_enabled(self) -> None:
        """Disable Close Pane when only one pane remains."""
        only_one = len(self._split_root.find_panes()) <= 1
        if hasattr(self, "close_pane_action"):
            self.close_pane_action.setEnabled(not only_one)

    def _on_focus_changed(self, _old, new) -> None:
        """Walk up the focus chain to find which FilePane (if any) owns ``new``."""
        widget = new
        while widget is not None:
            if isinstance(widget, FilePane):
                self._set_active_pane(widget)
                return
            widget = widget.parent()

    def _on_pane_path_changed(self, path: str) -> None:
        """Mirror the active pane's path into the tree view."""
        if self._active_pane is None or self.sender() is not self._active_pane:
            return
        idx = self.fs_model.index(path)
        if idx.isValid():
            self.tree_view.setCurrentIndex(idx)

    # --------------------------------------------------- split actions
    def _split_right(self) -> FilePane | None:
        return self._split(Qt.Orientation.Horizontal)

    def _split_down(self) -> FilePane | None:
        return self._split(Qt.Orientation.Vertical)

    def _split(self, orientation: Qt.Orientation) -> FilePane | None:
        if self._active_pane is None:
            return None
        starting_path = self._active_pane.current_path

        def factory() -> FilePane:
            pane = self._make_pane()
            if starting_path:
                pane._update_path(starting_path)
            return pane

        new_pane = self._split_root.split(self._active_pane, orientation, factory)
        if new_pane is not None:
            self._update_close_pane_enabled()
            self._set_active_pane(new_pane)
        return new_pane

    def _close_pane(self) -> None:
        """Close the active pane unless it's the only one left."""
        panes = self._split_root.find_panes()
        if self._active_pane is None or len(panes) <= 1:
            return
        target = self._active_pane
        # Pick a fallback active pane *before* removing.
        idx = panes.index(target)
        fallback = panes[idx - 1] if idx > 0 else panes[idx + 1]
        # Drop our signal connections before the pane is destroyed so we
        # don't keep stale lambdas alive in the QObject signal table.
        self._disconnect_pane_signals(target)
        self._split_root.remove_pane(target)
        self._active_pane = None
        self._set_active_pane(fallback)

    def _disconnect_pane_signals(self, pane: FilePane) -> None:
        """Disconnect every signal we wired up in ``_make_pane``."""
        for signal in (pane.status_changed, pane.path_changed, pane.focused):
            try:
                signal.disconnect()
            except TypeError:
                # No connections to disconnect — fine.
                pass

    # --------------------------------------- back-compat proxy methods
    def _update_path(self, path: str) -> None:
        if self._active_pane is not None:
            self._active_pane._update_path(path)

    def _load_files(self) -> None:
        if self._active_pane is not None:
            self._active_pane._load_files()

    def _refresh(self) -> None:
        if self._active_pane is not None:
            self._active_pane._refresh()

    def _go_back(self) -> None:
        if self._active_pane is not None:
            self._active_pane._go_back()

    def _go_forward(self) -> None:
        if self._active_pane is not None:
            self._active_pane._go_forward()

    def _go_up(self) -> None:
        if self._active_pane is not None:
            self._active_pane._go_up()

    def _go_home(self) -> None:
        if self._active_pane is not None:
            self._active_pane._go_home()

    def _change_view(self, view_type: str) -> None:
        if self._active_pane is not None:
            self._active_pane._change_view(view_type)

    def _change_sort(self, sort_by: str) -> None:
        if self._active_pane is not None:
            self._active_pane._change_sort(sort_by)

    def _toggle_hidden(self, _state=None) -> None:
        if self._active_pane is not None:
            self._active_pane._toggle_hidden(self.hidden_check.isChecked())

    def _new_folder(self) -> None:
        if self._active_pane is not None:
            self._active_pane._new_folder()

    def _open_selected(self) -> None:
        if self._active_pane is not None:
            self._active_pane._open_selected()

    def _rename(self) -> None:
        if self._active_pane is not None:
            self._active_pane._rename()

    def _delete(self) -> None:
        if self._active_pane is not None:
            self._active_pane._delete()

    def _show_context_menu(self, position) -> None:
        if self._active_pane is not None:
            self._active_pane._show_context_menu(position)

    def _on_file_click(self, item) -> None:
        if self._active_pane is not None:
            self._active_pane._on_file_click(item)

    def _on_file_double_click(self, item) -> None:
        if self._active_pane is not None:
            self._active_pane._on_file_double_click(item)

    def _on_path_enter(self) -> None:
        if self._active_pane is not None:
            self._active_pane._on_path_enter()

    def _on_tree_double_click(self, index) -> None:
        path = self.fs_model.filePath(index)
        if os.path.isdir(path):
            self._update_path(path)

    # ------------------------------------------ Total-Commander actions
    def _selected_path(self) -> str | None:
        """Return the currently-selected entry's path in the active pane."""
        if self._active_pane is None:
            return None
        item = self._active_pane.file_list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data.get("path") if data else None

    def _other_pane_path(self) -> str:
        """Return the cwd of the next pane after the active one, or ~ if none."""
        panes = self._split_root.find_panes()
        if not panes or self._active_pane is None:
            return os.path.expanduser("~")
        try:
            idx = panes.index(self._active_pane)
        except ValueError:
            return os.path.expanduser("~")
        target = panes[(idx + 1) % len(panes)]
        return target.current_path or os.path.expanduser("~")

    def _quick_view(self) -> None:
        """F3 — Quick View on the selected file via the embedded_viewer plugin."""
        path = self._selected_path()
        if not path or not os.path.isfile(path):
            return
        if self._plugin_manager is None:
            return
        plugin = self._plugin_manager.load("embedded_viewer")
        if plugin is None:
            return
        # The plugin exposes its viewer through a private method; we
        # call it directly rather than going through the menu indirection.
        plugin._view(path)

    def _edit_selected(self) -> None:
        """F4 — Open the selected file in ``$EDITOR``, else ``xdg-open``."""
        path = self._selected_path()
        if not path or not os.path.isfile(path):
            return
        import shutil
        import subprocess
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if editor and shutil.which(editor.split()[0]):
            try:
                subprocess.Popen([*editor.split(), path])
                return
            except OSError as e:
                log.warning("editor launch failed: %s", e)
        if shutil.which("xdg-open"):
            try:
                subprocess.Popen(["xdg-open", path])
            except OSError as e:
                log.warning("xdg-open failed: %s", e)

    def _copy_selected(self) -> None:
        """F5 — Copy the selected entry to a chosen destination directory."""
        self._copy_or_move(move=False)

    def _move_selected(self) -> None:
        """F6 — Move the selected entry to a chosen destination directory."""
        self._copy_or_move(move=True)

    def _copy_or_move(self, *, move: bool) -> None:
        """Route F5/F6 through the rsync runner so we get live progress
        in the qdshell notification on every copy/move — the same wire
        the rsync_sync plugin uses. Falls back to ``shutil`` when
        ``rsync`` isn't on PATH (the menu hides itself otherwise but
        the keyboard shortcut would still fire)."""
        import shutil

        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        path = self._selected_path()
        if not path:
            return
        default_dest = os.path.join(
            self._other_pane_path(), os.path.basename(path)
        )
        title = "Move" if move else "Copy"
        dest, ok = QInputDialog.getText(
            self, f"{title} — {os.path.basename(path)}",
            f"{title} to:", text=default_dest,
        )
        if not ok or not dest.strip():
            return
        dest = dest.strip()

        # rsync, shutil.move and shutil.copy2 all silently overwrite an
        # existing destination. The rsync branch and the shutil fallback have
        # *different* placement semantics for a directory source (rsync gets a
        # trailing slash and merges contents straight into dest; shutil drops
        # the whole tree at dest/basename), so resolve the real conflict against
        # the branch we're about to take and confirm before clobbering.
        from qfileman.file_model import copy_move_conflict
        use_rsync = bool(shutil.which("rsync"))
        conflict = copy_move_conflict(path, dest, rsync=use_rsync)
        if conflict is not None:
            reply = QMessageBox.question(
                self, f"{title} — Overwrite?",
                f"'{conflict}' already exists at the destination. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        if use_rsync:
            from qfileman.plugins.builtin._runner import run_command_dialog
            from qfileman.plugins.builtin.rsync_sync import rsync_argv
            # rsync needs a trailing slash on a source directory to
            # mean "the contents of"; without it we'd nest src/ inside
            # dest/src/, which is not the copy semantics F5 implies.
            source = path + "/" if os.path.isdir(path) else path
            argv = rsync_argv(source, dest, move=move)
            run_command_dialog(f"{title} {os.path.basename(path)}", argv)
            self._refresh()
            return

        # No rsync: do the shutil copy/move off the GUI thread (a large
        # tree would otherwise freeze the UI for the whole operation). The
        # overwrite prompt / clobber guard above already ran synchronously;
        # only the blocking filesystem call goes to the worker. We reuse the
        # project's ProgressRunner — the same off-thread mechanism the
        # folder-size and checksum call sites use.
        from qfileman.worker import ProgressRunner

        label = f"{title} {os.path.basename(path)}…"
        # Capture the launching pane and the affected destination directory on
        # the GUI thread now: the op is async, so the active pane may differ by
        # the time on_result fires. shutil drops the source at ``dest``, so the
        # destination view that changes is its parent directory.
        source_pane = self._active_pane
        # shutil places the source *inside* ``dest`` when ``dest`` is an
        # existing directory, so the file actually lands at the effective
        # target (``dest`` itself, or ``dest/basename`` when ``dest`` is a
        # directory). The visible view that changes is that target's parent.
        from qfileman.file_model import effective_copy_target
        final_target = effective_copy_target(path, dest)
        dest_dir = os.path.dirname(os.path.abspath(str(final_target)))

        def work(cancel, progress):
            progress(0, -1, label)
            cancel.raise_if_cancelled()
            if move:
                shutil.move(path, dest)
            elif os.path.isdir(path):
                shutil.copytree(path, dest)
            else:
                shutil.copy2(path, dest)

        def on_result(_value) -> None:
            # Refresh the pane that launched the op (if it still exists) plus
            # any pane currently showing the destination directory — a
            # successful copy/move changes both the source and target views.
            panes = self._split_root.find_panes()
            for pane in panes:
                if pane is source_pane or (
                    os.path.abspath(pane.current_path) == dest_dir
                ):
                    pane._refresh()

        def on_error(message: str) -> None:
            QMessageBox.warning(self, title, f"{title} failed: {message}")

        runner = ProgressRunner(
            work, title=title, label=label, parent=self,
            on_result=on_result,
            on_error=on_error,
        )
        # Keep the runner alive until the worker thread finishes; clear the
        # reference once it's done so the attribute doesn't dangle at a
        # deleted runner.
        self._copy_move_runner = runner
        runner._worker.finished.connect(
            lambda: setattr(self, "_copy_move_runner", None)
        )
        runner.start()

    def _new_file(self) -> None:
        """Shift+F4 — Create an empty file and open it for editing."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        if self._active_pane is None:
            return
        name, ok = QInputDialog.getText(self, "New Text File", "Filename:")
        if not (ok and name.strip()):
            return
        target = os.path.join(self._active_pane.current_path, name.strip())
        try:
            # Use O_CREAT|O_EXCL so we never clobber an existing file.
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
        except OSError as e:
            QMessageBox.warning(self, "New Text File", f"Create failed: {e}")
            return
        self._refresh()
        # Drop straight into the editor — that's the Norton/TC reflex.
        try:
            self._active_pane._select_path(target)
        except AttributeError:
            pass
        self._edit_selected()

    def _pack_selected(self) -> None:
        """Alt+F5 — Hand the selected entry to the archive plugin's creator."""
        path = self._selected_path()
        if not path or self._plugin_manager is None:
            return
        plugin = self._plugin_manager.load("archive")
        if plugin is None:
            return
        plugin._create_archive(path)

    def _unpack_selected(self) -> None:
        """Alt+F6 — Extract the selected archive via the archive plugin."""
        path = self._selected_path()
        if not path or self._plugin_manager is None:
            return
        plugin = self._plugin_manager.load("archive")
        if plugin is None:
            return
        plugin._extract_to(path)

    def _show_folder_size(self) -> None:
        """Ctrl+L — Folder size of the active pane's current directory."""
        if self._active_pane is None or self._plugin_manager is None:
            return
        plugin = self._plugin_manager.load("folder_size")
        if plugin is None:
            return
        plugin._show(self._active_pane.current_path)

    def _swap_panes(self) -> None:
        """Ctrl+U — Swap the cwd of the active pane with the next one."""
        panes = self._split_root.find_panes()
        if len(panes) < 2 or self._active_pane is None:
            return
        try:
            idx = panes.index(self._active_pane)
        except ValueError:
            return
        other = panes[(idx + 1) % len(panes)]
        a_path = self._active_pane.current_path
        b_path = other.current_path
        if a_path and b_path:
            self._active_pane._update_path(b_path)
            other._update_path(a_path)

    def _switch_pane(self) -> None:
        """Tab — Cycle keyboard focus through the panes."""
        panes = self._split_root.find_panes()
        if len(panes) < 2 or self._active_pane is None:
            return
        try:
            idx = panes.index(self._active_pane)
        except ValueError:
            idx = -1
        target = panes[(idx + 1) % len(panes)]
        target.file_list.setFocus()
        self._set_active_pane(target)

    def _show_about(self) -> None:
        """F1 — About dialog."""
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.about(
            self, "About QFileMan",
            "<b>QFileMan</b><br><br>"
            "A dual-pane file manager with a plugin system inspired by "
            "Total Commander, Krusader, and Double Commander.<br><br>"
            "Function-key bindings follow the Norton / TC convention "
            "(F3 View, F4 Edit, F5 Copy, F6 Move, F7 NewDir, F8 Delete, "
            "Alt+F5 Pack, Alt+F6 Unpack, Alt+F7 Find).",
        )

    # ------------------------------------------------ pane-backed props
    @property
    def file_list(self):
        return None if self._active_pane is None else self._active_pane.file_list

    @property
    def path_edit(self):
        return None if self._active_pane is None else self._active_pane.path_edit

    @property
    def current_path(self) -> str:
        return "" if self._active_pane is None else self._active_pane.current_path

    @current_path.setter
    def current_path(self, value: str) -> None:
        if self._active_pane is not None:
            self._active_pane.current_path = value

    @property
    def _model(self):
        return None if self._active_pane is None else self._active_pane._model

    @property
    def _history(self):
        return [] if self._active_pane is None else self._active_pane._history

    @_history.setter
    def _history(self, value) -> None:
        if self._active_pane is not None:
            self._active_pane._history = value

    @property
    def _history_index(self) -> int:
        return -1 if self._active_pane is None else self._active_pane._history_index

    @_history_index.setter
    def _history_index(self, value: int) -> None:
        if self._active_pane is not None:
            self._active_pane._history_index = value

    # ---------------------------------------------------- Find/Prefs
    def _open_search_dialog(self) -> None:
        from qfileman.search_dialog import SearchDialog

        if self._active_pane is None:
            return
        dlg = SearchDialog(
            self._active_pane.current_path,
            parent=self,
            show_hidden=self.hidden_check.isChecked(),
        )
        dlg.path_chosen.connect(self._navigate_to_result)
        dlg.exec()

    def _navigate_to_result(self, path: str) -> None:
        target = os.path.dirname(path) if not os.path.isdir(path) else path
        if not target:
            return
        self._update_path(target)
        basename = os.path.basename(path)
        fl = self.file_list
        if fl is None:
            return
        for i in range(fl.count()):
            if fl.item(i).text() == basename:
                fl.setCurrentRow(i)
                break

    def _populate_send_to_menu(self) -> None:
        """Lazy-fill the qdistro Send-To submenu from the broker.

        Payload = full contents of the currently-selected file (UTF-8
        decoded, capped at 1 MiB to stay below the broker's detail
        sanitiser budget). The cap is conservative — for binaries the
        receiving app gets only the readable prefix; users who need
        full-fidelity binary transfer should use the file manager's
        copy-to-pane operation, not Send-To.
        """
        self._send_to_menu.clear()
        try:
            from qfileman import qdistro_integration as _qi
        except ImportError:
            act = self._send_to_menu.addAction("(qdistro SDK not available)")
            act.setEnabled(False)
            return
        payload = self._collect_send_to_payload()
        targets = _qi.send_to_targets(kind="text/plain")
        if not targets:
            act = self._send_to_menu.addAction("(no receivers running)")
            act.setEnabled(False)
            return
        for row in targets:
            label = row["name"]
            silo = row.get("silo") or ""
            if silo:
                label = f"{label}  [{silo}]"
            act = self._send_to_menu.addAction(label)
            if not payload:
                act.setEnabled(False)
                act.setToolTip("Select a readable file first")
            else:
                uid = int(row["uid"])
                svc = str(row["service"])
                act.triggered.connect(
                    lambda _checked=False, u=uid, s=svc, p=payload:
                        _qi.send_payload(u, s, p, kind="text/plain"))

    def _collect_send_to_payload(self) -> str:
        pane = self._active_pane
        if pane is None:
            return ""
        try:
            selected = pane.selected_paths() if hasattr(pane, "selected_paths") else []
        except Exception:
            selected = []
        if not selected:
            return ""
        path = selected[0]
        try:
            with open(path, "rb") as fh:
                data = fh.read(1024 * 1024)
            return data.decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _open_preferences(self) -> None:
        from qfileman.config import Config
        from qfileman.preferences import PreferencesDialog

        dlg = PreferencesDialog(Config(), parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_preferences()

    def _apply_preferences(self) -> None:
        """Apply Config values to every pane, then resync the toolbar."""
        from qfileman.config import Config

        cfg = Config()
        show_hidden = bool(cfg.get("general", "show_hidden", default=False))
        sort_by = cfg.get("general", "sort_by", default="name")
        sort_order = cfg.get("general", "sort_order", default="asc")
        view = cfg.get("general", "default_view", default="list")
        view_label = "Grid" if view == "grid" else "List"

        for pane in self._split_root.find_panes():
            # apply_state mutates the FileModel and refreshes once, instead
            # of the two refreshes implied by set_show_hidden + set_sort.
            pane._model.apply_state(
                show_hidden=show_hidden,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            pane._change_view(view_label)
            pane._load_files()

        if self._active_pane is not None:
            self.hidden_check.setChecked(show_hidden)
            label = sort_by.capitalize()
            if self.sort_combo.findText(label) >= 0:
                self.sort_combo.setCurrentText(label)
            self.view_combo.setCurrentText(view_label)

    # ----------------------------------------------------- plugins
    def set_plugin_manager(self, pm) -> None:
        """Attach a plugin manager and broadcast file filters to all panes."""
        self._plugin_manager = pm
        self._sync_plugin_filters()

    def _sync_plugin_filters(self) -> None:
        for pane in self._split_root.find_panes():
            pane.set_plugin_manager(self._plugin_manager)

    def closeEvent(self, event):  # noqa: N802
        if self._plugin_manager:
            for name in self._plugin_manager.enabled_plugins():
                self._plugin_manager.disable(name)
        event.accept()
