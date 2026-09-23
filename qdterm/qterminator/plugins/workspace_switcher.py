"""Workspace switcher plugin for QTerminator.

Provides a fuzzy-find dialog to quickly switch between project workspaces.
Scans configured root directories for directories containing marker files
(e.g., .git, package.json) and opens the selected workspace in a new tab.

Inspired by smart_workspace_switcher.wezterm.

Config example:

    [plugins.workspace_switcher]
    search_roots = ["~/Documents", "~/Projects"]
    markers = [".git", "package.json"]
    max_depth = 3
"""

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from qterminator.config import Config
from qterminator.plugin import MenuProvider

DEFAULT_SEARCH_ROOTS = ["~/Documents", "~/Projects", "~/work"]
DEFAULT_MARKERS = [
    ".git",
    ".svn",
    "package.json",
    "Cargo.toml",
    "pyproject.toml",
    "go.mod",
    "Makefile",
]
DEFAULT_MAX_DEPTH = 3


def _fuzzy_match(query, text):
    """Simple fuzzy matcher: returns True if all chars of query appear in
    text in order (case-insensitive)."""
    if not query:
        return True
    q = query.lower()
    t = text.lower()
    i = 0
    for ch in t:
        if ch == q[i]:
            i += 1
            if i == len(q):
                return True
    return False


def find_workspaces(search_roots, markers, max_depth):
    """Scan search_roots up to max_depth and return a sorted list of
    workspace directory paths (absolute)."""
    markers_set = set(markers)
    found = []
    seen = set()

    for root in search_roots:
        root_abs = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root_abs):
            continue
        root_depth = root_abs.rstrip(os.sep).count(os.sep)

        for dirpath, dirnames, filenames in os.walk(root_abs, followlinks=False):
            depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
            entries = set(dirnames) | set(filenames)
            if entries & markers_set:
                if dirpath not in seen:
                    seen.add(dirpath)
                    found.append(dirpath)
                # Don't descend further into a matched workspace
                dirnames[:] = []
                continue
            if depth >= max_depth:
                dirnames[:] = []
                continue
            # Skip hidden dirs for efficiency
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

    found.sort(key=lambda p: os.path.basename(p).lower())
    return found


class WorkspaceDialog(QDialog):
    """Fuzzy-search dialog for selecting a workspace."""

    def __init__(self, workspaces, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Switch Workspace")
        self.resize(600, 400)
        self._all = list(workspaces)
        self._selected = None

        layout = QVBoxLayout(self)

        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText("Type to filter workspaces...")
        layout.addWidget(self._filter)

        self._list = QListWidget(self)
        layout.addWidget(self._list)

        self._populate(self._all)

        self._filter.textChanged.connect(self._on_filter_changed)
        self._filter.returnPressed.connect(self._accept_current)
        self._list.itemActivated.connect(lambda _: self._accept_current())

        # Allow arrow navigation from the filter box.
        self._filter.installEventFilter(self)

    def _populate(self, paths):
        self._list.clear()
        home = os.path.expanduser("~")
        for path in paths:
            display = path
            if display.startswith(home):
                display = "~" + display[len(home):]
            name = os.path.basename(path) or path
            item = QListWidgetItem(f"{name}    {display}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self._list.addItem(item)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _on_filter_changed(self, text):
        if not text:
            self._populate(self._all)
            return
        filtered = [
            p for p in self._all
            if _fuzzy_match(text, os.path.basename(p))
            or _fuzzy_match(text, p)
        ]
        self._populate(filtered)

    def _accept_current(self):
        item = self._list.currentItem()
        if item is None:
            return
        self._selected = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def selected_path(self):
        return self._selected

    def eventFilter(self, obj, event):
        if obj is self._filter and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up,
                       Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self._list.keyPressEvent(event)
                return True
        return super().eventFilter(obj, event)


class WorkspaceSwitcherPlugin(MenuProvider):
    name = "workspace_switcher"
    description = "Fuzzy-find dialog to switch between project workspaces"
    version = "1.0"
    category = "Workspace"

    def __init__(self):
        self._cache = None

    def _read_config(self):
        config = Config()
        search_roots = config.get(
            "plugins", "workspace_switcher", "search_roots",
            default=DEFAULT_SEARCH_ROOTS,
        )
        markers = config.get(
            "plugins", "workspace_switcher", "markers",
            default=DEFAULT_MARKERS,
        )
        max_depth = config.get(
            "plugins", "workspace_switcher", "max_depth",
            default=DEFAULT_MAX_DEPTH,
        )
        return search_roots, markers, int(max_depth)

    def refresh(self):
        """Rescan workspaces and update the cache."""
        roots, markers, depth = self._read_config()
        self._cache = find_workspaces(roots, markers, depth)
        return self._cache

    def get_workspaces(self, force_refresh=False):
        if self._cache is None or force_refresh:
            self.refresh()
        return self._cache

    def get_menu_items(self, terminal):
        return [
            ("Switch Workspace...", lambda t=terminal: self._show_dialog(t)),
            ("Refresh Workspaces", lambda: self.refresh()),
        ]

    def _show_dialog(self, terminal):
        workspaces = self.get_workspaces()
        window = terminal.window()
        dialog = WorkspaceDialog(workspaces, parent=window)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            path = dialog.selected_path()
            if path and hasattr(window, "new_tab"):
                window.new_tab(working_directory=path)
