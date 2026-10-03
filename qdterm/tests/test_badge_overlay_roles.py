"""Badge overlays restyle from presentation pane roles without pyte."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QWidget
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.plugins.badges import BadgesService, _BadgeOverlay
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


def _surface_rgba(hex_color: str) -> str:
    color = QColor(hex_color)
    return f"rgba({color.red()}, {color.green()}, {color.blue()},"


def test_badge_overlay_uses_snapshot_surface_and_keeps_profile_color(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    overlay = _BadgeOverlay(
        terminal=SimpleNamespace(_term=host),
        template="{hostname}",
        color="#abcdef",
        parent_widget=host,
    )
    snap = example_snapshot()
    old = overlay.styleSheet()
    assert snap.colors.mSurface.lower() not in old.lower()
    assert _surface_rgba(snap.colors.mSurface) not in old.replace(" ", "")

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert overlay.styleSheet() == old

    overlay.apply_presentation_update()
    sheet = overlay.styleSheet().replace(" ", "")
    assert _surface_rgba(snap.colors.mSurface).replace(" ", "") in sheet
    assert "#abcdef" in overlay.styleSheet().lower()
    assert "rgba(0,0,0" not in sheet
    assert "font-size" not in overlay.styleSheet()
    reset_controller_for_tests()


def test_badges_service_restyles_existing_overlays(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    overlay = _BadgeOverlay(
        terminal=SimpleNamespace(_term=host),
        template="X",
        color="#e74c3c",
        parent_widget=host,
    )
    service = BadgesService(window=SimpleNamespace())
    service._overlays[id(overlay)] = overlay
    snap = example_snapshot()
    old = overlay.styleSheet()

    _attach_snapshot(qapp, tmp_path, monkeypatch, snap)
    assert overlay.styleSheet() == old

    service.apply_presentation_update()
    sheet = overlay.styleSheet().replace(" ", "")
    assert _surface_rgba(snap.colors.mSurface).replace(" ", "") in sheet
    assert "#e74c3c" in overlay.styleSheet().lower()
    reset_controller_for_tests()


def test_mainwindow_presentation_update_restyles_badge_overlays(
    qtbot, qapp, tmp_path, monkeypatch
):
    host = QWidget()
    qtbot.addWidget(host)
    overlay = _BadgeOverlay(
        terminal=SimpleNamespace(_term=host),
        template="X",
        color="#abcdef",
        parent_widget=host,
    )
    snap = example_snapshot()
    old = overlay.styleSheet()
    assert _surface_rgba(snap.colors.mSurface).replace(" ", "") not in old.replace(
        " ", ""
    )

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

    class _Badges:
        def apply_presentation_update(self):
            overlay.apply_presentation_update()

    win = MainWindow.__new__(MainWindow)
    win._tab_bar = _EmptyBar()
    win._tabs = _EmptyTabs()
    win.iter_terminals = lambda: iter(())
    win.badges = _Badges()
    win.instant_replay = None
    win.timestamps = None
    win.tmux_share_plugin = None
    MainWindow.apply_presentation_update(win)

    sheet = overlay.styleSheet().replace(" ", "")
    assert _surface_rgba(snap.colors.mSurface).replace(" ", "") in sheet
    assert "#abcdef" in overlay.styleSheet().lower()
    assert "font-size" not in overlay.styleSheet()
    reset_controller_for_tests()
