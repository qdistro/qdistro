"""Session/layout serialization. Mirrors qterminator/layout.py."""

import json

from PyQt6.QtCore import Qt

from qdbrowser.splitter import SplitContainer
from qdbrowser.webview import WebView


def serialize_layout(tabs_widget):
    tabs = []
    for i in range(tabs_widget.count()):
        split = tabs_widget.widget(i)
        tabs.append({
            "name": tabs_widget.tabText(i),
            "tree": _serialize_node(split),
        })
    return {"tabs": tabs}


def _serialize_node(widget):
    if isinstance(widget, SplitContainer):
        children = [_serialize_node(widget.widget(i))
                    for i in range(widget.count())]
        orientation = ("horizontal"
                       if widget.orientation() == Qt.Orientation.Horizontal
                       else "vertical")
        return {
            "type": "split",
            "orientation": orientation,
            "sizes": widget.sizes(),
            "children": children,
        }
    if isinstance(widget, WebView):
        return {
            "type": "webview",
            "url": widget.url() or "about:blank",
            "group": widget.group,
            "pinned": widget.pinned,
            "muted": widget.muted,
            "zoom": widget.zoom(),
            "profile": widget.profile_name,
        }
    return {"type": "unknown"}


def restore_layout(window, layout_data):
    """Restore a saved layout into a window. Window should have no tabs.

    Every restored WebView is wired exactly once (no double-connect) and
    ``window.webview_added`` is emitted for each so plugins that index
    tabs (tab_list, agent_control, downloads) see them.
    """
    tabs = layout_data.get("tabs", [])
    if not tabs:
        window.new_tab()
        return
    for tab_data in tabs:
        if isinstance(tab_data, str):
            try:
                tab_data = json.loads(tab_data)
            except (json.JSONDecodeError, ValueError):
                continue
        tree = tab_data.get("tree", {})
        split = _restore_node(tree)
        if split is None:
            split = SplitContainer(Qt.Orientation.Horizontal)
            split.add_webview()
        name = tab_data.get("name", "New Tab")
        window._tabs.addTab(split, name)
        # Single-pass wiring + emit, after the splitter is fully parented.
        for wv in split.find_webviews():
            window._connect_webview(wv)
            window.webview_added.emit(wv)
    window._tabs.setCurrentIndex(0)
    first = window._tabs.widget(0)
    views = first.find_webviews()
    if views:
        views[0].setFocus()
        window._set_active_webview(views[0])


def _restore_node(data):
    t = data.get("type", "unknown")
    if t == "webview":
        wv = WebView(url=data.get("url", "about:blank"),
                     profile_name=data.get("profile", "default"))
        wv.group = data.get("group")
        wv.set_pinned(bool(data.get("pinned", False)))
        wv.set_muted(bool(data.get("muted", False)))
        wv.set_zoom(float(data.get("zoom", 1.0)))
        return wv
    if t == "split":
        orientation = (Qt.Orientation.Horizontal
                       if data.get("orientation", "horizontal") == "horizontal"
                       else Qt.Orientation.Vertical)
        split = SplitContainer(orientation)
        for child_data in data.get("children", []):
            child = _restore_node(child_data)
            if child is not None:
                split.addWidget(child)
        sizes = data.get("sizes")
        if sizes and len(sizes) == split.count():
            split.setSizes(sizes)
        return split
    return None
