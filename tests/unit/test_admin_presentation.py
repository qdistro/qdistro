"""Admin-app shared presentation chrome.

Cosmetic palette/font integration must not change permission decisions,
trusted request rendering, silo-state colors, age colors, or tray
severity paint.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
QtGui = pytest.importorskip("PyQt6.QtGui")

from PyQt6.QtGui import QFont, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "admin_app"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qdistro_admin_app import (  # noqa: E402
    DetailPane,
    SilosTab,
    _age_color,
    attach_presentation,
    reset_presentation_for_tests,
)


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    native_palette = QPalette(app.palette())
    native_style = app.style().objectName()
    native_font = QFont(app.font())
    native_ss = app.styleSheet()
    yield app
    reset_presentation_for_tests()
    app.setStyle(native_style)
    app.setPalette(native_palette)
    app.setFont(native_font)
    app.setStyleSheet(native_ss)


@pytest.fixture(autouse=True)
def _reset_presentation():
    reset_presentation_for_tests()
    yield
    reset_presentation_for_tests()


def _snapshot_path(tmp_path):
    from qdistro_presentation.paths import ResolvedPath

    return ResolvedPath(
        path=str(tmp_path / "current.json"),
        kind="override",
        expected_uid=None,
        watch=False,
    )


def test_attach_follows_snapshot_palette(qapp, tmp_path):
    from qdistro_presentation.model import example_snapshot
    from qdistro_presentation.publish import write_snapshot

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = attach_presentation(qapp, snapshot_path=_snapshot_path(tmp_path), watch=False)
    assert ctrl is not None
    assert (
        qapp.palette().color(QPalette.ColorRole.Window).name()
        == example_snapshot().colors.mSurface
    )
    assert (
        qapp.palette().color(QPalette.ColorRole.PlaceholderText).name()
        == example_snapshot().colors.mOnSurfaceVariant
    )
    ctrl.stop()


def test_missing_package_leaves_native_palette(qapp):
    native = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    real_import = __import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "qdistro_presentation" or name.startswith("qdistro_presentation."):
            raise ImportError("missing")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", fake):
        ctrl = attach_presentation(qapp)
    assert ctrl is None
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native


def test_detail_pane_uses_secondary_text_role_not_gray(qapp):
    pane = DetailPane()
    assert pane.lbl_exe.styleSheet() == ""
    assert pane.lbl_exe.foregroundRole() == QPalette.ColorRole.PlaceholderText
    assert pane.lbl_user.font().bold() or pane.lbl_user.font().weight() >= 600
    assert "gray" not in pane.lbl_exe.styleSheet().lower()
    assert "14pt" not in pane.lbl_user.styleSheet()


def test_age_color_unchanged():
    import time

    now = time.time()
    assert _age_color(now).name() == "#4caf50"
    assert _age_color(now - 60).name() == "#ffeb3b"
    assert _age_color(now - 180).name() == "#ff9800"
    assert _age_color(now - 400).name() == "#f44336"


def test_silo_state_colours_unchanged():
    assert SilosTab.STATE_COLOURS["Active"].name() == "#7bc97b"
    assert SilosTab.STATE_COLOURS["Stopped"].name() == "#d56b6b"
    assert SilosTab.STATE_COLOURS["Deleting"].name() == "#666666"
    assert SilosTab.STATE_COLOURS["Created"].name() == "#a0a0a0"


def test_live_update_refreshes_detail_heading_font(qapp, tmp_path):
    from dataclasses import replace

    from qdistro_presentation.model import example_snapshot, with_generation
    from qdistro_presentation.publish import write_snapshot

    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    ctrl = attach_presentation(qapp, snapshot_path=_snapshot_path(tmp_path), watch=False)
    pane = DetailPane()
    before = pane.lbl_user.font().pointSizeF()
    fonts = replace(example_snapshot().fonts, ui_scale=1.25)
    second = with_generation(replace(example_snapshot(), fonts=fonts))
    write_snapshot(str(tmp_path), second, require_unwritable_dirs=False, skip_unchanged=False)
    ctrl._reload()
    pane.apply_presentation_update()
    after = pane.lbl_user.font().pointSizeF()
    assert after > before
    ctrl.stop()
