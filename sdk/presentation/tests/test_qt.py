"""Qt controller: native capture, snapshot apply, last-known-good, watches."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QApplication, QLabel
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ResolvedPath
from qdistro_presentation.publish import write_snapshot
from qdistro_presentation.qt import PresentationController, snapshot_palette


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def _path(tmp_path) -> ResolvedPath:
    return ResolvedPath(
        path=str(tmp_path / "current.json"),
        kind="override",
        expected_uid=None,
        watch=True,
    )


def test_native_restore_and_snapshot_palette(qapp, tmp_path):
    native_window = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    snap_window = qapp.palette().color(QPalette.ColorRole.Window)
    assert snap_window.name() == example_snapshot().colors.mSurface
    highlight = qapp.palette().color(QPalette.ColorRole.HighlightedText).name()
    assert highlight == example_snapshot().colors.mOnPrimary
    ctrl.set_theme_mode("native")
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native_window
    ctrl.stop()


def test_selection_uses_on_primary_not_white():
    pal = snapshot_palette(example_snapshot().colors)
    assert pal.color(QPalette.ColorRole.HighlightedText).name() == "#0e0e43"
    assert pal.color(QPalette.ColorRole.Highlight).name() == "#fff59b"


def test_missing_file_uses_fallback_then_recovers(qapp, tmp_path):
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    assert ctrl.state.using_shared_palette is False
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl._reload()
    assert ctrl.state.using_shared_palette is True
    ctrl.stop()


def test_malformed_keeps_last_good(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    generation = ctrl.state.generation
    (tmp_path / "current.json").write_text("{not json", encoding="utf-8")
    ctrl._reload()
    assert ctrl.state.generation == generation
    ctrl.stop()


def test_deletion_keeps_last_good(qapp, tmp_path):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="system", snapshot_path=_path(tmp_path), watch=False
    )
    (tmp_path / "current.json").unlink()
    ctrl._reload()
    assert ctrl.state.using_shared_palette is True
    ctrl.stop()


def test_local_override_survives_snapshot(qapp, tmp_path):
    from qdistro_presentation.model import LocalOverrides

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp,
        theme_mode="system",
        local=LocalOverrides(ui_font_family="DoesNotExistFontXYZ"),
        snapshot_path=_path(tmp_path),
        watch=False,
    )
    # Unavailable family falls back at apply time; resolved state keeps the
    # requested override so it can round-trip in app config.
    assert ctrl.state.ui_family == "DoesNotExistFontXYZ"
    ctrl.stop()


def test_changed_signal_and_existing_widget(qapp, tmp_path):
    label = QLabel("hello")
    seen = []
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = PresentationController(
        qapp, theme_mode="native", snapshot_path=_path(tmp_path), watch=False
    )
    ctrl.changed.connect(lambda old, new, fields: seen.append(fields))
    ctrl.set_theme_mode("system")
    assert seen
    assert "using_shared_palette" in seen[0]
    label.setParent(None)
    ctrl.stop()
