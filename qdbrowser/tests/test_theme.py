"""Theme apply + resolve."""

import re

import pytest

_LITERAL_WHITE_FG = re.compile(r"(?<!background-)color:\s*#ffffff", re.I)


def test_resolve_explicit():
    from qdbrowser.theme import resolve_theme
    assert resolve_theme("dark") == "dark"
    assert resolve_theme("light") == "light"
    assert resolve_theme("native") == "native"


def test_explicit_dark_selection_uses_named_pair(themed_app):
    from PyQt6.QtGui import QPalette
    from qdbrowser.theme import (
        DARK_QSS,
        HOVER,
        HOVER_FG,
        SELECTION,
        SELECTION_FG,
        apply_theme,
        palette_dict,
        reset_controller_for_tests,
    )

    reset_controller_for_tests()
    apply_theme(themed_app, "dark")
    pal = themed_app.palette()
    assert pal.color(QPalette.ColorRole.Highlight).name() == SELECTION
    assert pal.color(QPalette.ColorRole.HighlightedText).name() == SELECTION_FG
    assert pal.color(QPalette.ColorRole.HighlightedText).name() != "#ffffff"
    assert f"color: {SELECTION_FG}" in DARK_QSS
    assert f"background-color: {SELECTION}" in DARK_QSS
    assert f"color: {HOVER_FG}" in DARK_QSS
    assert f"background-color: {HOVER}" in DARK_QSS
    assert _LITERAL_WHITE_FG.search(DARK_QSS) is None
    roles = palette_dict("dark")
    assert roles["selection"] == SELECTION
    assert roles["selection_fg"] == SELECTION_FG
    assert roles["hover"] == HOVER
    assert roles["hover_fg"] == HOVER_FG


def test_explicit_light_selection_uses_named_pair(themed_app):
    from PyQt6.QtGui import QPalette
    from qdbrowser.theme import (
        LIGHT_QSS,
        LT_HOVER,
        LT_HOVER_FG,
        LT_SELECTION,
        LT_SELECTION_FG,
        apply_theme,
        palette_dict,
        reset_controller_for_tests,
    )

    reset_controller_for_tests()
    apply_theme(themed_app, "light")
    pal = themed_app.palette()
    assert pal.color(QPalette.ColorRole.Highlight).name() == LT_SELECTION
    assert pal.color(QPalette.ColorRole.HighlightedText).name() == LT_SELECTION_FG
    assert pal.color(QPalette.ColorRole.HighlightedText).name() != "#ffffff"
    assert f"color: {LT_SELECTION_FG}" in LIGHT_QSS
    assert f"background-color: {LT_SELECTION}" in LIGHT_QSS
    assert f"color: {LT_HOVER_FG}" in LIGHT_QSS
    assert f"background-color: {LT_HOVER}" in LIGHT_QSS
    assert _LITERAL_WHITE_FG.search(LIGHT_QSS) is None
    roles = palette_dict("light")
    assert roles["selection"] == LT_SELECTION
    assert roles["selection_fg"] == LT_SELECTION_FG
    assert roles["hover"] == LT_HOVER
    assert roles["hover_fg"] == LT_HOVER_FG


def test_auto_palette_without_controller_includes_hover_pair(themed_app):
    from PyQt6.QtGui import QPalette
    from qdbrowser.theme import (
        HOVER,
        HOVER_FG,
        SELECTION,
        SELECTION_FG,
        apply_theme,
        palette_dict,
        reset_controller_for_tests,
    )

    reset_controller_for_tests()
    apply_theme(themed_app, "dark")
    auto = palette_dict("auto")
    pal = themed_app.palette()
    assert auto["selection"] == pal.color(QPalette.ColorRole.Highlight).name()
    assert auto["selection_fg"] == pal.color(QPalette.ColorRole.HighlightedText).name()
    assert auto["hover"] == pal.color(QPalette.ColorRole.Midlight).name()
    assert auto["hover_fg"] == pal.color(QPalette.ColorRole.Text).name()
    assert auto["selection"] == SELECTION
    assert auto["selection_fg"] == SELECTION_FG
    assert auto["hover"] == HOVER
    assert auto["hover_fg"] == HOVER_FG
    assert auto["selection_fg"] != "#ffffff"


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
