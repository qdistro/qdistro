"""captured_output — sidebar that aggregates triggers `capture` events.

Subscribes to the ``triggers`` service for ``action="capture"`` events
and maintains a per-sidebar, per-tab list of matches. Surfaces them
in a lazily-created ``QDockWidget`` on the right edge of the main
window: a tabbed pane with one tab per named sidebar (``URLs``,
``Errors``, …), each containing a tree grouped by source terminal.

This is mostly plumbing on top of the ``triggers`` plugin:

  - ``triggers`` parses the byte stream, applies the rule's regex,
    and dispatches the configured action.
  - When the action is ``capture``, ``triggers`` also writes the
    match into its own in-memory sidebar bucket *and* emits the
    event to every subscriber.
  - We subscribe and project the events into the dock view + an
    enriched event log (annotated with ``shell_integration``'s
    last-command record so a "Errors from the last `cargo build`"
    filter is one click away).

Click-to-jump (MVP): scroll the destination terminal to the bottom.
A future revision can target the exact line via ShadowScreen seq →
terminal-line mapping; the binding gap (`sendKeyEvent`/scroll-to-seq)
is tracked in ``todo/qterm-*.md``.
"""

import os
import time

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qterminator.config import Config
from qterminator.plugin import MenuProvider

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class CapturedOutputService:
    """Holds the per-(sidebar, tab_id) event lists. Independent of the
    dock widget so it can be queried headlessly in tests."""

    def __init__(self, window, max_per_sidebar: int = 1000):
        self._window = window
        self._max = max(1, int(max_per_sidebar))
        # sidebar -> list[dict]  (each entry: {tab_id, match, groups,
        # fired_at, command_record}).
        self._entries: dict[str, list[dict]] = {}
        # Callable list invoked on each accepted capture; the dock
        # widget hooks itself here.
        self._listeners: list = []

    def add_listener(self, cb) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    def sidebars(self) -> list[str]:
        return list(self._entries.keys())

    def entries(self, sidebar: str) -> list[dict]:
        return list(self._entries.get(sidebar, []))

    def clear(self, sidebar: str | None = None) -> None:
        if sidebar is None:
            self._entries.clear()
        else:
            self._entries.pop(sidebar, None)

    def clear_tab(self, tab_id: int) -> None:
        """Drop every entry tagged with ``tab_id`` from every sidebar.
        Empty sidebars are removed too so the dock can hide them."""
        for name in list(self._entries.keys()):
            kept = [e for e in self._entries[name] if e["tab_id"] != tab_id]
            if kept:
                self._entries[name] = kept
            else:
                del self._entries[name]

    # -- triggers wiring --

    def handle_capture(self, event) -> None:
        """Subscriber for ``triggers``. Only ``action="capture"``
        events are stored; the rest are ignored so a single shared
        subscription is enough."""
        if getattr(event, "action", None) != "capture":
            return
        terminal = event.terminal
        # Sidebar name is on the rule options; the event itself only
        # tells us the rule index. Walk back to the rule to pick the
        # sidebar name. If we can't (no service), fall back to the
        # default name the ``capture`` action uses.
        sidebar_name = "Captured"
        triggers = getattr(self._window, "triggers", None)
        if triggers is not None:
            for rule in triggers.rules:
                if rule.index == event.rule_index:
                    sidebar_name = rule.options.get("sidebar", "Captured")
                    break
        # Pull the command context from shell_integration if loaded.
        cmd_rec = None
        shell_int = getattr(self._window, "shell_integration", None)
        if shell_int is not None:
            try:
                cmd_rec = shell_int.serialize_last_command(terminal)
            except Exception:
                cmd_rec = None
        entry = {
            "tab_id": id(terminal),
            "sidebar": sidebar_name,
            "match": event.text,
            "groups": dict(event.groups),
            "fired_at": event.fired_at,
            "command_record": cmd_rec,
        }
        bucket = self._entries.setdefault(sidebar_name, [])
        bucket.append(entry)
        if len(bucket) > self._max:
            del bucket[: len(bucket) - self._max]
        for cb in list(self._listeners):
            try:
                cb(entry)
            except Exception:
                pass

    # -- export --

    def export_to_text(self, path: str, sidebar: str | None = None) -> int:
        """Dump entries to a text file. One match per line, prefixed
        with sidebar + tab_id. Returns the number of lines written."""
        targets = [sidebar] if sidebar else list(self._entries.keys())
        lines: list[str] = []
        for name in targets:
            for e in self._entries.get(name, []):
                lines.append(f"[{name}] tab={e['tab_id']} {e['match']}")
        with open(path, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        return len(lines)


# ---------------------------------------------------------------------------
# Dock widget
# ---------------------------------------------------------------------------

class CapturedOutputDock(QDockWidget):
    """Tabbed dock with one inner tab per sidebar name. Items grouped
    by source terminal id. Double-click jumps to the terminal."""

    jump_requested = pyqtSignal(int)  # tab_id

    def __init__(self, service: CapturedOutputService, parent=None):
        super().__init__("Captured Output", parent)
        self._service = service
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(False)
        # sidebar -> QTreeWidget
        self._trees: dict[str, QTreeWidget] = {}
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(2, 2, 2, 2)
        v.addWidget(self._tabs)
        btns = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_active_sidebar)
        export_btn = QPushButton("Export…")
        export_btn.clicked.connect(self._export_active_sidebar)
        btns.addStretch(1)
        btns.addWidget(clear_btn)
        btns.addWidget(export_btn)
        v.addLayout(btns)
        self.setWidget(container)
        service.add_listener(self._on_entry)
        self.rebuild()

    def rebuild(self) -> None:
        self._tabs.clear()
        self._trees.clear()
        for sidebar in self._service.sidebars():
            self._ensure_sidebar(sidebar)
            for entry in self._service.entries(sidebar):
                self._append_to_tree(sidebar, entry)

    def _ensure_sidebar(self, sidebar: str) -> QTreeWidget:
        tree = self._trees.get(sidebar)
        if tree is not None:
            return tree
        tree = QTreeWidget()
        tree.setHeaderLabels(["Match", "Time"])
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tabs.addTab(tree, sidebar)
        self._trees[sidebar] = tree
        return tree

    def _append_to_tree(self, sidebar: str, entry: dict) -> None:
        tree = self._ensure_sidebar(sidebar)
        # Find-or-create the per-tab group node.
        tab_id = entry["tab_id"]
        group_label = self._group_label(tab_id)
        parent = None
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == tab_id:
                parent = item
                break
        if parent is None:
            parent = QTreeWidgetItem([group_label, ""])
            parent.setData(0, Qt.ItemDataRole.UserRole, tab_id)
            tree.addTopLevelItem(parent)
            parent.setExpanded(True)
        ts = time.strftime("%H:%M:%S", time.localtime(entry["fired_at"]))
        child = QTreeWidgetItem([entry["match"], ts])
        child.setData(0, Qt.ItemDataRole.UserRole, tab_id)
        parent.addChild(child)

    def _group_label(self, tab_id: int) -> str:
        # Best-effort: query the window for a terminal title.
        win = self.parent()
        if win is not None and hasattr(win, "_tabs"):
            for i in range(win._tabs.count()):
                split = win._tabs.widget(i)
                for term in split.find_terminals():
                    if id(term) == tab_id:
                        return f"{term.title()} ({tab_id})"
        return f"tab {tab_id}"

    def _on_entry(self, entry: dict) -> None:
        self._append_to_tree(entry["sidebar"], entry)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        tab_id = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(tab_id, int):
            self.jump_requested.emit(tab_id)

    def _clear_active_sidebar(self) -> None:
        idx = self._tabs.currentIndex()
        if idx < 0:
            return
        name = self._tabs.tabText(idx)
        self._service.clear(name)
        self.rebuild()

    def _export_active_sidebar(self) -> None:
        idx = self._tabs.currentIndex()
        if idx < 0:
            return
        name = self._tabs.tabText(idx)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Captured Output",
            f"captured-{name}.txt",
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            n = self._service.export_to_text(path, name)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return
        QMessageBox.information(
            self, "Export complete",
            f"Wrote {n} lines to {os.path.basename(path)}.",
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

class CapturedOutputPlugin(MenuProvider):
    name = "captured_output"
    description = (
        "Dock widget aggregating triggers `capture` events into "
        "grouped, filter-friendly sidebars."
    )
    version = "0.1"
    category = "View"
    capabilities = ["menu_provider", "captured_output"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: CapturedOutputService | None = None
        self._dock: CapturedOutputDock | None = None
        # Subscriber callback we installed on triggers; held so we can
        # remove it on deactivate without depending on closure equality.
        self._triggers_sub = None
        self._original_tab_close = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = bool(cfg.get(
            "plugins", "captured_output", "enabled", default=True,
        ))
        if not enabled:
            return
        self._window = app_controller
        cap = int(cfg.get(
            "plugins", "captured_output", "max_per_sidebar", default=1000,
        ))
        self._service = CapturedOutputService(app_controller, max_per_sidebar=cap)
        if not hasattr(app_controller, "captured_output"):
            app_controller.captured_output = self._service
        triggers = getattr(app_controller, "triggers", None)
        if triggers is not None:
            self._triggers_sub = self._service.handle_capture
            triggers.subscribe(self._triggers_sub)
        # Hook tab-close before MainWindow processes it so we can read
        # the doomed terminals' identity. The signal is already wired
        # to MainWindow's bound slot; rebind the slot to a wrapper
        # that clears our entries first, then delegates.
        tabs = getattr(app_controller, "_tabs", None)
        original = getattr(app_controller, "_on_tab_close_requested", None)
        if tabs is not None and callable(original):
            self._original_tab_close = original

            def wrapped(index, _orig=original):
                self._on_tab_close_requested(index)
                _orig(index)
            try:
                tabs.tabCloseRequested.disconnect(original)
            except (TypeError, RuntimeError):
                pass
            tabs.tabCloseRequested.connect(wrapped)
            app_controller._on_tab_close_requested = wrapped

    def deactivate(self):
        triggers = getattr(self._window, "triggers", None) if self._window else None
        if triggers is not None and self._triggers_sub is not None:
            try:
                triggers.unsubscribe(self._triggers_sub)
            except Exception:
                pass
        self._triggers_sub = None
        if self._original_tab_close is not None and self._window is not None:
            try:
                self._window._on_tab_close_requested = self._original_tab_close
            except AttributeError:
                pass
        self._original_tab_close = None
        if self._dock is not None and self._window is not None:
            try:
                self._window.removeDockWidget(self._dock)
                self._dock.setParent(None)
            except Exception:
                pass
        self._dock = None
        if (self._window is not None
                and getattr(self._window, "captured_output", None) is self._service):
            try:
                del self._window.captured_output
            except AttributeError:
                pass
        self._service = None

    # -- menu --

    def get_menu_items(self, terminal):
        return [
            ("Show Captured Output", lambda: self.show_dock()),
            ("Hide Captured Output", lambda: self.hide_dock()),
            ("---", None),
            ("Clear All Captured", lambda: self._clear_all()),
        ]

    # -- public API used by tests --

    def show_dock(self) -> CapturedOutputDock | None:
        if self._window is None or self._service is None:
            return None
        if self._dock is None:
            self._dock = CapturedOutputDock(self._service, parent=self._window)
            self._dock.jump_requested.connect(self._jump_to_tab)
            self._window.addDockWidget(
                Qt.DockWidgetArea.RightDockWidgetArea, self._dock,
            )
        else:
            self._dock.rebuild()
        self._dock.show()
        return self._dock

    def hide_dock(self) -> None:
        if self._dock is not None:
            self._dock.hide()

    def _clear_all(self) -> None:
        if self._service is None:
            return
        self._service.clear()
        if self._dock is not None:
            self._dock.rebuild()

    def _jump_to_tab(self, tab_id: int) -> None:
        if self._window is None:
            return
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return
        for i in range(tabs.count()):
            split = tabs.widget(i)
            for term in split.find_terminals():
                if id(term) == tab_id:
                    tabs.setCurrentIndex(i)
                    try:
                        term.term.scrollToEnd()
                    except Exception:
                        pass
                    try:
                        term.term.setFocus()
                    except Exception:
                        pass
                    return

    def _on_tab_close_requested(self, index: int) -> None:
        if self._service is None:
            return
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return
        widget = tabs.widget(index)
        if widget is None:
            return
        for term in widget.find_terminals():
            self._service.clear_tab(id(term))
        if self._dock is not None:
            self._dock.rebuild()
