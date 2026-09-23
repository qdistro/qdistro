"""More layout serialization coverage."""

from PyQt6.QtCore import Qt
from qdbrowser.layout import _restore_node, _serialize_node


def test_serialize_unknown_widget_type():
    class Stranger:
        pass
    assert _serialize_node(Stranger()) == {"type": "unknown"}


def test_restore_unknown_returns_none():
    assert _restore_node({"type": "alien"}) is None


def test_roundtrip_preserves_orientation(window):
    split = window._tabs.widget(0)
    # Mutate orientation to vertical via a split.
    window._split(Qt.Orientation.Vertical)
    node = _serialize_node(split)
    assert node["orientation"] in ("horizontal", "vertical")
    restored = _restore_node(node)
    assert restored.orientation() == split.orientation()


def test_roundtrip_preserves_group(window):
    split = window._tabs.widget(0)
    wv = window._active_webview
    wv.group = "research"
    node = _serialize_node(split)
    restored = _restore_node(node)
    views = restored.find_webviews()
    assert any(v.group == "research" for v in views)


def test_roundtrip_preserves_pinned(window):
    wv = window._active_webview
    wv.set_pinned(True)
    split = window._tabs.widget(0)
    node = _serialize_node(split)
    restored = _restore_node(node)
    assert restored.find_webviews()[0].pinned is True


def test_roundtrip_preserves_zoom(window):
    wv = window._active_webview
    wv.set_zoom(1.7)
    split = window._tabs.widget(0)
    node = _serialize_node(split)
    restored = _restore_node(node)
    assert restored.find_webviews()[0].zoom() == 1.7


def test_serialize_layout_window(window):
    from qdbrowser.layout import serialize_layout
    data = serialize_layout(window._tabs)
    assert "tabs" in data
    assert len(data["tabs"]) == 1
    assert data["tabs"][0]["tree"]["type"] == "split"


def test_serialize_layout_after_split(window):
    from qdbrowser.layout import serialize_layout
    window._split(Qt.Orientation.Horizontal)
    data = serialize_layout(window._tabs)
    tab = data["tabs"][0]
    # Count leaves.
    def count(node):
        if node.get("type") == "webview":
            return 1
        return sum(count(c) for c in node.get("children", []))
    assert count(tab["tree"]) == 2
