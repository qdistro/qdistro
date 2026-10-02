"""Tab bar and splitter chrome restyle from presentation roles."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QWidget
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.splitter import SplitContainer
from qterminator.theme import attach_presentation, reset_controller_for_tests
from qterminator.window import EditableTabBar, MainWindow


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


def test_splitter_handle_uses_snapshot_outline(qtbot, qapp, tmp_path, monkeypatch):
    reset_controller_for_tests()
    host = QWidget()
    qtbot.addWidget(host)
    split = SplitContainer(parent=host)
    nested = SplitContainer()
    split.addWidget(nested)
    snap = example_snapshot()
    old = split.styleSheet()
    old_nested = nested.styleSheet()
    assert snap.colors.mOutline not in old

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert split.styleSheet() == old
    assert nested.styleSheet() == old_nested

    split.apply_presentation_update()
    assert snap.colors.mOutline in split.styleSheet()
    assert "#555" not in split.styleSheet()
    assert "font-size" not in split.styleSheet()
    assert snap.colors.mOutline in nested.styleSheet()
    reset_controller_for_tests()


def test_tab_bar_uses_snapshot_pane_roles(qtbot, qapp, tmp_path, monkeypatch):
    reset_controller_for_tests()
    host = QWidget()
    qtbot.addWidget(host)
    bar = EditableTabBar(host)
    snap = example_snapshot()
    old = bar.styleSheet()
    assert snap.colors.mSurfaceVariant not in old
    assert snap.colors.mOutline not in old

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert bar.styleSheet() == old

    bar.apply_presentation_update()
    sheet = bar.styleSheet()
    assert snap.colors.mSurface in sheet
    assert snap.colors.mSurfaceVariant in sheet
    assert snap.colors.mOnSurface in sheet
    assert snap.colors.mOnSurfaceVariant in sheet
    assert snap.colors.mOutline in sheet
    assert snap.colors.mHover in sheet
    assert "#555" not in sheet
    assert "font-size" not in sheet
    reset_controller_for_tests()


def test_mainwindow_presentation_update_restyles_tab_bar_and_splitter(
    qtbot, qapp, tmp_path, monkeypatch
):
    reset_controller_for_tests()
    host = QWidget()
    qtbot.addWidget(host)
    bar = EditableTabBar(host)
    split = SplitContainer(parent=host)
    snap = example_snapshot()
    old_bar = bar.styleSheet()
    old_split = split.styleSheet()
    assert snap.colors.mOutline not in old_split
    assert snap.colors.mSurfaceVariant not in old_bar

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert bar.styleSheet() == old_bar
    assert split.styleSheet() == old_split

    class _Tabs:
        def count(self):
            return 1

        def widget(self, _index):
            return split

    win = MainWindow.__new__(MainWindow)
    win._tab_bar = bar
    win._tabs = _Tabs()
    win.iter_terminals = lambda: iter(())
    win.badges = None
    MainWindow.apply_presentation_update(win)

    assert snap.colors.mSurfaceVariant in bar.styleSheet()
    assert snap.colors.mOutline in split.styleSheet()
    assert "#555" not in split.styleSheet()
    reset_controller_for_tests()
