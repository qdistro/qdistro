"""Terminal content font/ANSI inheritance from the presentation snapshot."""

from __future__ import annotations

from dataclasses import replace

import pytest
import qterminator.config as config_mod
from PyQt6.QtWidgets import QApplication
from qdistro_presentation.model import example_snapshot, with_generation
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qterminator.config import Config
from qterminator.preferences import PreferencesDialog
from qterminator.theme import attach_presentation, reset_controller_for_tests
from qterminator.window import MainWindow


def _fixed_snapshot(family: str, scale: float, *, mode: str = "dark"):
    snap = example_snapshot()
    snap = replace(
        snap,
        mode=mode,
        fonts=replace(snap.fonts, fixed_family=family, fixed_scale=scale),
    )
    return with_generation(snap)


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture(autouse=True)
def _reset_presentation():
    reset_controller_for_tests()
    yield
    reset_controller_for_tests()


@pytest.fixture
def window(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    return win


def test_desktop_font_uses_snapshot_fixed_family(qtbot, tmp_path, monkeypatch):
    write_snapshot(
        str(tmp_path),
        _fixed_snapshot("Hack", 1.2),
        require_unwritable_dirs=False,
    )
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(QApplication.instance(), Config())
    win = MainWindow()
    qtbot.addWidget(win)
    font = win._active_terminal.term.getTerminalFont()
    assert font.family() == "Hack"
    assert font.pointSizeF() == pytest.approx(13.2)


def test_local_font_ignores_snapshot(qtbot, tmp_path, monkeypatch):
    cfg = Config()
    cfg.set("profiles", "default", "font_source", "local")
    cfg.set("profiles", "default", "font_family", "Monospace")
    cfg.set("profiles", "default", "font_size", 11)
    write_snapshot(
        str(tmp_path),
        _fixed_snapshot("Hack", 1.2),
        require_unwritable_dirs=False,
    )
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(QApplication.instance(), Config())
    win = MainWindow()
    qtbot.addWidget(win)
    font = win._active_terminal.term.getTerminalFont()
    assert font.family() == "Monospace"
    assert font.pointSize() == 11


def test_live_desktop_font_keeps_zoom_and_pty(qtbot, tmp_path, monkeypatch):
    write_snapshot(
        str(tmp_path),
        _fixed_snapshot("Hack", 1.0),
        require_unwritable_dirs=False,
    )
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(QApplication.instance(), Config())
    win = MainWindow()
    qtbot.addWidget(win)
    term = win._active_terminal
    started = {"n": 0}
    orig = term.term.startShellProgram

    def _count():
        started["n"] += 1
        orig()

    term.term.startShellProgram = _count
    cwd = term.term.workingDirectory()
    term.zoom_in()
    term.zoom_in()
    assert term.term.getTerminalFont().pointSizeF() == pytest.approx(13.0)

    write_snapshot(
        str(tmp_path),
        _fixed_snapshot("Fira Code", 1.2),
        require_unwritable_dirs=False,
    )
    from qterminator.theme import current_controller

    current_controller()._reload()
    font = term.term.getTerminalFont()
    assert font.family() == "Fira Code"
    assert font.pointSizeF() == pytest.approx(15.2)
    assert started["n"] == 0
    assert term.term.workingDirectory() == cwd
    assert term.profile_name == "default"
    win._zoom_normal()
    assert term.term.getTerminalFont().pointSizeF() == pytest.approx(13.2)


def test_zoom_reset_uses_this_terminal_profile(qtbot):
    cfg = Config()
    cfg.set_profile(
        "other",
        {
            "font_family": "Monospace",
            "font_size": 16,
            "font_source": "local",
            "color_scheme": "Linux",
            "color_source": "profile",
            "scrollback_lines": 5000,
        },
    )
    win = MainWindow()
    qtbot.addWidget(win)
    win._split_horizontal()
    other = [t for t in win.iter_terminals() if t is not win._active_terminal][0]
    other.apply_profile("other")
    other.zoom_in()
    other.zoom_in()
    win._active_terminal = other
    win._zoom_normal()
    assert other.term.getTerminalFont().pointSize() == 16
    assert win._active_terminal.term.getTerminalFont().pointSize() == 16


def test_theme_apply_does_not_recolor_profile_terminals(window, qtbot):
    cfg = Config()
    cfg.set("profiles", "default", "color_source", "profile")
    cfg.set("profiles", "default", "color_scheme", "Linux")
    cfg.set("general", "light_color_scheme", "BlackOnWhite")
    window._active_terminal.apply_profile_fields()
    calls = []
    window.apply_color_scheme_to_all = lambda scheme: calls.append(scheme)
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)
    dlg._theme_mode.setCurrentIndex(2)  # Light
    dlg._apply()
    assert calls == []
    assert window._active_terminal._applied_scheme == "Linux"


