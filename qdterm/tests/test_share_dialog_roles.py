"""Tmux share dialog warning restyle from presentation error role."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QWidget
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.plugins.tmux_share import (
    Share,
    TmuxSharePlugin,
    _ShareDialog,
)
from qterminator.theme import attach_presentation, reset_controller_for_tests
from qterminator.titlebar import _ui_font
from qterminator.window import MainWindow


@pytest.fixture(autouse=True)
def _reset_presentation(qapp):
    reset_controller_for_tests()
    qapp.setPalette(QPalette())
    qapp.setStyleSheet("")
    yield
    reset_controller_for_tests()
    qapp.setPalette(QPalette())
    qapp.setStyleSheet("")


def _config(theme_mode: str = "system"):
    def get(*keys, default=None):
        if keys[:2] == ("general", "theme_mode"):
            return theme_mode
        if keys == ("appearance",):
            return {}
        return default

    return SimpleNamespace(get=get)


def _attach_snapshot(qapp, tmp_path, monkeypatch, snap):
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _config("system"))


def _public_share() -> Share:
    share = Share(session="qterm-1", bind="0.0.0.0")
    share.port = 60001
    share.key = "KEY"
    return share


def test_share_dialog_warning_uses_snapshot_error_role(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    dialog = _ShareDialog(_public_share(), public_bind=True, parent=host)
    snap = example_snapshot()
    warn = dialog._warn
    assert warn is not None
    old_sheet = warn.styleSheet()
    old_text = warn.text()
    assert snap.colors.mError.lower() not in old_sheet.lower()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert warn.styleSheet() == old_sheet
    assert warn.text() == old_text

    dialog.apply_presentation_update()
    expected_font = _ui_font(bold=True)
    sheet = warn.styleSheet()
    assert snap.colors.mError.lower() in sheet.lower()
    assert "#e74c3c" not in sheet.lower()
    assert "font-size" not in sheet
    assert warn.font().family() == expected_font.family()
    assert warn.font().pointSizeF() == expected_font.pointSizeF()
    assert warn.font().bold()
    assert warn.text() == old_text
    reset_controller_for_tests()


def test_tmux_share_plugin_restyles_existing_dialog(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    dialog = _ShareDialog(_public_share(), public_bind=True, parent=host)
    plugin = TmuxSharePlugin()
    plugin._dialogs.append(dialog)
    snap = example_snapshot()
    old_sheet = dialog._warn.styleSheet()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert dialog._warn.styleSheet() == old_sheet

    plugin.apply_presentation_update()
    sheet = dialog._warn.styleSheet()
    assert snap.colors.mError.lower() in sheet.lower()
    assert "#e74c3c" not in sheet.lower()
    reset_controller_for_tests()


def test_mainwindow_presentation_update_restyles_share_dialog(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    dialog = _ShareDialog(_public_share(), public_bind=True, parent=host)
    snap = example_snapshot()
    old_sheet = dialog._warn.styleSheet()
    assert snap.colors.mError.lower() not in old_sheet.lower()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert dialog._warn.styleSheet() == old_sheet

    class _EmptyBar:
        def apply_presentation_update(self):
            return None

    class _EmptyTabs:
        def count(self):
            return 0

        def widget(self, _index):
            return None

    class _SharePlugin:
        def apply_presentation_update(self):
            dialog.apply_presentation_update()

    win = MainWindow.__new__(MainWindow)
    win._tab_bar = _EmptyBar()
    win._tabs = _EmptyTabs()
    win.iter_terminals = lambda: iter(())
    win.badges = None
    win.instant_replay = None
    win.timestamps = None
    win.tmux_share_plugin = _SharePlugin()
    MainWindow.apply_presentation_update(win)

    sheet = dialog._warn.styleSheet()
    assert snap.colors.mError.lower() in sheet.lower()
    assert "#e74c3c" not in sheet.lower()
    reset_controller_for_tests()
