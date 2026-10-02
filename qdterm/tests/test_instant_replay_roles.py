"""Instant-replay overlay restyle from presentation pane roles without pyte."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QWidget
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.plugins.instant_replay import InstantReplayPlugin, ReplayOverlay
from qterminator.theme import attach_presentation, reset_controller_for_tests
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


def _surface_rgba(hex_color: str, alpha: int = 220) -> str:
    color = QColor(hex_color)
    return (
        f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha}"
    )


def test_replay_overlay_uses_snapshot_pane_roles(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    overlay = ReplayOverlay(host)
    snap = example_snapshot()
    old = overlay.styleSheet()
    old_bg = QColor(overlay._status_bg).name()
    old_fg = QColor(overlay._status_fg).name()
    surface_rgba = _surface_rgba(snap.colors.mSurface)
    assert surface_rgba.replace(" ", "") not in old.replace(" ", "")
    assert snap.colors.mOnSurface.lower() not in old.lower()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert overlay.styleSheet() == old
    assert QColor(overlay._status_bg).name() == old_bg
    assert QColor(overlay._status_fg).name() == old_fg

    overlay.apply_presentation_update()
    sheet = overlay.styleSheet()
    compact = sheet.replace(" ", "")
    assert surface_rgba.replace(" ", "") in compact
    assert snap.colors.mOnSurface.lower() in sheet.lower()
    assert "rgba(30,30,30" not in compact
    assert "font-size" not in sheet
    assert QColor(overlay._status_bg).name().lower() == snap.colors.mSurfaceVariant.lower()
    assert QColor(overlay._status_fg).name().lower() == snap.colors.mOnSurfaceVariant.lower()
    reset_controller_for_tests()


def test_instant_replay_plugin_restyles_existing_overlay(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    overlay = ReplayOverlay(host)
    plugin = InstantReplayPlugin()
    plugin._overlay = overlay
    snap = example_snapshot()
    old = overlay.styleSheet()
    old_bg = QColor(overlay._status_bg).name()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert overlay.styleSheet() == old
    assert QColor(overlay._status_bg).name() == old_bg

    plugin.apply_presentation_update()
    compact = overlay.styleSheet().replace(" ", "")
    assert _surface_rgba(snap.colors.mSurface).replace(" ", "") in compact
    assert snap.colors.mOnSurface.lower() in overlay.styleSheet().lower()
    assert QColor(overlay._status_bg).name().lower() == snap.colors.mSurfaceVariant.lower()
    reset_controller_for_tests()


def test_mainwindow_presentation_update_restyles_replay_overlay(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    overlay = ReplayOverlay(host)
    snap = example_snapshot()
    old = overlay.styleSheet()
    surface_rgba = _surface_rgba(snap.colors.mSurface)
    assert surface_rgba.replace(" ", "") not in old.replace(" ", "")

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert overlay.styleSheet() == old

    class _EmptyBar:
        def apply_presentation_update(self):
            return None

    class _EmptyTabs:
        def count(self):
            return 0

        def widget(self, _index):
            return None

    class _Replay:
        def apply_presentation_update(self):
            overlay.apply_presentation_update()

    win = MainWindow.__new__(MainWindow)
    win._tab_bar = _EmptyBar()
    win._tabs = _EmptyTabs()
    win.iter_terminals = lambda: iter(())
    win.badges = None
    win.instant_replay = _Replay()
    win.timestamps = None
    MainWindow.apply_presentation_update(win)

    sheet = overlay.styleSheet()
    compact = sheet.replace(" ", "")
    assert surface_rgba.replace(" ", "") in compact
    assert snap.colors.mOnSurface.lower() in sheet.lower()
    assert "font-size" not in sheet
    assert QColor(overlay._status_bg).name().lower() == snap.colors.mSurfaceVariant.lower()
    reset_controller_for_tests()
