"""Side panel host."""

from PyQt6.QtWidgets import QLabel


def test_add_panel_registers(window):
    sp = window._side_panel
    before = sp.panel_ids()
    sp.add_panel("test1", "Test 1", "T", QLabel("hi"))
    assert "test1" in sp.panel_ids()
    assert len(sp.panel_ids()) == len(before) + 1


def test_duplicate_add_ignored(window):
    sp = window._side_panel
    n = len(sp.panel_ids())
    sp.add_panel("dup", "D", "D", QLabel("a"))
    sp.add_panel("dup", "D", "D", QLabel("b"))
    assert len(sp.panel_ids()) == n + 1


def test_get_panel_returns_widget(window):
    sp = window._side_panel
    w = QLabel("widget")
    sp.add_panel("getme", "G", "G", w)
    assert sp.get_panel("getme") is w


def test_get_unknown_panel(window):
    assert window._side_panel.get_panel("never_added") is None


def test_show_panel_switches_stack(window):
    sp = window._side_panel
    a = QLabel("A")
    b = QLabel("B")
    sp.add_panel("aaa", "A", "A", a)
    sp.add_panel("bbb", "B", "B", b)
    sp.show_panel("aaa")
    assert sp._stack.currentWidget() is a
    sp.show_panel("bbb")
    assert sp._stack.currentWidget() is b


def test_show_unknown_panel_noop(window):
    sp = window._side_panel
    sp.show_panel("does_not_exist")  # must not raise


def test_default_panels_from_plugins(window):
    sp = window._side_panel
    for needed in ("bookmarks", "history", "downloads", "notes",
                   "web_panels"):
        assert needed in sp.panel_ids()
