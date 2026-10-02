"""Shared presentation attach for qterminator chrome."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QPalette
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.theme import attach_presentation, reset_controller_for_tests


def _config(theme_mode: str = "system", appearance: dict | None = None):
    appearance = appearance or {}

    def get(*keys, default=None):
        if keys[:2] == ("general", "theme_mode"):
            return theme_mode
        if keys == ("appearance",):
            return appearance
        return default

    return SimpleNamespace(get=get)


@pytest.fixture(autouse=True)
def _reset_presentation(qapp):
    reset_controller_for_tests()
    qapp.setPalette(QPalette())
    qapp.setStyleSheet("")
    yield
    reset_controller_for_tests()
    qapp.setPalette(QPalette())
    qapp.setStyleSheet("")


def test_attach_presentation_follows_snapshot(qapp, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    resolved = attach_presentation(qapp, _config("system"))
    assert resolved == example_snapshot().mode
    assert qapp.palette().color(QPalette.ColorRole.Window).name() == example_snapshot().colors.mSurface
    assert qapp.palette().color(QPalette.ColorRole.HighlightedText).name() == (
        example_snapshot().colors.mOnPrimary
    )


def test_attach_presentation_native_ignores_snapshot(qapp, tmp_path, monkeypatch):
    native = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    resolved = attach_presentation(qapp, _config("native"))
    assert resolved == "native"
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native


def test_missing_snapshot_applies_system_fallback(qapp, tmp_path, monkeypatch):
    from PyQt6.QtGui import QColor
    from qterminator.theme import (
        LT_BG,
        LT_FG,
        apply_dark_theme,
        current_controller,
    )

    monkeypatch.setattr("qterminator.theme.detect_system_theme", lambda: "light")
    apply_dark_theme(qapp)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "missing.json"))
    resolved = attach_presentation(qapp, _config("system"))
    assert resolved == "light"
    ctrl = current_controller()
    assert ctrl is not None
    assert ctrl.state.using_shared_palette is False
    assert qapp.palette().color(QPalette.ColorRole.Window) == QColor(LT_BG)
    assert qapp.palette().color(QPalette.ColorRole.WindowText) == QColor(LT_FG)


def test_mainwindow_presentation_update_restyles_titlebar(qapp, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QWidget
    from qdistro_presentation.model import example_snapshot
    from qterminator.terminal import TerminalWidget
    from qterminator.titlebar import TerminalTitlebar
    from qterminator.window import MainWindow

    host = QWidget()
    active = TerminalTitlebar(host)
    inactive = TerminalTitlebar(host)
    active.set_active(True)
    inactive.set_active(False)
    snap = example_snapshot()
    old_active = active.styleSheet()
    old_inactive = inactive.styleSheet()
    assert snap.colors.mPrimary not in old_active

    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _config("system"))
    assert active.styleSheet() == old_active
    assert inactive.styleSheet() == old_inactive

    class _Term:
        def __init__(self, titlebar):
            self._titlebar = titlebar

        def apply_inherited_presentation(self):
            return None

        apply_presentation_update = TerminalWidget.apply_presentation_update

    class _EmptyTabs:
        def count(self):
            return 0

        def widget(self, _index):
            return None

    class _EmptyBar:
        def apply_presentation_update(self):
            return None

    win = MainWindow.__new__(MainWindow)
    terms = (_Term(active), _Term(inactive))
    win._tab_bar = _EmptyBar()
    win._tabs = _EmptyTabs()
    win.iter_terminals = lambda: iter(terms)
    win.apply_presentation_update()
    assert snap.colors.mPrimary in active.styleSheet()
    assert snap.colors.mOnPrimary in active._title_label.styleSheet()
    assert snap.colors.mSurfaceVariant in inactive.styleSheet()
    assert snap.colors.mOnSurface in inactive._title_label.styleSheet()
    host.deleteLater()
