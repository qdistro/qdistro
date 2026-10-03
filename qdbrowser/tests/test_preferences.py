"""Appearance preferences dialog persistence and live apply."""

from __future__ import annotations

from dataclasses import replace

import pytest
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QWidget
from qdbrowser.preferences import THEME_KEY_TO_LABEL, PreferencesDialog
from qdbrowser.theme import (
    attach_presentation,
    current_controller,
    reset_controller_for_tests,
)
from qdistro_presentation.model import (
    DESKTOP_SETTINGS_UNAVAILABLE,
    example_snapshot,
    with_generation,
)
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot


@pytest.fixture
def isolated_config(fresh_config):
    yield fresh_config.Config()
    reset_controller_for_tests()


@pytest.fixture(autouse=True)
def _reset_presentation(qapp):
    reset_controller_for_tests()
    yield
    reset_controller_for_tests()


def test_dialog_loads_follow_desktop_default(qapp, isolated_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        assert dlg.combo_theme.currentText() == "Follow desktop"
        assert dlg.cb_desktop_fonts.isChecked() is True
        assert dlg.combo_ui_font.isEnabled() is False
        assert dlg.spin_ui_font_size.isEnabled() is False
    finally:
        dlg.deleteLater()


def test_dialog_loads_existing_theme_modes(qapp, isolated_config):
    for mode, label in THEME_KEY_TO_LABEL.items():
        isolated_config.set("general", "theme_mode", mode)
        dlg = PreferencesDialog(isolated_config)
        try:
            assert dlg.combo_theme.currentText() == label
        finally:
            dlg.deleteLater()


def test_apply_persists_theme_and_appearance(qapp, isolated_config, fresh_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_theme.setCurrentText("Dark")
        dlg.cb_desktop_fonts.setChecked(True)
        dlg._apply()
    finally:
        dlg.deleteLater()

    fresh_config.Config._instance = None
    fresh = fresh_config.Config()
    assert fresh.get("general", "theme_mode") == "dark"
    appearance = fresh.get("appearance", default={})
    assert appearance.get("version") == 1
    assert "ui_font_family" not in appearance
    assert "ui_font_size_pt" not in appearance


def test_native_and_light_persist(qapp, isolated_config, fresh_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_theme.setCurrentText("Native")
        dlg._apply()
        dlg.combo_theme.setCurrentText("Light")
        dlg._apply()
    finally:
        dlg.deleteLater()
    fresh_config.Config._instance = None
    fresh = fresh_config.Config()
    assert fresh.get("general", "theme_mode") == "light"


def test_follow_desktop_and_desktop_fonts_persist(qapp, isolated_config, fresh_config):
    isolated_config.set(
        "appearance",
        {"version": 1, "ui_font_family": "Inter", "ui_font_size_pt": 14.0},
    )
    isolated_config.set("general", "theme_mode", "dark")
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_theme.setCurrentText("Follow desktop")
        dlg.cb_desktop_fonts.setChecked(True)
        dlg._apply()
    finally:
        dlg.deleteLater()
    fresh_config.Config._instance = None
    fresh = fresh_config.Config()
    assert fresh.get("general", "theme_mode") == "system"
    appearance = fresh.get("appearance", default={})
    assert appearance.get("version") == 1
    assert "ui_font_family" not in appearance
    assert "ui_font_size_pt" not in appearance


def test_apply_preserves_unrelated_appearance_overrides(qapp, isolated_config):
    isolated_config.set(
        "appearance",
        {
            "version": 1,
            "tooltips_enabled": False,
            "icon_theme": "Adwaita",
            "ui_scale": 1.1,
            "ui_font_family": "Inter",
        },
    )
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.cb_desktop_fonts.setChecked(False)
        dlg._apply()
    finally:
        dlg.deleteLater()
    appearance = isolated_config.get("appearance", default={})
    assert appearance.get("tooltips_enabled") is False
    assert appearance.get("icon_theme") == "Adwaita"
    assert appearance.get("ui_scale") == 1.1
    assert appearance.get("ui_font_family")
    assert "ui_font_size_pt" not in appearance


def test_apply_preserves_unrelated_plugin_keys(qapp, isolated_config):
    isolated_config.set("plugins", "bridge_adapter", "auto_register", False)
    isolated_config.set("dark_mode", "default", "never")
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_theme.setCurrentText("Dark")
        dlg._apply()
    finally:
        dlg.deleteLater()
    assert isolated_config.get("plugins", "bridge_adapter", "auto_register") is False
    assert isolated_config.get("dark_mode", "default") == "never"


def test_theme_only_apply_keeps_size_only_override(
    qapp, isolated_config, tmp_path, monkeypatch
):
    isolated_config.set("appearance", {"version": 1, "ui_font_size_pt": 14.0})
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, isolated_config)
    dlg = PreferencesDialog(isolated_config)
    try:
        assert dlg.cb_desktop_fonts.isChecked() is False
        dlg.combo_theme.setCurrentText("Dark")
        dlg._apply()
    finally:
        dlg.deleteLater()
    appearance = isolated_config.get("appearance", default={})
    assert appearance.get("ui_font_size_pt") == 14.0
    assert "ui_font_family" not in appearance
    ctrl = current_controller()
    assert ctrl is not None
    assert ctrl.state.theme_mode == "dark"
    assert ctrl.state.ui_family == example_snapshot().fonts.ui_family


def test_explicit_eleven_point_size_is_saved(qapp, isolated_config):
    isolated_config.set("appearance", {"version": 1, "ui_font_family": "Inter"})
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.cb_desktop_fonts.setChecked(False)
        dlg.spin_ui_font_size.setValue(10)
        dlg.spin_ui_font_size.setValue(11)
        dlg._apply()
    finally:
        dlg.deleteLater()
    appearance = isolated_config.get("appearance", default={})
    assert appearance.get("ui_font_size_pt") == 11.0


def test_cancel_does_not_write_inherited_fonts(qapp, isolated_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_ui_font.setCurrentText("Serif")
        dlg.combo_theme.setCurrentText("Dark")
        dlg.reject()
    finally:
        dlg.deleteLater()
    appearance = isolated_config.get("appearance", default={}) or {}
    assert appearance.get("ui_font_family") is None
    assert isolated_config.get("general", "theme_mode") == "system"


def test_accept_applies_before_closing(qapp, isolated_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_theme.setCurrentText("Native")
        dlg.accept()
    finally:
        dlg.deleteLater()
    assert isolated_config.get("general", "theme_mode") == "native"


class _ProbeWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.updates = 0

    def apply_presentation_update(self) -> None:
        self.updates += 1


def test_apply_updates_controller_and_windows(qapp, isolated_config, tmp_path, monkeypatch):
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, isolated_config)
    probe = _ProbeWindow()
    probe.show()
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_theme.setCurrentText("Dark")
        dlg._apply()
    finally:
        dlg.deleteLater()
        probe.deleteLater()
    ctrl = current_controller()
    assert ctrl is not None
    assert ctrl.state.theme_mode == "dark"
    assert isolated_config.get("general", "theme_mode") == "dark"
    assert probe.updates >= 1
    assert (
        qapp.palette().color(QPalette.ColorRole.Window).name()
        != example_snapshot().colors.mSurface
    )


def test_apply_ui_font_override_reaches_controller(
    qapp, isolated_config, tmp_path, monkeypatch
):
    from PyQt6.QtGui import QFontDatabase

    families = QFontDatabase.families()
    assert families
    family = families[0]
    write_snapshot(str(tmp_path), example_snapshot(), require_unwritable_dirs=False)
    monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
    attach_presentation(qapp, isolated_config)
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.cb_desktop_fonts.setChecked(False)
        dlg.combo_ui_font.setCurrentText(family)
        dlg.spin_ui_font_size.setValue(18)
        dlg._apply()
    finally:
        dlg.deleteLater()
    appearance = isolated_config.get("appearance", default={})
    assert appearance.get("ui_font_family") == family
    assert appearance.get("ui_font_size_pt") == 18.0
    ctrl = current_controller()
    assert ctrl is not None
    assert ctrl.state.ui_family == family
    assert abs(ctrl.state.content_ui_point_size - 18.0) < 0.01


def _windows_style_name():
    from PyQt6.QtWidgets import QStyleFactory

    for name in QStyleFactory.keys():
        if name.lower() == "windows":
            return name
    pytest.skip("Windows style required to distinguish Fusion")


def _prime_native_baseline(app, *, with_qss):
    from PyQt6.QtGui import QColor
    from qdbrowser.theme import _underlying_style_name

    style_name = _windows_style_name()
    app.setStyle(style_name)
    pal = QPalette(app.palette())
    pal.setColor(QPalette.ColorRole.Window, QColor("#c8dcc8"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#dce8dc"))
    app.setPalette(pal)
    app.setStyleSheet("QWidget { background-color: #c8dcc8; }" if with_qss else "")
    return (
        _underlying_style_name(app).lower(),
        app.palette().color(QPalette.ColorRole.Window).getRgb(),
        app.styleSheet(),
    )


@pytest.mark.parametrize("with_qss", [False, True])
def test_native_without_controller_restores_captured_baseline(
    qapp, isolated_config, with_qss
):
    from qdbrowser.theme import (
        _underlying_style_name,
        apply_theme,
        current_controller,
    )

    original_style = _underlying_style_name(qapp)
    original_pal = QPalette(qapp.palette())
    original_qss = qapp.styleSheet()
    reset_controller_for_tests()
    try:
        style, window_rgb, qss = _prime_native_baseline(qapp, with_qss=with_qss)
        assert style == "windows"
        assert current_controller() is None
        dlg = PreferencesDialog(isolated_config)
        try:
            dlg.combo_theme.setCurrentText("Dark")
            dlg._apply()
            assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() != window_rgb
            if with_qss:
                assert qapp.styleSheet() != qss
            dlg.combo_theme.setCurrentText("Native")
            dlg._apply()
        finally:
            dlg.deleteLater()
        assert current_controller() is None
        assert _underlying_style_name(qapp).lower() == "windows"
        assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == window_rgb
        assert qapp.styleSheet() == qss
        assert isolated_config.get("general", "theme_mode") == "native"
        apply_theme(qapp, "light")
        dlg = PreferencesDialog(isolated_config)
        try:
            dlg.combo_theme.setCurrentText("Native")
            dlg._apply()
        finally:
            dlg.deleteLater()
        assert _underlying_style_name(qapp).lower() == "windows"
        assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == window_rgb
        assert qapp.styleSheet() == qss
    finally:
        reset_controller_for_tests()
        qapp.setStyle(original_style)
        qapp.setPalette(original_pal)
        qapp.setStyleSheet(original_qss)


def test_missing_controller_still_applies_legacy_theme(
    qapp, isolated_config, monkeypatch
):
    from qdbrowser import theme as theme_mod

    monkeypatch.setattr(theme_mod, "current_controller", lambda: None)
    applied = []

    def _fake_apply(app, mode):
        applied.append(mode)
        return mode

    monkeypatch.setattr(theme_mod, "apply_theme", _fake_apply)

    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_theme.setCurrentText("Light")
        dlg._apply()
    finally:
        dlg.deleteLater()
    assert isolated_config.get("general", "theme_mode") == "light"
    assert "light" in applied


def test_preferences_dialog_apply_presentation_update_polishes(qapp, isolated_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.show()
        dlg.apply_presentation_update()
        assert dlg.isVisible()
    finally:
        dlg.close()
        dlg.deleteLater()


def _scaled_snapshot():
    snap = example_snapshot()
    return with_generation(
        replace(snap, fonts=replace(snap.fonts, ui_scale=1.25, fixed_scale=1.25))
    )


def test_follow_desktop_without_snapshot_shows_unavailable(qapp, isolated_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        assert dlg.combo_theme.currentText() == "Follow desktop"
        assert dlg.cb_desktop_fonts.isChecked() is True
        assert DESKTOP_SETTINGS_UNAVAILABLE in dlg.lbl_desktop_status.text()
        dlg.combo_theme.setCurrentText("Dark")
        dlg.cb_desktop_fonts.setChecked(False)
        assert DESKTOP_SETTINGS_UNAVAILABLE not in dlg.lbl_desktop_status.text()
        assert dlg.lbl_desktop_status.text() == ""
    finally:
        dlg.deleteLater()


def test_live_update_replaces_unavailable_with_inherited_size(
    qapp, isolated_config, tmp_path, monkeypatch
):
    from qdbrowser.theme import _underlying_style_name

    original_style = _underlying_style_name(qapp)
    original_pal = QPalette(qapp.palette())
    original_qss = qapp.styleSheet()
    dlg = PreferencesDialog(isolated_config)
    try:
        assert DESKTOP_SETTINGS_UNAVAILABLE in dlg.lbl_desktop_status.text()
        snap = _scaled_snapshot()
        write_snapshot(str(tmp_path), snap, require_unwritable_dirs=False)
        monkeypatch.setenv(ENV_OVERRIDE, str(tmp_path / "current.json"))
        attach_presentation(qapp, isolated_config)
        assert DESKTOP_SETTINGS_UNAVAILABLE in dlg.lbl_desktop_status.text()
        dlg.apply_presentation_update()
        text = dlg.lbl_desktop_status.text()
        assert DESKTOP_SETTINGS_UNAVAILABLE not in text
        assert "13.75" in text
        assert snap.fonts.ui_family in text
    finally:
        dlg.deleteLater()
        reset_controller_for_tests()
        qapp.setStyle(original_style)
        qapp.setPalette(original_pal)
        qapp.setStyleSheet(original_qss)
