"""Appearance preferences dialog persistence and live apply."""

from __future__ import annotations

import pytest
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QWidget
from qdbrowser.config import Config
from qdbrowser.preferences import PreferencesDialog, THEME_KEY_TO_LABEL
from qdbrowser.theme import (
    attach_presentation,
    current_controller,
    reset_controller_for_tests,
)
from qdistro_presentation.model import example_snapshot
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
