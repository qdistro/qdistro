"""Layout serialize / restore. Uses the window fixture for parented widgets."""

from PyQt6.QtCore import Qt
from qdbrowser.layout import _serialize_node, serialize_layout


def test_serialize_single(window):
    split = window._tabs.widget(0)
    node = _serialize_node(split)
    assert node["type"] == "split"
    assert len(node["children"]) == 1
    assert node["children"][0]["type"] == "webview"


def test_serialize_with_splits(window):
    window._split(Qt.Orientation.Horizontal)
    split = window._tabs.widget(0)
    node = _serialize_node(split)
    leaves = [c for c in node["children"] if c["type"] == "webview"]
    assert len(leaves) == 2


def test_serialize_window_layout(window):
    data = serialize_layout(window._tabs)
    assert isinstance(data["tabs"], list)
    assert len(data["tabs"]) == 1
    assert "tree" in data["tabs"][0]
