"""Mouse gesture computation."""

from PyQt6.QtCore import QPoint


def _filter():
    from qdbrowser.plugins.mouse_gestures import _GestureFilter
    return _GestureFilter(plugin=None)


def _points(seq):
    return [QPoint(x, y) for x, y in seq]


def test_short_stroke_returns_empty():
    f = _filter()
    f._points = _points([(0, 0), (1, 1)])
    assert f._compute_gesture() == ""


def test_right_stroke():
    f = _filter()
    f._points = _points([(0, 0), (50, 0), (100, 0), (150, 0), (200, 0),
                         (250, 0), (300, 0)])
    g = f._compute_gesture()
    assert "R" in g


def test_left_stroke():
    f = _filter()
    f._points = _points([(300, 0), (200, 0), (100, 0), (50, 0), (10, 0),
                         (0, 0), (-50, 0)])
    g = f._compute_gesture()
    assert "L" in g


def test_down_stroke():
    f = _filter()
    f._points = _points([(0, 0), (0, 50), (0, 100), (0, 150),
                         (0, 200), (0, 250), (0, 300)])
    g = f._compute_gesture()
    assert "D" in g


def test_up_stroke():
    f = _filter()
    f._points = _points([(0, 300), (0, 250), (0, 200), (0, 150),
                         (0, 100), (0, 50), (0, 0)])
    g = f._compute_gesture()
    assert "U" in g


def test_dr_stroke():
    f = _filter()
    pts = [(0, 0)]
    # Down then right.
    for i in range(1, 8):
        pts.append((0, i * 40))
    for i in range(1, 8):
        pts.append((i * 40, 280))
    f._points = _points(pts)
    g = f._compute_gesture()
    assert g.startswith("D")
    assert "R" in g


def test_plugin_inactive_with_gestures_disabled(fresh_config):
    from qdbrowser.config import Config
    Config().set("gestures", "enabled", False)
    from qdbrowser.plugins.mouse_gestures import MouseGesturesPlugin
    plug = MouseGesturesPlugin()
    plug.activate(window=None)
    assert plug._filter is None


def test_plugin_loads_bindings_from_config(fresh_config, themed_app):
    from qdbrowser.config import Config
    Config().set("gestures", "bindings", {"R": "forward", "L": "back"})

    class FakeWin:
        pass

    from qdbrowser.plugins.mouse_gestures import MouseGesturesPlugin
    plug = MouseGesturesPlugin()
    plug.activate(FakeWin())
    assert plug._bindings == {"R": "forward", "L": "back"}
    plug.deactivate()


def test_unknown_action_does_not_raise(fresh_config, themed_app):
    from qdbrowser.config import Config
    Config().set("gestures", "bindings", {"X": "unknown_action"})

    class FakeWin:
        pass

    from qdbrowser.plugins.mouse_gestures import MouseGesturesPlugin
    plug = MouseGesturesPlugin()
    plug.activate(FakeWin())
    # "X" is not in the action_map so it should silently no-op.
    plug._fire("X")
    plug.deactivate()
