"""QSettings appearance migration: absent vs explicit-false ``dark_mode``."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QSettings
from qnotebook.appearance import (
    APPEARANCE_VERSION,
    load_overrides,
    load_theme_mode,
    migrate_appearance,
    save_overrides,
    save_theme_mode,
)


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield s


def test_absent_dark_mode_migrates_to_system(_isolated_settings):
    s = _isolated_settings
    assert not s.contains("dark_mode")
    assert migrate_appearance(s) == "system"
    assert int(s.value("appearance/version")) == APPEARANCE_VERSION
    assert s.value("appearance/theme_mode") == "system"
    assert not s.contains("dark_mode")


def test_explicit_false_dark_mode_migrates_to_native(_isolated_settings):
    s = _isolated_settings
    s.setValue("dark_mode", False)
    s.sync()
    assert s.contains("dark_mode")
    assert migrate_appearance(s) == "native"
    assert s.value("appearance/theme_mode") == "native"
    assert bool(s.value("dark_mode", True, type=bool)) is False


def test_explicit_true_dark_mode_migrates_to_dark(_isolated_settings):
    s = _isolated_settings
    s.setValue("dark_mode", True)
    s.sync()
    assert migrate_appearance(s) == "dark"
    assert s.value("appearance/theme_mode") == "dark"


def test_existing_version_ignores_legacy_dark_mode(_isolated_settings):
    s = _isolated_settings
    s.setValue("appearance/version", APPEARANCE_VERSION)
    s.setValue("appearance/theme_mode", "system")
    s.setValue("dark_mode", True)
    s.sync()
    assert migrate_appearance(s) == "system"
    assert s.value("appearance/theme_mode") == "system"


def test_system_mode_does_not_rewrite_legacy_boolean(_isolated_settings):
    s = _isolated_settings
    s.setValue("dark_mode", True)
    s.sync()
    save_theme_mode(s, "system", update_legacy=False)
    assert s.value("appearance/theme_mode") == "system"
    assert bool(s.value("dark_mode", False, type=bool)) is True
    save_theme_mode(s, "light", update_legacy=True)
    assert bool(s.value("dark_mode", True, type=bool)) is False
    save_theme_mode(s, "dark", update_legacy=True)
    assert bool(s.value("dark_mode", False, type=bool)) is True
    save_theme_mode(s, "system", update_legacy=False)
    assert bool(s.value("dark_mode", False, type=bool)) is True


def test_load_theme_mode_migrates_once(_isolated_settings):
    s = _isolated_settings
    s.setValue("dark_mode", False)
    s.sync()
    assert load_theme_mode(s) == "native"
    s.setValue("dark_mode", True)
    s.sync()
    assert load_theme_mode(s) == "native"


def test_save_overrides_roundtrip_and_clear(_isolated_settings):
    s = _isolated_settings
    save_overrides(s, {"version": 1, "ui_font_family": "Noto Sans", "ui_font_size_pt": 13.0})
    loaded = load_overrides(s)
    assert loaded["ui_font_family"] == "Noto Sans"
    assert loaded["ui_font_size_pt"] == 13.0
    save_overrides(s, {"version": 1})
    cleared = load_overrides(s)
    assert "ui_font_family" not in cleared
    assert "ui_font_size_pt" not in cleared
