"""Timestamp margin restyle from presentation pane roles without pyte."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QWidget
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.plugins.timestamps import TimestampMargin, TimestampsPlugin
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


def test_timestamp_margin_uses_snapshot_pane_roles(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    margin = TimestampMargin(host)
    now = time.time()
    stored = [(0, now), (16, now + 1)]
    margin.set_timestamps(stored)
    snap = example_snapshot()
    old_bg = QColor(margin._bg).name()
    old_fg = QColor(margin._fg).name()
    old_ts = list(margin._timestamps)
    assert QColor(snap.colors.mSurface).name().lower() != old_bg.lower()
    assert QColor(snap.colors.mOnSurfaceVariant).name().lower() != old_fg.lower()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert QColor(margin._bg).name() == old_bg
    assert QColor(margin._fg).name() == old_fg
    assert margin._timestamps == old_ts

    margin.apply_presentation_update()
    expected_font = _ui_font(relative=0.9)
    assert QColor(margin._bg).name().lower() == snap.colors.mSurface.lower()
    assert QColor(margin._fg).name().lower() == snap.colors.mOnSurfaceVariant.lower()
    assert margin._timestamps == old_ts
    assert margin._font.family() == expected_font.family()
    assert margin._font.pointSizeF() == expected_font.pointSizeF()
    reset_controller_for_tests()


def test_timestamps_plugin_restyles_existing_margins(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    margin = TimestampMargin(host)
    plugin = TimestampsPlugin()
    plugin._margins[id(margin)] = margin
    snap = example_snapshot()
    old_bg = QColor(margin._bg).name()
    old_fg = QColor(margin._fg).name()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert QColor(margin._bg).name() == old_bg
    assert QColor(margin._fg).name() == old_fg

    plugin.apply_presentation_update()
    assert QColor(margin._bg).name().lower() == snap.colors.mSurface.lower()
    assert QColor(margin._fg).name().lower() == snap.colors.mOnSurfaceVariant.lower()
    reset_controller_for_tests()


def test_mainwindow_presentation_update_restyles_timestamp_margins(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    margin = TimestampMargin(host)
    snap = example_snapshot()
    old_bg = QColor(margin._bg).name()
    assert QColor(snap.colors.mSurface).name().lower() != old_bg.lower()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert QColor(margin._bg).name() == old_bg

    class _EmptyBar:
        def apply_presentation_update(self):
            return None

    class _EmptyTabs:
        def count(self):
            return 0

        def widget(self, _index):
            return None

    class _Timestamps:
        def apply_presentation_update(self):
            margin.apply_presentation_update()

    win = MainWindow.__new__(MainWindow)
    win._tab_bar = _EmptyBar()
    win._tabs = _EmptyTabs()
    win.iter_terminals = lambda: iter(())
    win.badges = None
    win.instant_replay = None
    win.timestamps = _Timestamps()
    MainWindow.apply_presentation_update(win)

    assert QColor(margin._bg).name().lower() == snap.colors.mSurface.lower()
    assert QColor(margin._fg).name().lower() == snap.colors.mOnSurfaceVariant.lower()
    reset_controller_for_tests()
