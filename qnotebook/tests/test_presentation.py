"""Shared presentation chrome for qnotebook: attach, pin, two windows."""

from __future__ import annotations

import sys

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QFont, QPalette
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qnotebook.appearance import SettingsAdapter, save_overrides
from qnotebook.editor import (
    BODY_POINT_SIZE,
    native_body_font,
    reset_pinned_body_font_for_tests,
)
from qnotebook.theme import (
    apply_theme,
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


def _distinct_ui_family(native_family: str) -> str:
    from PyQt6.QtGui import QFontDatabase

    for family in QFontDatabase.families():
        if family and family != native_family:
            return family
    pytest.skip("need two installed font families")


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
    ui_family = _distinct_ui_family(native_family)
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    adapter = _adapter("system")
    save_overrides(adapter._settings, {
        "version": 1,
        "ui_font_family": ui_family,
        "ui_font_size_pt": 18.0,
    })
    attach_presentation(qapp, adapter)
    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    assert qapp.font().family() != native_family
    assert qapp.font().pointSizeF() == 18.0
    assert win.editor.font().family() == native_family
    assert win.editor.font().pointSize() == BODY_POINT_SIZE
    assert win.editor.document().defaultFont().family() == native_family
    assert native_body_font().family() == native_family
    assert "palette(base)" in win.editor.styleSheet()


def test_repeated_attach_does_not_replace_native_pin(
    qapp, tmp_path, monkeypatch,
):
    native_family = qapp.font().family()
    ui_family = _distinct_ui_family(native_family)
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    adapter = _adapter("system")
    save_overrides(adapter._settings, {
        "version": 1,
        "ui_font_family": ui_family,
        "ui_font_size_pt": 18.0,
    })
    first = attach_presentation(qapp, adapter)
    second = attach_presentation(qapp, adapter)
    assert first is second
    assert qapp.font().family() != native_family
    assert native_body_font().family() == native_family
    assert qapp.font().pointSizeF() == 18.0


def test_system_restores_captured_native_palette(qapp):
    from PyQt6.QtGui import QColor

    native = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
    attach_presentation(qapp, _adapter("system"))
    apply_theme(qapp, "dark")
    assert qapp.palette().color(QPalette.ColorRole.Window) == QColor("#2b2b2b")
    apply_theme(qapp, "system")
    assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == native


@pytest.mark.parametrize("with_qss", [False, True])
def test_native_restores_captured_style_after_fusion(qapp, with_qss):
    from PyQt6.QtGui import QColor, QPalette
    from PyQt6.QtWidgets import QStyleFactory
    from qnotebook.theme import _underlying_style_name, reset_controller_for_tests

    windows = next((n for n in QStyleFactory.keys() if n.lower() == "windows"), None)
    if windows is None:
        pytest.skip("Windows style required to distinguish Fusion")
    original_style = qapp.style().objectName()
    original_pal = QPalette(qapp.palette())
    original_qss = qapp.styleSheet()
    reset_controller_for_tests()
    try:
        qapp.setStyle(windows)
        pal = QPalette(qapp.palette())
        pal.setColor(QPalette.ColorRole.Window, QColor("#c8dcc8"))
        qapp.setPalette(pal)
        qapp.setStyleSheet("QWidget { background-color: #c8dcc8; }" if with_qss else "")
        window_rgb = qapp.palette().color(QPalette.ColorRole.Window).getRgb()
        qss = qapp.styleSheet()
        apply_theme(qapp, "dark")
        assert qapp.palette().color(QPalette.ColorRole.Window) == QColor("#2b2b2b")
        assert _underlying_style_name(qapp).lower() == "fusion"
        assert apply_theme(qapp, "native") == "native"
        assert _underlying_style_name(qapp).lower() == "windows"
        assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == window_rgb
        assert qapp.styleSheet() == qss
    finally:
        reset_controller_for_tests()
        qapp.setStyle(original_style)
        qapp.setPalette(original_pal)
        qapp.setStyleSheet(original_qss)


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
    first.act_appearance_dark.trigger()
    qapp.processEvents()
    assert first.act_appearance_dark.isChecked()
    assert second.act_appearance_dark.isChecked()
    assert first.editor.markdown() == md
    assert first.editor.is_dirty() is dirty


def test_two_windows_sync_mode_without_controller(qapp, tmp_notebook, qtbot):
    from qnotebook.settings_dialog import SettingsDialog
    from qnotebook.theme import current_controller as _ctrl

    assert _ctrl() is None
    first = MainWindow()
    first.open_notebook(str(tmp_notebook))
    qtbot.addWidget(first)
    second = MainWindow()
    second.open_notebook(str(tmp_notebook))
    qtbot.addWidget(second)
    dlg = SettingsDialog(second)
    qtbot.addWidget(dlg)
    first.act_appearance_dark.trigger()
    qapp.processEvents()
    assert first.act_appearance_dark.isChecked()
    assert second.act_appearance_dark.isChecked()
    assert dlg._combo_appearance.currentText() == "Dark"


def test_theme_change_does_not_dirty_or_rewrite_editor(
    qapp, tmp_notebook, qtbot,
):
    from PyQt6.QtGui import QTextCursor

    win = MainWindow()
    win.open_notebook(str(tmp_notebook))
    qtbot.addWidget(win)
    win.load_page("Home")
    cur = win.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\nunsaved chrome-test edit")
    win.editor.setTextCursor(cur)
    md = win.editor.markdown()
    assert win.editor.is_dirty() is True
    stack = win.editor.document().availableUndoSteps()
    assert stack > 0
    win.act_appearance_dark.trigger()
    qapp.processEvents()
    assert win.editor.markdown() == md
    assert win.editor.is_dirty() is True
    assert win.editor.document().availableUndoSteps() == stack
    win.act_appearance_light.trigger()
    qapp.processEvents()
    assert win.editor.markdown() == md
    assert win.editor.is_dirty() is True


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
    from qnotebook.__main__ import main

    called = {"n": 0}

    def _boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("PDF export must not attach a presentation watcher")

    monkeypatch.setattr("qnotebook.theme.attach_presentation", _boom)
    out = tmp_path / "Home.pdf"
    rc = main(
        ["qnotebook", "--export", str(tmp_notebook), "Home",
         "--format", "pdf", "--output", str(out)]
    )
    assert rc == 0
    assert called["n"] == 0
    assert out.is_file()
    assert out.read_bytes()[:4] == b"%PDF"
