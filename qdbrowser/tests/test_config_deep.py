"""Deeper coverage of the Config singleton."""

import os

import pytest


def test_singleton_returns_same_instance(fresh_config):
    a = fresh_config.Config()
    b = fresh_config.Config()
    assert a is b


def test_singleton_load_runs_once(fresh_config):
    cfg = fresh_config.Config()
    # Set a value, then construct again — should NOT reload from disk.
    cfg.set("general", "homepage", "https://changed.test")
    cfg2 = fresh_config.Config()
    assert cfg2.get("general", "homepage") == "https://changed.test"


def test_get_with_default(fresh_config):
    cfg = fresh_config.Config()
    assert cfg.get("nonexistent", default="fallback") == "fallback"
    assert cfg.get("general", "no_such_key", default=42) == 42


def test_get_nested_missing_returns_default(fresh_config):
    cfg = fresh_config.Config()
    assert cfg.get("a", "b", "c", default="x") == "x"


def test_set_creates_nested_path(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("a", "b", "c", 7)
    assert cfg.get("a", "b", "c") == 7


def test_set_overwrites_non_dict(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("a", "leaf", 1)
    cfg.set("a", "leaf", "deeper", 2)
    assert cfg.get("a", "leaf", "deeper") == 2


def test_set_with_one_arg_is_noop(fresh_config):
    cfg = fresh_config.Config()
    before = dict(cfg.general)
    cfg.set("x")
    assert dict(cfg.general) == before


def test_keybindings_property(fresh_config):
    cfg = fresh_config.Config()
    kb = cfg.keybindings
    assert "new_tab" in kb
    assert "find" in kb


def test_general_property(fresh_config):
    cfg = fresh_config.Config()
    g = cfg.general
    assert "homepage" in g
    assert "window_width" in g


def test_toml_value_roundtrip_bool(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "confirm_close", False)
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    assert cfg2.get("general", "confirm_close") is False


def test_toml_value_roundtrip_int(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "side_panel_width", 333)
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    assert cfg2.get("general", "side_panel_width") == 333


def test_toml_value_roundtrip_float(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("profiles", "default", "default_zoom", 1.25)
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    assert cfg2.get("profiles", "default", "default_zoom") == pytest.approx(1.25)


def test_toml_string_with_special_chars(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", 'Mozilla "weird" \\ Agent')
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    assert cfg2.get("general", "user_agent") == 'Mozilla "weird" \\ Agent'


def test_toml_string_with_newline(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "homepage", "line1\nline2")
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    assert cfg2.get("general", "homepage") == "line1\nline2"


def test_toml_list_of_strings(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("blocklist", "extra_blocked", ["a.com", "b.org"])
    cfg.save()
    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    assert set(cfg2.get("blocklist", "extra_blocked")) == {"a.com", "b.org"}


def test_toml_writer_helpers():
    from qdbrowser.config import _toml_value
    assert _toml_value(True) == "true"
    assert _toml_value(False) == "false"
    assert _toml_value(42) == "42"
    assert _toml_value(3.14) == "3.14"
    assert _toml_value("simple") == "'simple'"
    assert _toml_value([]) == "[]"
    assert _toml_value([1, 2]) == "[1, 2]"


def test_get_profile_with_explicit_override(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("profiles", "private", {"javascript_enabled": False})
    p = cfg.get_profile("private")
    assert p["javascript_enabled"] is False
    # Defaults preserved for unchanged fields.
    assert p["default_zoom"] == 1.0


def test_get_keybinding_returns_none_for_missing(fresh_config):
    cfg = fresh_config.Config()
    assert cfg.get_keybinding("does_not_exist") is None


def test_config_save_creates_dir(fresh_config, tmp_path):
    cfg = fresh_config.Config()
    cfg.save()
    assert os.path.exists(fresh_config.CONFIG_FILE)
    assert os.path.isdir(fresh_config.CONFIG_DIR)


def test_merge_deep_dicts(fresh_config, tmp_path):
    # Write a partial config file, ensure merge preserves defaults.
    os.makedirs(fresh_config.CONFIG_DIR, exist_ok=True)
    with open(fresh_config.CONFIG_FILE, "w") as f:
        f.write("[general]\nhomepage = 'https://merged.test'\n")
    fresh_config.Config._instance = None
    cfg = fresh_config.Config()
    assert cfg.get("general", "homepage") == "https://merged.test"
    # Defaults still present.
    assert cfg.get("general", "window_width") == 1280
    assert cfg.get_keybinding("new_tab") == "Ctrl+T"
