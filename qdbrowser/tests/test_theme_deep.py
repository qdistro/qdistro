"""Theme palette + stylesheet edge cases."""


def test_resolve_unknown_falls_back():
    from qdbrowser.theme import resolve_theme
    # 'system' calls detect_system_theme which may return dark/light.
    res = resolve_theme("garbage")
    assert res in ("dark", "light")


def test_apply_dark_then_light(themed_app):
    from qdbrowser.theme import apply_theme
    a = apply_theme(themed_app, "dark")
    assert a == "dark"
    b = apply_theme(themed_app, "light")
    assert b == "light"


def test_dark_stylesheet_constants_present():
    from qdbrowser.theme import DARK_QSS, LIGHT_QSS
    assert "QTabBar" in DARK_QSS
    assert "QTabBar" in LIGHT_QSS
    assert "QToolBar" in DARK_QSS
    assert "QLineEdit" in DARK_QSS


def test_palette_distinguishes_dark_light(themed_app):
    from PyQt6.QtGui import QPalette
    from qdbrowser.theme import apply_theme
    apply_theme(themed_app, "dark")
    dark_window = themed_app.palette().color(QPalette.ColorRole.Window).name()
    apply_theme(themed_app, "light")
    light_window = themed_app.palette().color(QPalette.ColorRole.Window).name()
    assert dark_window.lower() != light_window.lower()


def test_detect_system_theme_returns_string(themed_app):
    from qdbrowser.theme import detect_system_theme
    assert detect_system_theme() in ("dark", "light")
