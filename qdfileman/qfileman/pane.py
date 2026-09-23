"""A single self-contained file-manager pane.

A :class:`FilePane` owns its own ``FileModel``, navigation history, file
list widget, and path edit. It's a leaf widget in the split-container
tree; the window holds the toolbar/menus/tree-view and routes user
actions to whichever pane is currently active.
"""

from __future__ import annotations

import logging
import os
import subprocess

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qfileman.file_model import (
    FileModel,
    is_safe_rename_name,
)
from qfileman.file_model import (
    rename_exclusive as _rename_exclusive,
)

log = logging.getLogger(__name__)


class FilePane(QWidget):
    """One file-browsing pane.

    Emits:
      - ``path_changed(str)`` — after a successful navigation.
      - ``focused()``         — when the pane (or any descendant) takes focus.
      - ``status_changed(str)`` — when status text should be displayed by the
        host window (e.g. "12 items", "File: /tmp/foo (8 bytes)").
    """

    path_changed = pyqtSignal(str)
    focused = pyqtSignal()
    status_changed = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model = FileModel()
        self._plugin_manager = None
        self._history: list[str] = []
        self._history_index: int = -1
        self.current_path: str = ""

        self._build_ui()
        self._update_path("")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # Path bar
        path_layout = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setMinimumHeight(28)
        self.path_edit.returnPressed.connect(self._on_path_enter)
        path_layout.addWidget(self.path_edit, 1)
        home_btn = QPushButton("⌂")
        home_btn.setMaximumWidth(36)
        home_btn.setToolTip("Home")
        home_btn.clicked.connect(self._go_home)
        path_layout.addWidget(home_btn)
        layout.addLayout(path_layout)

        # File list
        self.file_list = QListWidget()
        self.file_list.setViewMode(QListWidget.ViewMode.ListMode)
        self.file_list.setIconSize(QSize(48, 48))
        self.file_list.itemDoubleClicked.connect(self._on_file_double_click)
        self.file_list.itemClicked.connect(self._on_file_click)
        self.file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.file_list, 1)

    # ------------------------------------------------------- plugin wiring
    def set_plugin_manager(self, pm) -> None:
        """Attach a plugin manager and re-sync file filters from it."""
        self._plugin_manager = pm
        self._sync_plugin_filters()

    def _sync_plugin_filters(self) -> None:
        self._model.clear_filters()
        if not self._plugin_manager:
            self._load_files()
            return
        for f in self._plugin_manager.get_file_filters():
            self._model.add_filter(f)
        self._load_files()

    # ----------------------------------------------------- focus tracking
    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        """Re-elect this pane as active on any mouse press inside its area.

        ``super().mousePressEvent`` is invoked for symmetry — QWidget's
        default does nothing, but if a future subclass overrides we still
        want to forward the event correctly. Child widgets (file_list,
        path_edit) get their press events before this method runs because
        Qt delivers events to the deepest widget under the cursor first.
        """
        self.focused.emit()
        super().mousePressEvent(event)

    # ---------------------------------------------------------- navigation
    def _update_path(self, path: str) -> None:
        """Navigate to ``path`` and refresh the listing.

        Plugin NavigationHooks may veto the move by returning ``False``
        from ``on_enter_directory``.
        """
        if not path:
            path = os.path.expanduser("~")

        if self._plugin_manager:
            for hook in self._plugin_manager.get_navigation_hooks():
                if hook.on_enter_directory(path) is False:
                    log.info("plugin %s blocked navigation to %s", hook.name, path)
                    return

        old_path = self.current_path

        if not self._model.set_path(path):
            log.warning("invalid path %s; ignoring navigation", path)
            return

        if old_path and self._plugin_manager:
            for hook in self._plugin_manager.get_navigation_hooks():
                hook.on_leave_directory(old_path)

        self.current_path = path
        self.path_edit.setText(path)

        if not self._history or self._history[self._history_index] != path:
            self._history = self._history[: self._history_index + 1]
            self._history.append(path)
            self._history_index = len(self._history) - 1

        self._load_files()
        self.path_changed.emit(path)

    def _load_files(self) -> None:
        """Render the FileModel's files into the list widget."""
        self.file_list.clear()
        for fi in self._model.get_files():
            item = QListWidgetItem(fi.name)
            item.setData(
                Qt.ItemDataRole.UserRole,
                {"path": str(fi.path), "is_dir": fi.is_dir},
            )
            self.file_list.addItem(item)
        self.status_changed.emit(f"{self.file_list.count()} items")

    def _refresh(self) -> None:
        self._model.refresh()
        self._load_files()

    def _go_back(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self._update_path(self._history[self._history_index])

    def _go_forward(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._update_path(self._history[self._history_index])

    def _go_up(self) -> None:
        parent = os.path.dirname(self.current_path)
        if parent and parent != self.current_path:
            self._update_path(parent)

    def _go_home(self) -> None:
        self._update_path(os.path.expanduser("~"))

    # --------------------------------------------- view / sort / hidden
    def _change_view(self, view_type: str) -> None:
        if view_type == "List":
            self.file_list.setViewMode(QListWidget.ViewMode.ListMode)
        else:
            self.file_list.setViewMode(QListWidget.ViewMode.IconMode)

    def _change_sort(self, sort_by: str) -> None:
        key = sort_by.lower()
        order = "desc" if key == "date" else "asc"
        self._model.set_sort(key, sort_order=order)
        self._load_files()

    def _toggle_hidden(self, show: bool) -> None:
        self._model.set_show_hidden(bool(show))
        self._load_files()

    # --------------------------------------------- file-list event slots
    def _on_path_enter(self) -> None:
        path = self.path_edit.text()
        if os.path.isdir(path):
            self._update_path(path)
        else:
            QMessageBox.warning(
                self, "Invalid Path", "The specified path does not exist."
            )

    def _on_file_click(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        path = data.get("path", "")
        if data.get("is_dir"):
            self.status_changed.emit(f"Directory: {path}")
        else:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            self.status_changed.emit(f"File: {path} ({size} bytes)")

    def _on_file_double_click(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        path = data.get("path", "")
        if not path:
            return
        if data.get("is_dir"):
            self._update_path(path)
        else:
            # Fire-and-forget: xdg-open hands the file to the system's
            # default app and we don't want the file-manager event loop
            # to block on its lifetime.
            try:
                subprocess.Popen(["xdg-open", path])
            except OSError as e:
                log.warning("xdg-open failed: %s", e)

    def _show_context_menu(self, position) -> None:
        item = self.file_list.itemAt(position)
        if not item:
            return

        menu = QMenu(self)
        data = item.data(Qt.ItemDataRole.UserRole)
        path = data.get("path", "") if data else ""

        if self._plugin_manager:
            for provider in self._plugin_manager.get_menu_providers():
                for label, callback in provider.get_menu_items(path):
                    action = menu.addAction(label)
                    action.triggered.connect(lambda c=callback, p=path: c(p))

        if data and data.get("is_dir"):
            open_action = QAction("Open", self)
            open_action.triggered.connect(lambda: self._update_path(path))
            menu.addAction(open_action)

        rename_action = QAction("Rename", self)
        rename_action.triggered.connect(self._rename)
        menu.addAction(rename_action)

        delete_action = QAction("Move to Trash", self)
        delete_action.triggered.connect(self._delete)
        menu.addAction(delete_action)

        copy_path_action = QAction("Copy Path", self)
        copy_path_action.triggered.connect(
            lambda: QApplication.clipboard().setText(path)
        )
        menu.addAction(copy_path_action)

        menu.exec(self.file_list.mapToGlobal(position))

    # --------------------------------------------------- file operations
    def _new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not (ok and name):
            return
        try:
            os.makedirs(os.path.join(self.current_path, name))
            self._refresh()
        except OSError as e:
            QMessageBox.warning(self, "Error", f"Could not create folder: {e}")

    def _open_selected(self) -> None:
        item = self.file_list.currentItem()
        if item:
            self._on_file_double_click(item)

    def _rename(self) -> None:
        item = self.file_list.currentItem()
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        old_path = data.get("path", "") if data else ""
        old_name = os.path.basename(old_path)

        new_name, ok = QInputDialog.getText(
            self, "Rename", "New name:", text=old_name
        )
        if not (ok and new_name) or new_name == old_name:
            return
        if not is_safe_rename_name(new_name):
            QMessageBox.warning(
                self, "Error",
                "Rename target must be a single file name."
            )
            return
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        # POSIX rename silently replaces an existing destination. Attempt an
        # atomic no-clobber rename first; only if it reports the name is taken
        # do we prompt, then retry with overwrite=True on confirmation. Doing
        # the check inside the rename syscall (rather than a separate stat)
        # closes the TOCTOU window a plain os.rename would leave open.
        try:
            _rename_exclusive(old_path, new_path)
        except FileExistsError:
            reply = QMessageBox.question(
                self, "Overwrite?",
                f"'{new_name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            try:
                _rename_exclusive(old_path, new_path, overwrite=True)
            except OSError as e:
                QMessageBox.warning(self, "Error", f"Could not rename: {e}")
                return
        except OSError as e:
            QMessageBox.warning(self, "Error", f"Could not rename: {e}")
            return
        self._refresh()

    def _delete(self) -> None:
        item = self.file_list.currentItem()
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        path = data.get("path", "") if data else ""
        name = os.path.basename(path)

        reply = QMessageBox.question(
            self,
            "Move to Trash",
            f"Move '{name}' to Trash?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        from qfileman.plugins.builtin.trash import trash_argv
        argv = trash_argv(path)
        if argv is None:
            QMessageBox.warning(
                self,
                "Move to Trash",
                "No system trash backend is available. Use the Trash plugin "
                "or remove the file outside QFileMan for permanent deletion.",
            )
            return

        # Run the trash backend off the GUI thread — it's a subprocess and a
        # slow/large trash op would otherwise block the event loop, even with
        # the timeout. We reuse the project's ProgressRunner (the off-thread
        # mechanism the folder-size and checksum call sites use); the worker
        # keeps the existing subprocess.run(..., timeout=15) call verbatim so
        # the 15s timeout and error reporting are preserved.
        from qfileman.worker import ProgressRunner

        def work(cancel, progress):
            progress(0, -1, f"Moving '{name}' to Trash…")
            cancel.raise_if_cancelled()
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                raise OSError(result.stderr.strip() or result.stdout.strip())

        def on_error(message: str) -> None:
            QMessageBox.warning(self, "Error", f"Could not move to Trash: {message}")

        runner = ProgressRunner(
            work, title="Move to Trash", label=f"Moving '{name}' to Trash…",
            parent=self,
            on_result=lambda _value: self._refresh(),
            on_error=on_error,
        )
        # Keep the runner alive until the worker thread finishes; clear the
        # reference once it's done so the attribute doesn't dangle at a
        # deleted runner.
        self._delete_runner = runner
        runner._worker.finished.connect(
            lambda: setattr(self, "_delete_runner", None)
        )
        runner.start()
