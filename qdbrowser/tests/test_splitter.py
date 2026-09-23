"""Recursive splitter behavior.

We exercise the splitter through the real MainWindow path — top-level
SplitContainer instances destroyed between tests crash QtWebEngine
internals (Chromium's IPC channels outlive Python objects). Letting the
window own all webviews keeps the lifecycle clean.
"""

from PyQt6.QtCore import Qt


def test_initial_tab_has_one_webview(window):
    split = window._tabs.widget(0)
    assert split.count() == 1
    assert len(split.find_webviews()) == 1


def test_split_horizontal_appends(window):
    a = window._active_webview
    window._split(Qt.Orientation.Horizontal)
    split = window._tabs.widget(0)
    views = split.find_webviews()
    assert len(views) == 2
    assert a in views


def test_split_vertical_nests(window):
    from qdbrowser.splitter import SplitContainer
    window._split(Qt.Orientation.Horizontal)  # 2 views, horizontal
    window._split(Qt.Orientation.Vertical)    # nests
    split = window._tabs.widget(0)
    has_nested = any(
        isinstance(split.widget(i), SplitContainer)
        for i in range(split.count())
    )
    assert has_nested
    assert len(split.find_webviews()) == 3


def test_close_split_unnests(window):
    window._split(Qt.Orientation.Horizontal)
    window._split(Qt.Orientation.Vertical)
    split = window._tabs.widget(0)
    assert len(split.find_webviews()) == 3
    window._close_active_split()
    assert len(split.find_webviews()) == 2


def test_navigate_between_splits(window):
    a = window._active_webview
    window._split(Qt.Orientation.Horizontal)
    b = window._active_webview
    assert a is not b
    window._navigate_split("left")
    # Active should rotate.
    assert window._active_webview is not b
