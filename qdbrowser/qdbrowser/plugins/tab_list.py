"""Vertical tab list — Vivaldi-style tab strip in the side panel.

Tabs are grouped by their ``WebView.group`` attribute (tab stacks).
Click to focus, middle-click to close. The widget listens to the
window's ``webview_added``/``webview_removed`` and ``navigation_event``
signals so it stays in sync without polling.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qdbrowser.plugin import CommandProvider, SidePanelProvider

_ROLE_WV = Qt.ItemDataRole.UserRole + 1


class TabListPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)

        top = QHBoxLayout()
        new_btn = QPushButton("+ New tab")
        new_btn.clicked.connect(lambda: window.new_tab())
        top.addWidget(new_btn, 1)
        layout.addLayout(top)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(14)
        self._tree.setExpandsOnDoubleClick(False)
        self._tree.itemClicked.connect(self._on_clicked)
        self._tree.itemDoubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self._tree, 1)

        # Debounce refreshes so a session-restore opening 200 tabs
        # doesn't trigger 200 full QTreeWidget rebuilds — coalesce
        # into a single repaint after a short idle.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(50)
        self._refresh_timer.timeout.connect(self.refresh)

        # Wire window signals → schedule.
        window.webview_added.connect(lambda _wv: self._schedule_refresh())
        window.webview_removed.connect(lambda _wv: self._schedule_refresh())
        window.active_webview_changed.connect(
            lambda _wv: self._schedule_refresh())
        # url_changed fires per character of typed URL; we only care
        # about the final-result kind. ``title_changed`` is cheaper
        # and arrives once per page.
        window.webview_added.connect(self._wire_title_signal)

        self.refresh()

    def _wire_title_signal(self, wv):
        try:
            wv.title_changed.connect(lambda _w, _t: self._schedule_refresh())
        except Exception:
            pass

    def _schedule_refresh(self):
        self._refresh_timer.start()

    def refresh(self):
        self._tree.clear()
        # Group webviews by .group; ungrouped go at the top.
        groups: dict = {}
        ungrouped: list = []
        for i in range(self._window._tabs.count()):
            split = self._window._tabs.widget(i)
            try:
                views = split.find_webviews()
            except AttributeError:
                continue
            for wv in views:
                if wv.group:
                    groups.setdefault(wv.group, []).append(wv)
                else:
                    ungrouped.append(wv)

        # Ungrouped first.
        for wv in ungrouped:
            self._add_leaf(None, wv)
        # Then each group.
        for name, views in groups.items():
            top = QTreeWidgetItem([f"▸  {name}  ({len(views)})"])
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            top.setExpanded(True)
            self._tree.addTopLevelItem(top)
            for wv in views:
                self._add_leaf(top, wv)
        # Expand top-level so groups are visible.
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setExpanded(True)

    def _add_leaf(self, parent, wv):
        title = wv.title() or wv.url() or "New tab"
        prefix = ""
        if wv.pinned:
            prefix += "📌 "
        if wv.muted:
            prefix += "🔇 "
        if wv is self._window._active_webview:
            prefix = "● " + prefix
        text = f"{prefix}{title}"
        item = QTreeWidgetItem([text])
        item.setData(0, _ROLE_WV, wv)
        item.setToolTip(0, wv.url() or "")
        if parent is None:
            self._tree.addTopLevelItem(item)
        else:
            parent.addChild(item)

    def _on_clicked(self, item, _col):
        wv = item.data(0, _ROLE_WV)
        if wv is None:
            return
        # Focus the tab/split containing wv.
        for i in range(self._window._tabs.count()):
            split = self._window._tabs.widget(i)
            try:
                views = split.find_webviews()
            except AttributeError:
                continue
            if wv in views:
                self._window._tabs.setCurrentIndex(i)
                self._window._set_active_webview(wv)
                wv.setFocus()
                return

    def _on_double_clicked(self, item, _col):
        # Double-click on the group header toggles collapse.
        if item.data(0, _ROLE_WV) is None:
            item.setExpanded(not item.isExpanded())

    def mousePressEvent(self, event):  # noqa: N802 (Qt)
        if event.button() == Qt.MouseButton.MiddleButton:
            item = self._tree.itemAt(event.pos())
            if item:
                wv = item.data(0, _ROLE_WV)
                if wv is not None:
                    # Close the *tab* this webview is in.
                    for i in range(self._window._tabs.count()):
                        split = self._window._tabs.widget(i)
                        try:
                            views = split.find_webviews()
                        except AttributeError:
                            continue
                        if wv in views:
                            self._window._on_tab_close_requested(i)
                            return
        super().mousePressEvent(event)


class TabListPlugin(SidePanelProvider, CommandProvider):
    name = "tab_list"
    description = "Vertical tab list grouped by stack."
    capabilities = ["side_panel", "command_provider"]
    panel_id = "tab_list"
    panel_label = "Tabs"
    panel_icon = "≡"

    def __init__(self):
        super().__init__()
        self._panel: TabListPanel | None = None
        self._window = None

    def build_panel(self, window):
        self._window = window
        self._panel = TabListPanel(window)
        return self._panel

    def get_commands(self, window):
        return [
            ("Show tab list panel",
             lambda: window._side_panel.show_panel(self.panel_id)),
        ]
