"""Shared presentation chrome for qnotebook: attach, pin, two windows."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QFont, QPalette
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qnotebook.appearance import SettingsAdapter
from qnotebook.editor import (
    BODY_POINT_SIZE,
    native_body_font,
    pin_native_body_font,
    reset_pinned_body_font_for_tests,
)
from qnotebook.theme import (
    EDITOR_PALETTE_QSS,
    attach_presentation,
    current_controller,
    reset_controller_for_tests,
)
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield


@pytest.fixture(autouse=True)
def _reset_presentation(qapp):
    if not hasattr(qapp, "_qnotebook_native_font"):
        qapp._qnotebook_native_font = QFont(qapp.font())
    reset_controller_for_tests()
    reset_pinned_body_font_for_tests()
    qapp.setPalette(QPalette())
    qapp.setStyleSheet("")
    qapp.setFont(QFont(qapp._qnotebook_native_font))
    yield
    reset_controller_for_tests()
    reset_pinned_body_font_for_tests()
    qapp.setPalette(QPalette())
    qapp.setStyleSheet("")
    qapp.setFont(QFont(qapp._qnotebook_native_font))


def _adapter(theme_mode: str = "system"):
    s = QSettings("qnotebook", "qnotebook")
    from qnotebook.appearance import save_theme_mode

    save_theme_mode(s, theme_mode, update_legacy=(theme_mode != "system"))
    return SettingsAdapter(s)


def test_attach_presentation_follows_snapshot(qapp, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    ctrl = attach_presentation(qapp, _adapter("system"))
    assert ctrl is not None
    assert ctrl.state.using_shared_palette is True
    assert qapp.palette().color(QPalette.ColorRole.Window).name() == example_snapshot().colors.mSurface
    assert qapp.palette().color(QPalette.ColorRole.HighlightedText).name() == (
        example_snapshot().colors.mOnPrimary
    )


def test_attach_presentation_native_ignores_snapshot(qapp, tmp_path, monkeypatch):
    native = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    ctrl = attach_presentation(qapp, _adapter("native"))
    assert ctrl is not None
    assert ctrl.theme_mode == "native"
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native


def test_missing_snapshot_keeps_native_fallback(qapp, tmp_path, monkeypatch):
    native = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "missing.json"))
    ctrl = attach_presentation(qapp, _adapter("system"))
    assert ctrl is not None
    assert ctrl.state.using_shared_palette is False
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native


def test_editor_keeps_pinned_body_font_when_ui_font_applies(
    qapp, tmp_path, tmp_notebook, qtbot, monkeypatch,
):
    native_family = qapp.font().family()
    pin_native_body_font(qapp.font())
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    assert win.editor.font().family() == native_family
    assert win.editor.font().pointSize() == BODY_POINT_SIZE
    assert native_body_font().family() == native_family
    assert qapp.font().pointSizeF() != 0
    # Shared UI font may differ from the pinned body family; the document widget
    # must not have followed QApplication.setFont.
    assert win.editor.font().family() == native_family
    assert EDITOR_PALETTE_QSS.split("{")[0].strip() in win.editor.styleSheet() or (
        "palette(base)" in win.editor.styleSheet()
    )


def test_two_windows_follow_same_generation_without_resetting(
    qapp, tmp_path, tmp_notebook, qtbot, monkeypatch,
):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, _adapter("system"))
    first = MainWindow()
    first.open_notebook(str(tmp_notebook))
    qtbot.addWidget(first)
    first.load_page("Home")
    md = first.editor.markdown()
    dirty = first.editor.is_dirty()
    generation = current_controller().state.generation
    second = MainWindow()
    second.open_notebook(str(tmp_notebook))
    qtbot.addWidget(second)
    qapp.processEvents()
    assert current_controller().state.generation == generation
    assert first.editor.markdown() == md
    assert first.editor.is_dirty() is dirty
    assert first.act_appearance_system.isChecked()
    assert second.act_appearance_system.isChecked()
    assert qapp.palette().color(QPalette.ColorRole.Window).name() == example_snapshot().colors.mSurface


def test_theme_change_does_not_dirty_or_rewrite_editor(
    qapp, tmp_notebook, qtbot,
):
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    win.load_page("Home")
    md = win.editor.markdown()
    dirty = win.editor.is_dirty()
    stack = win.editor.document().availableUndoSteps()
    win.act_appearance_dark.trigger()
    qapp.processEvents()
    assert win.editor.markdown() == md
    assert win.editor.is_dirty() is dirty
    assert win.editor.document().availableUndoSteps() == stack
    win.act_appearance_light.trigger()
    qapp.processEvents()
    assert win.editor.markdown() == md
    assert win.editor.is_dirty() is dirty


def test_package_import_is_stdlib_only():
    import os
    import subprocess
    from pathlib import Path

    script = (
        "import sys, qnotebook; "
        "assert 'PyQt6' not in sys.modules; "
        "assert 'qdistro_presentation' not in sys.modules"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr


def test_cli_pdf_export_does_not_attach_presentation(
    tmp_notebook, tmp_path, monkeypatch, qapp,
):
    from qnotebook import cli

    called = {"n": 0}

    def _boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("PDF export must not attach a presentation watcher")

    monkeypatch.setattr("qnotebook.theme.attach_presentation", _boom)
    out = tmp_path / "Home.pdf"
    rc = cli.run(
        ["--export", str(tmp_notebook), "Home", "--format", "pdf", "--output", str(out)]
    )
    assert rc == 0
    assert called["n"] == 0
    assert out.is_file()
    assert out.read_bytes()[:4] == b"%PDF"
