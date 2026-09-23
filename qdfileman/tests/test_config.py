"""Tests for config module."""


import pytest
from qfileman import config as config_mod
from qfileman.config import Config


@pytest.fixture
def fresh_config(tmp_path, monkeypatch):
    """Each test gets a fresh config with no disk state."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    Config._data = None
    yield
    Config._instance = None
    Config._data = None


def test_config_default_values(fresh_config):
    """Test default configuration values."""
    config = Config()
    assert config.get("general", "show_hidden") is False
    assert config.get("general", "confirm_delete") is True
    assert config.get("general", "default_view") == "list"


def test_config_get_nested(fresh_config):
    """Test nested key retrieval."""
    config = Config()
    # Test with defaults
    assert config.get("general", "nonexistent", default="default") == "default"


def test_config_set_value(fresh_config):
    """Test setting a config value."""
    config = Config()
    config.set("general", "show_hidden", True)
    assert config.get("general", "show_hidden") is True


def test_config_set_nested(fresh_config):
    """Test setting nested values."""
    config = Config()
    config.set("general", "custom_key", "custom_value")
    assert config.get("general", "custom_key") == "custom_value"


def test_config_bookmarks(fresh_config):
    """Test bookmark management."""
    config = Config()

    # Initially empty
    assert config.get_bookmarks() == []

    # Add bookmark
    config.add_bookmark("/home/user/Documents", "Docs")
    bookmarks = config.get_bookmarks()
    assert len(bookmarks) == 1
    assert bookmarks[0]["path"] == "/home/user/Documents"
    assert bookmarks[0]["name"] == "Docs"

    # Add another
    config.add_bookmark("/home/user/Downloads")
    bookmarks = config.get_bookmarks()
    assert len(bookmarks) == 2
    assert bookmarks[1]["name"] == "Downloads"  # Auto-named

    # Remove bookmark
    config.remove_bookmark("/home/user/Documents")
    bookmarks = config.get_bookmarks()
    assert len(bookmarks) == 1


def test_config_window_settings(fresh_config):
    """Test window settings."""
    config = Config()
    assert config.get("window", "width") == 900
    assert config.get("window", "height") == 600


def test_config_sort_settings(fresh_config):
    """Test sort settings."""
    config = Config()
    assert config.get("general", "sort_by") == "name"
    assert config.get("general", "sort_order") == "asc"


def test_config_plugins_section(fresh_config):
    """Test plugins section."""
    config = Config()
    assert config.get("plugins", "enabled", default=[]) == []


def test_config_loads_user_overrides(tmp_path, monkeypatch):
    """User-supplied TOML overrides defaults but leaves unspecified keys intact."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[general]\n'
        'show_hidden = true\n'
        'default_view = "grid"\n'
    )
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_file))
    Config._instance = None
    Config._data = None

    config = Config()
    assert config.get("general", "show_hidden") is True
    assert config.get("general", "default_view") == "grid"
    # Unspecified key should still have its default
    assert config.get("general", "confirm_delete") is True

    Config._instance = None
    Config._data = None


def test_config_malformed_toml_falls_back_to_defaults(tmp_path, monkeypatch, caplog):
    """Garbage in config.toml should log a warning and fall back to defaults."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("this is not = valid = toml [[[")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_file))
    Config._instance = None
    Config._data = None

    with caplog.at_level("WARNING", logger="qfileman.config"):
        config = Config()

    # Defaults should still be in place
    assert config.get("general", "show_hidden") is False
    # Warning should have been logged
    assert any("failed to load config" in r.message for r in caplog.records), \
        f"Expected warning about failed config load, got {caplog.records}"

    Config._instance = None
    Config._data = None


def test_config_save_roundtrip(tmp_path, monkeypatch):
    """A saved config should reload to the same values."""
    pytest.importorskip("tomli_w")
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_file))
    Config._instance = None
    Config._data = None

    config = Config()
    config.set("general", "show_hidden", True)
    config.set("general", "default_view", "grid")
    config.add_bookmark("/tmp/marker", "Marker")
    config.save()
    assert config_file.is_file(), "save() should create config file"

    # Reset and reload
    Config._instance = None
    Config._data = None
    reloaded = Config()
    assert reloaded.get("general", "show_hidden") is True
    assert reloaded.get("general", "default_view") == "grid"
    bookmarks = reloaded.get_bookmarks()
    assert any(b["path"] == "/tmp/marker" and b["name"] == "Marker" for b in bookmarks)

    Config._instance = None
    Config._data = None


def test_config_save_without_tomli_w_logs_warning(tmp_path, monkeypatch, caplog):
    """If tomli_w is unavailable, save() should log a warning and not crash."""
    import builtins
    import sys

    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    Config._data = None
    config = Config()

    # Drop any pre-imported tomli_w and force ImportError on next import.
    monkeypatch.delitem(sys.modules, "tomli_w", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tomli_w":
            raise ImportError("simulated missing tomli_w")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with caplog.at_level("WARNING", logger="qfileman.config"):
        config.save()

    assert not (tmp_path / "config.toml").is_file(), \
        "save() should not write a file when tomli_w is missing"
    assert any("tomli_w is missing" in r.message for r in caplog.records)

    Config._instance = None
    Config._data = None


def test_config_singleton(fresh_config):
    """Two Config() calls return the same instance and share state."""
    a = Config()
    b = Config()
    assert a is b
    a.set("general", "show_hidden", True)
    assert b.get("general", "show_hidden") is True


def test_config_reset_instance(fresh_config):
    """reset_instance() classmethod clears the singleton."""
    a = Config()
    a.set("general", "show_hidden", True)
    Config.reset_instance()
    b = Config()
    assert a is not b
    # New instance should have defaults, not mutations from the prior one
    assert b.get("general", "show_hidden") is False


def test_config_remove_missing_bookmark_is_noop(fresh_config):
    """Removing a path that isn't bookmarked should not raise."""
    config = Config()
    config.add_bookmark("/a", "A")
    config.remove_bookmark("/never-bookmarked")
    assert len(config.get_bookmarks()) == 1
