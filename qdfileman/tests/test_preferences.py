"""Tests for the PreferencesDialog."""

from __future__ import annotations

import pytest
from qfileman import config as config_mod
from qfileman.config import Config
from qfileman.preferences import PreferencesDialog


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