def test_dark_scheme_edit_refreshes_opted_in_not_profile(qtbot, tmp_path, monkeypatch):
    cfg = Config()
    cfg.set("general", "theme_mode", "dark")
    cfg.set("general", "dark_color_scheme", "Linux")
    cfg.set("general", "light_color_scheme", "BlackOnWhite")
    cfg.set("profiles", "default", "color_source", "profile")
    cfg.set("profiles", "default", "color_scheme", "Linux")
    cfg.set("profiles", "default", "font_source", "local")
    cfg.set_profile(
        "opted",
        {
            "font_family": "Monospace",
            "font_size": 11,
            "font_source": "local",
            "color_scheme": "Linux",
            "color_source": "appearance-mode",
            "scrollback_lines": 5000,
        },
    )
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(QApplication.instance(), cfg)
    win = MainWindow()
    qtbot.addWidget(win)
    win._split_horizontal()
    opted = [t for t in win.iter_terminals() if t is not win._active_terminal][0]
    opted.apply_profile("opted")
    assert opted._applied_scheme == "Linux"
    dlg = PreferencesDialog(win)
    qtbot.addWidget(dlg)
    dlg._theme_mode.setCurrentIndex(1)  # Dark, unchanged
    idx = dlg._dark_color_scheme.findText("WhiteOnBlack")
    assert idx >= 0
    dlg._dark_color_scheme.setCurrentIndex(idx)
    calls = []
    win.apply_color_scheme_to_all = lambda scheme: calls.append(scheme)
    dlg._apply()
    assert calls == []
    assert opted._applied_scheme == "WhiteOnBlack"
    assert win._active_terminal._applied_scheme == "Linux"


def test_profile_font_apply_reaches_other_windows(qtbot):
    cfg = Config()
    cfg.set("profiles", "default", "font_source", "local")
    cfg.set("profiles", "default", "font_family", "Monospace")
    cfg.set("profiles", "default", "font_size", 11)
    cfg.set_profile(
        "other",
        {
            "font_family": "Monospace",
            "font_size": 11,
            "font_source": "local",
            "color_scheme": "Linux",
            "color_source": "profile",
            "scrollback_lines": 5000,
        },
    )
    win_a = MainWindow()
    win_b = MainWindow()
    qtbot.addWidget(win_a)
    qtbot.addWidget(win_b)
    win_a._split_horizontal()
    other = [t for t in win_a.iter_terminals() if t is not win_a._active_terminal][0]
    other.apply_profile("other")
    dlg = PreferencesDialog(win_a)
    qtbot.addWidget(dlg)
    dlg._font_size.setValue(16)
    dlg._apply()
    assert win_a._active_terminal.term.getTerminalFont().pointSize() == 16
    assert win_b._active_terminal.term.getTerminalFont().pointSize() == 16
    assert other.term.getTerminalFont().pointSize() == 11


def test_appearance_mode_follows_theme_only_for_opted_in(qtbot):
    cfg = Config()
    cfg.set("general", "dark_color_scheme", "Linux")
    cfg.set("general", "light_color_scheme", "BlackOnWhite")
    cfg.set_profile(
        "opted",
        {
            "font_family": "Monospace",
            "font_size": 11,
            "font_source": "local",
            "color_scheme": "Linux",
            "color_source": "appearance-mode",
            "scrollback_lines": 5000,
        },
    )
    win = MainWindow()
    qtbot.addWidget(win)
    win._split_horizontal()
    opted = [t for t in win.iter_terminals() if t is not win._active_terminal][0]
    opted.apply_profile("opted")
    cfg.set("general", "theme_mode", "light")
    from qterminator.theme import apply_theme

    apply_theme(QApplication.instance(), "light")
    # No controller: appearance-mode uses detect_system_theme unless we
    # attach. Drive the inherit path directly after setting explicit mode
    # through a controller-less apply_inherited_presentation by patching.
    from qterminator import terminal_style

    orig_mode = terminal_style.effective_appearance_mode
    terminal_style.effective_appearance_mode = lambda: "light"
    try:
        opted.apply_inherited_presentation()
        win._active_terminal.apply_inherited_presentation()
        assert opted._applied_scheme == "BlackOnWhite"
        assert win._active_terminal._applied_scheme == "Linux"
    finally:
        terminal_style.effective_appearance_mode = orig_mode


def test_desktop_apply_does_not_serialize_effective_family(window, qtbot):
    cfg = Config()
    cfg.set("profiles", "default", "font_source", "desktop")
    cfg.set("profiles", "default", "font_family", "Monospace")
    cfg.set("profiles", "default", "font_size", 11)
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)
    assert dlg._chk_desktop_font.isChecked()
    dlg._apply()
    assert cfg.get("profiles", "default", "font_source") == "desktop"
    assert cfg.get("profiles", "default", "font_family") == "Monospace"
    assert cfg.get("profiles", "default", "font_size") == 11


def test_font_preview_uses_selected_scheme_background(window, qtbot):
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)
    dlg._chk_appearance_colors.setChecked(False)
    idx = dlg._color_scheme.findText("BlackOnWhite")
    assert idx >= 0
    dlg._color_scheme.setCurrentIndex(idx)
    assert "background: #ffffff" in dlg._font_preview.styleSheet()


def test_preferences_has_desktop_font_checkbox(window, qtbot):
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)
    assert dlg._chk_desktop_font.text() == "Use desktop monospace font"
    assert dlg._chk_appearance_colors.text() == (
        "Use application appearance for terminal colors"
    )
