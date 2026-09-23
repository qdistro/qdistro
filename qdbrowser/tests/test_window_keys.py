"""Keyboard shortcut wiring on MainWindow."""



def _shortcuts_for(window):
    out = {}
    for act in window.actions():
        seq = act.shortcut().toString()
        if seq:
            out[seq] = act
    return out


def test_basic_shortcuts_registered(window):
    sc = _shortcuts_for(window)
    for needed in ("Ctrl+T", "Ctrl+W", "Ctrl+L", "Ctrl+F", "Ctrl+E",
                   "F11", "F12", "Alt+1", "Alt+9"):
        assert needed in sc, f"missing shortcut: {needed}"


def test_split_shortcuts(window):
    sc = _shortcuts_for(window)
    assert "Ctrl+Shift+O" in sc
    assert "Ctrl+Shift+E" in sc


def test_zoom_shortcuts(window):
    sc = _shortcuts_for(window)
    assert "Ctrl+=" in sc
    assert "Ctrl+-" in sc
    assert "Ctrl+0" in sc


def test_navigation_shortcuts(window):
    sc = _shortcuts_for(window)
    assert "Alt+Left" in sc
    assert "Alt+Right" in sc


def test_panel_shortcuts(window):
    sc = _shortcuts_for(window)
    assert "F4" in sc


def test_alt_1_through_9(window):
    sc = _shortcuts_for(window)
    for i in range(1, 10):
        assert f"Alt+{i}" in sc, f"missing Alt+{i}"
