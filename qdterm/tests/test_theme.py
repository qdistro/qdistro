"""Tests for qterminator.theme module."""

import pytest
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication
from qterminator.theme import STYLESHEET, apply_dark_theme


@pytest.fixture
def app(qtbot):
    """Return the QApplication instance (qtbot ensures one exists)."""
    return QApplication.instance()


class TestApplyDarkTheme:
    """Tests for apply_dark_theme function."""

    def test_apply_does_not_crash(self, app):
        """apply_dark_theme should complete without raising."""
        apply_dark_theme(app)

    def test_window_color_is_dark(self, app):
        """Window background should be dark after applying theme."""
        apply_dark_theme(app)
        color = app.palette().color(QPalette.ColorRole.Window)
        assert color.lightness() < 100

    def test_window_text_color_is_light(self, app):
        """WindowText should be light after applying theme."""
        apply_dark_theme(app)
        color = app.palette().color(QPalette.ColorRole.WindowText)
        assert color.lightness() > 150

    def test_base_color_is_dark(self, app):
        """Base color should be dark after applying theme."""
        apply_dark_theme(app)
        color = app.palette().color(QPalette.ColorRole.Base)
        assert color.lightness() < 100

    def test_text_color_is_light(self, app):
        """Text color should be light after applying theme."""
        apply_dark_theme(app)
        color = app.palette().color(QPalette.ColorRole.Text)
        assert color.lightness() > 150

    def test_button_color_is_dark(self, app):
        """Button color should be dark after applying theme."""
        apply_dark_theme(app)
        color = app.palette().color(QPalette.ColorRole.Button)
        assert color.lightness() < 100

    def test_highlight_color_is_set(self, app):
        """Highlight color should be set to the selection color."""
        apply_dark_theme(app)
        color = app.palette().color(QPalette.ColorRole.Highlight)
        expected = QColor("#264f78")
        assert color.red() == expected.red()
        assert color.green() == expected.green()
        assert color.blue() == expected.blue()

    def test_link_color_is_set(self, app):
        """Link color should be set to the accent light color."""
        apply_dark_theme(app)
        color = app.palette().color(QPalette.ColorRole.Link)
        expected = QColor("#3d8fd4")
        assert color.red() == expected.red()
        assert color.green() == expected.green()
        assert color.blue() == expected.blue()

    def test_stylesheet_is_applied(self, app):
        """App stylesheet should be non-empty after applying theme."""
        apply_dark_theme(app)
        assert len(app.styleSheet()) > 0

    def test_idempotent_double_apply(self, app):
        """Applying theme twice should not crash or change the result."""
        apply_dark_theme(app)
        palette_after_first = app.palette()
        window_color_1 = palette_after_first.color(QPalette.ColorRole.Window)

        apply_dark_theme(app)
        palette_after_second = app.palette()
        window_color_2 = palette_after_second.color(QPalette.ColorRole.Window)

        assert window_color_1.name() == window_color_2.name()

    def test_disabled_text_dimmer_than_active(self, app):
        """Disabled text should be dimmer (lower lightness) than active text."""
        apply_dark_theme(app)
        palette = app.palette()
        active_text = palette.color(
            QPalette.ColorGroup.Normal, QPalette.ColorRole.Text
        )
        disabled_text = palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
        )
        assert disabled_text.lightness() < active_text.lightness()


class TestStylesheet:
    """Tests for the STYLESHEET constant."""

    def test_stylesheet_is_nonempty_string(self):
        """STYLESHEET should be a non-empty string."""
        assert isinstance(STYLESHEET, str)
        assert len(STYLESHEET) > 0

    def test_stylesheet_contains_qmenubar(self):
        """STYLESHEET should contain QMenuBar selector."""
        assert "QMenuBar" in STYLESHEET

    def test_stylesheet_contains_qmenu(self):
        """STYLESHEET should contain QMenu selector."""
        assert "QMenu" in STYLESHEET

    def test_stylesheet_contains_qtabbar(self):
        """STYLESHEET should contain QTabBar selector."""
        assert "QTabBar" in STYLESHEET

    def test_stylesheet_contains_qsplitter(self):
        """STYLESHEET should contain QSplitter selector."""
        assert "QSplitter" in STYLESHEET
