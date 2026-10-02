"""Plugin titlebar extras restyle from presentation roles without pyte."""

from types import SimpleNamespace

from PyQt6.QtWidgets import QWidget
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.plugins.output_monitors import (
    BuildProgressMonitor,
    ErrorDetector,
    LogLevelColorizer,
)
from qterminator.theme import attach_presentation, reset_controller_for_tests
from qterminator.titlebar import TerminalTitlebar


def _follow_desktop(qapp, tmp_path, monkeypatch):
    reset_controller_for_tests()
    snap = example_snapshot()
    write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(
        qapp,
        SimpleNamespace(
            get=lambda *keys, default=None: (
                "system"
                if keys[:2] == ("general", "theme_mode")
                else {}
                if keys == ("appearance",)
                else default
            )
        ),
    )
    return snap


class _TitleTerm:
    def __init__(self, titlebar):
        self._titlebar = titlebar


def test_error_detector_paints_snapshot_error_role(qtbot, qapp, tmp_path, monkeypatch):
    """Error flags use mError, not a hardcoded red, and no pixel font-size."""
    host = QWidget()
    qtbot.addWidget(host)
    titlebar = TerminalTitlebar(host)
    snap = _follow_desktop(qapp, tmp_path, monkeypatch)
    titlebar.apply_presentation_update()
    ErrorDetector().on_snapshot(_TitleTerm(titlebar), {"lines": ["BUILD ERROR"]})
    style = titlebar._activity_label.styleSheet()
    assert snap.colors.mError in style
    assert "#e74c3c" not in style
    assert "font-size" not in style
    reset_controller_for_tests()


def test_build_progress_paints_snapshot_primary_role(qtbot, qapp, tmp_path, monkeypatch):
    host = QWidget()
    qtbot.addWidget(host)
    titlebar = TerminalTitlebar(host)
    snap = _follow_desktop(qapp, tmp_path, monkeypatch)
    titlebar.apply_presentation_update()
    BuildProgressMonitor()._process_text(_TitleTerm(titlebar), "[42/100]")
    style = titlebar._activity_label.styleSheet()
    assert snap.colors.mPrimary in style
    assert "#3498db" not in style
    assert "font-size" not in style
    reset_controller_for_tests()


def test_build_progress_visible_on_active_titlebar(qtbot, qapp, tmp_path, monkeypatch):
    """Primary progress uses on-primary on an active bar so the dot stays visible."""
    host = QWidget()
    qtbot.addWidget(host)
    titlebar = TerminalTitlebar(host)
    titlebar.set_active(True)
    snap = _follow_desktop(qapp, tmp_path, monkeypatch)
    assert snap.colors.mPrimary not in titlebar._activity_label.styleSheet()
    titlebar.apply_presentation_update()
    BuildProgressMonitor()._process_text(_TitleTerm(titlebar), "[42/100]")
    style = titlebar._activity_label.styleSheet()
    assert snap.colors.mPrimary in titlebar.styleSheet()
    assert snap.colors.mOnPrimary in style
    assert snap.colors.mPrimary not in style
    assert "#3498db" not in style
    assert "font-size" not in style
    reset_controller_for_tests()


def test_build_progress_role_survives_live_restyle(qtbot, qapp, tmp_path, monkeypatch):
    host = QWidget()
    qtbot.addWidget(host)
    titlebar = TerminalTitlebar(host)
    titlebar.set_active(True)
    BuildProgressMonitor()._process_text(_TitleTerm(titlebar), "[42/100]")
    old = titlebar._activity_label.styleSheet()
    snap = _follow_desktop(qapp, tmp_path, monkeypatch)
    assert titlebar._activity_label.styleSheet() == old
    titlebar.apply_presentation_update()
    style = titlebar._activity_label.styleSheet()
    assert snap.colors.mOnPrimary in style
    assert snap.colors.mPrimary not in style
    assert snap.colors.mTertiary not in style
    reset_controller_for_tests()


def test_log_level_info_uses_secondary_role(qtbot, qapp, tmp_path, monkeypatch):
    host = QWidget()
    qtbot.addWidget(host)
    titlebar = TerminalTitlebar(host)
    snap = _follow_desktop(qapp, tmp_path, monkeypatch)
    titlebar.apply_presentation_update()
    LogLevelColorizer().on_snapshot(_TitleTerm(titlebar), {"lines": ["INFO ok"]})
    style = titlebar._activity_label.styleSheet()
    assert snap.colors.mSecondary in style
    assert "#2ecc71" not in style
    assert "font-size" not in style
    reset_controller_for_tests()


def test_error_role_survives_live_restyle(qtbot, qapp, tmp_path, monkeypatch):
    """Stored error role is reapplied on presentation update."""
    host = QWidget()
    qtbot.addWidget(host)
    titlebar = TerminalTitlebar(host)
    ErrorDetector().on_snapshot(_TitleTerm(titlebar), {"lines": ["FATAL error"]})
    old = titlebar._activity_label.styleSheet()
    snap = _follow_desktop(qapp, tmp_path, monkeypatch)
    assert titlebar._activity_label.styleSheet() == old
    titlebar.apply_presentation_update()
    style = titlebar._activity_label.styleSheet()
    assert snap.colors.mError in style
    assert snap.colors.mTertiary not in style
    assert "#e74c3c" not in style
    reset_controller_for_tests()
