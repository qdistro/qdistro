"""Theme apply + resolve."""

import pytest


def test_resolve_explicit():
    from qdbrowser.theme import resolve_theme
    assert resolve_theme("dark") == "dark"
    assert resolve_theme("light") == "light"
    assert resolve_theme("native") == "native"


def test_apply_theme_returns_resolved(themed_app):
    from qdbrowser.theme import apply_theme
    assert apply_theme(themed_app, "dark") == "dark"
    assert apply_theme(themed_app, "light") == "light"
    assert apply_theme(themed_app, "native") == "native"


@pytest.mark.parametrize("with_qss", [False, True])
def test_apply_theme_native_restores_captured_baseline(themed_app, with_qss):
    from PyQt6.QtGui import QColor, QPalette
    from PyQt6.QtWidgets import QStyleFactory
    from qdbrowser.theme import (
        _underlying_style_name,
        apply_theme,
        reset_controller_for_tests,
    )

    windows = next((n for n in QStyleFactory.keys() if n.lower() == "windows"), None)
    if windows is None:
        pytest.skip("Windows style required to distinguish Fusion")
    original_style = _underlying_style_name(themed_app)
    original_pal = QPalette(themed_app.palette())
    original_qss = themed_app.styleSheet()
    reset_controller_for_tests()
    try:
        themed_app.setStyle(windows)
        pal = QPalette(themed_app.palette())
        pal.setColor(QPalette.ColorRole.Window, QColor("#c8dcc8"))
        themed_app.setPalette(pal)
        themed_app.setStyleSheet("QWidget { background-color: #c8dcc8; }" if with_qss else "")
        window_rgb = themed_app.palette().color(QPalette.ColorRole.Window).getRgb()
        qss = themed_app.styleSheet()
        apply_theme(themed_app, "dark")
        assert themed_app.palette().color(QPalette.ColorRole.Window).getRgb() != window_rgb
        if with_qss:
            assert themed_app.styleSheet() != qss
        assert apply_theme(themed_app, "native") == "native"
        assert _underlying_style_name(themed_app).lower() == "windows"
        assert themed_app.palette().color(QPalette.ColorRole.Window).getRgb() == window_rgb
        assert themed_app.styleSheet() == qss
        apply_theme(themed_app, "light")
        assert apply_theme(themed_app, "native") == "native"
        assert _underlying_style_name(themed_app).lower() == "windows"
        assert themed_app.palette().color(QPalette.ColorRole.Window).getRgb() == window_rgb
        assert themed_app.styleSheet() == qss
    finally:
        reset_controller_for_tests()
        themed_app.setStyle(original_style)
        themed_app.setPalette(original_pal)
        themed_app.setStyleSheet(original_qss)
