"""Tests for theme.apply_theme."""

from __future__ import annotations

from PyQt6.QtGui import QPalette
from qfileman.theme import apply_theme


def test_apply_theme_dark_sets_dark_window_color(qapp):
    """The dark palette uses an explicit dark window colour."""
    apply_theme(qapp, "dark")
    color = qapp.palette().color(QPalette.ColorRole.Window)
    # Lightness 0..255; Fusion default is bright. Our dark palette is 53,53,53.
    assert color.lightness() < 80, f"dark mode should be dark, got {color.getRgb()}"


def test_apply_theme_light_is_default_palette(qapp):
    """Light mode resets to a default Fusion palette."""
    # Switch to dark first, then to light, to prove the override is removed.
    apply_theme(qapp, "dark")
    apply_theme(qapp, "light")
    assert qapp.style().objectName().lower() == "fusion"
    # Compare against a fresh QPalette() — light mode should match it.
    default = QPalette()
    assert (
        qapp.palette().color(QPalette.ColorRole.Window).getRgb()
        == default.color(QPalette.ColorRole.Window).getRgb()
    )


def test_apply_theme_returns_resolved_mode(qapp):
    assert apply_theme(qapp, "dark") == "dark"
    assert apply_theme(qapp, "light") == "light"
    assert apply_theme(qapp, "system") == "system"


def test_apply_theme_unknown_mode_falls_back_with_warning(qapp, caplog):
    with caplog.at_level("WARNING", logger="qfileman.theme"):
        resolved = apply_theme(qapp, "neon")
    assert resolved == "system"
    assert any("unknown theme mode" in r.message for r in caplog.records)


def test_apply_theme_light_uses_fusion_style(qapp):
    """Light mode opts into Fusion for consistent rendering."""
    apply_theme(qapp, "light")
    assert qapp.style().objectName().lower() == "fusion"


def test_apply_theme_dark_uses_fusion_style(qapp):
    """Dark mode also opts into Fusion (the palette is Fusion-shaped)."""
    apply_theme(qapp, "dark")
    assert qapp.style().objectName().lower() == "fusion"


def test_apply_theme_system_is_a_noop(qapp):
    """``system`` mode must not touch style or palette — Qt's platform
    integration owns that. Set a known non-default state first, then
    verify ``system`` leaves it intact."""
    apply_theme(qapp, "dark")
    style_before = qapp.style().objectName()
    palette_before = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    resolved = apply_theme(qapp, "system")
    assert resolved == "system"
    assert qapp.style().objectName() == style_before, (
        "system mode must not call setStyle"
    )
    assert (
        qapp.palette().color(QPalette.ColorRole.Window).getRgb() == palette_before
    ), "system mode must not call setPalette"
