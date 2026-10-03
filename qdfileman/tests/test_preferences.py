"""Tests for the PreferencesDialog."""

from __future__ import annotations

from dataclasses import replace

import pytest
from qdistro_presentation.model import (
    DESKTOP_SETTINGS_UNAVAILABLE,
    example_snapshot,
    with_generation,
)
from qdistro_presentation.paths import ENV_OVERRIDE
from qdistro_presentation.publish import write_snapshot
from qfileman import config as config_mod
from qfileman.config import Config
from qfileman.preferences import PreferencesDialog
from qfileman.theme import attach_presentation, reset_controller_for_tests


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Config singleton backed by an isolated TOML file."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    Config._data = None
    yield Config()
    Config._instance = None
    Config._data = None


def test_dialog_loads_defaults(qapp, isolated_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        assert dlg.cb_show_hidden.isChecked() is False
        assert dlg.cb_confirm_delete.isChecked() is True
        assert dlg.cb_single_click.isChecked() is False
        assert dlg.combo_view.currentText() == "List"
        assert dlg.combo_sort.currentText() == "Name"
        assert dlg.combo_order.currentText() == "Ascending"
        assert dlg.combo_theme.currentText() == "Follow desktop"
        assert dlg.spin_icon_size.value() == 32
    finally:
        dlg.deleteLater()


def test_dialog_loads_existing_values(qapp, isolated_config):
    isolated_config.set("general", "show_hidden", True)
    isolated_config.set("general", "default_view", "grid")
    isolated_config.set("general", "sort_by", "size")
    isolated_config.set("general", "sort_order", "desc")
    isolated_config.set("general", "theme_mode", "dark")
    isolated_config.set("general", "icon_size", 64)

    dlg = PreferencesDialog(isolated_config)
    try:
        assert dlg.cb_show_hidden.isChecked() is True
        assert dlg.combo_view.currentText() == "Grid"
        assert dlg.combo_sort.currentText() == "Size"
        assert dlg.combo_order.currentText() == "Descending"
        assert dlg.combo_theme.currentText() == "Dark"
        assert dlg.spin_icon_size.value() == 64
    finally:
        dlg.deleteLater()


def test_apply_persists_to_disk(qapp, isolated_config, tmp_path):
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.cb_show_hidden.setChecked(True)
        dlg.combo_view.setCurrentText("Grid")
        dlg.combo_sort.setCurrentText("Date")
        dlg.combo_order.setCurrentText("Descending")
        dlg.combo_theme.setCurrentText("Dark")
        dlg.spin_icon_size.setValue(48)
        dlg._apply()
    finally:
        dlg.deleteLater()

    # Bypass the cached singleton to prove the values hit disk.
    Config._instance = None
    Config._data = None
    fresh = Config()
    assert fresh.get("general", "show_hidden") is True
    assert fresh.get("general", "default_view") == "grid"
    assert fresh.get("general", "sort_by") == "date"
    assert fresh.get("general", "sort_order") == "desc"
    assert fresh.get("general", "theme_mode") == "dark"
    assert fresh.get("general", "icon_size") == 48


def test_follow_desktop_and_desktop_fonts_persist(qapp, isolated_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.combo_theme.setCurrentText("Follow desktop")
        dlg.cb_desktop_fonts.setChecked(True)
        dlg._apply()
    finally:
        dlg.deleteLater()
    Config._instance = None
    Config._data = None
    fresh = Config()
    assert fresh.get("general", "theme_mode") == "system"
    appearance = fresh.get("appearance", default={})
    assert appearance.get("version") == 1
    assert "ui_font_family" not in appearance


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
        dlg.reject()
    finally:
        dlg.deleteLater()
    appearance = isolated_config.get("appearance", default={})
    assert appearance.get("ui_font_family") is None


def test_accept_applies_before_closing(qapp, isolated_config):
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.cb_confirm_delete.setChecked(False)
        dlg.accept()  # accept() also calls _apply()
    finally:
        dlg.deleteLater()

    assert isolated_config.get("general", "confirm_delete") is False


def test_reject_does_not_mutate_config(qapp, isolated_config):
    """Clicking Cancel must not write to the config."""
    dlg = PreferencesDialog(isolated_config)
    try:
        dlg.cb_show_hidden.setChecked(True)
        dlg.reject()
    finally:
        dlg.deleteLater()

    # show_hidden should still be at its default.
    assert isolated_config.get("general", "show_hidden") is False


def _scaled_snapshot():
    snap = example_snapshot()
    return with_generation(
        replace(snap, fonts=replace(snap.fonts, ui_scale=1.25, fixed_scale=1.25))
    )


def _windows_style_name():
    from PyQt6.QtWidgets import QStyleFactory

    for name in QStyleFactory.keys():
        if name.lower() == "windows":
            return name
    pytest.skip("Windows style required to distinguish Fusion")


def _prime_native_baseline(app, *, with_qss):
    from PyQt6.QtGui import QColor, QPalette
    from qfileman.theme import _underlying_style_name

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
    from PyQt6.QtGui import QPalette
    from qfileman.theme import _underlying_style_name, current_controller

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
            assert _underlying_style_name(qapp).lower() == "fusion"
            dlg.combo_theme.setCurrentText("Native")
            dlg._apply()
        finally:
            dlg.deleteLater()
        assert current_controller() is None
        assert _underlying_style_name(qapp).lower() == "windows"
        assert qapp.palette().color(QPalette.ColorRole.Window).getRgb() == window_rgb
        assert qapp.styleSheet() == qss
        assert isolated_config.get("general", "theme_mode") == "native"
    finally:
        reset_controller_for_tests()
        qapp.setStyle(original_style)
        qapp.setPalette(original_pal)
        qapp.setStyleSheet(original_qss)


def test_follow_desktop_without_snapshot_shows_unavailable(qapp, isolated_config):
    reset_controller_for_tests()
    dlg = PreferencesDialog(isolated_config)
    try:
        assert dlg.combo_theme.currentText() == "Follow desktop"
        assert dlg.cb_desktop_fonts.isChecked() is True
        assert DESKTOP_SETTINGS_UNAVAILABLE in dlg.lbl_desktop_status.text()
        dlg.combo_theme.setCurrentText("Dark")
        dlg.cb_desktop_fonts.setChecked(False)
        assert dlg.lbl_desktop_status.text() == ""
    finally:
        dlg.deleteLater()
        reset_controller_for_tests()


def test_live_update_replaces_unavailable_with_inherited_size(
    qapp, isolated_config, tmp_path, monkeypatch
):
    from PyQt6.QtGui import QPalette
    from qfileman.theme import _underlying_style_name

    original_style = _underlying_style_name(qapp)
    original_pal = QPalette(qapp.palette())
    original_qss = qapp.styleSheet()
    reset_controller_for_tests()
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
