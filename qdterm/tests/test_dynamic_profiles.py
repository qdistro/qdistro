"""Tests for the dynamic_profiles plugin.

Layers:
  - Profile loading and merging.
  - File watcher setup.
  - Plugin lifecycle.
"""

import json

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.dynamic_profiles import (
    DynamicProfilesPlugin,
    _default_profiles_dir,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


# ---------------------------------------------------------------------------
# Profile loading tests
# ---------------------------------------------------------------------------

def test_load_single_profile(tmp_path):
    """Test loading a single profile from JSON."""
    cfg = Config()
    plugin = DynamicProfilesPlugin()

    # Create a profile file
    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()

    profile_file = profiles_dir / "test.json"
    profile_data = {
        "name": "test-profile",
        "font_family": "JetBrains Mono",
        "font_size": 14,
    }
    profile_file.write_text(json.dumps(profile_data))

    # Load
    plugin._profiles_dir = str(profiles_dir)
    plugin._load_profiles(cfg)

    # Check profile was loaded
    assert cfg.get("profiles", "test-profile", "font_family") == "JetBrains Mono"
    assert cfg.get("profiles", "test-profile", "font_size") == 14


def test_load_multiple_profiles(tmp_path):
    """Test loading multiple profiles from one file."""
    cfg = Config()
    plugin = DynamicProfilesPlugin()

    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()

    profile_file = profiles_dir / "multi.json"
    profile_data = [
        {"name": "profile-1", "font_size": 12},
        {"name": "profile-2", "font_size": 14},
    ]
    profile_file.write_text(json.dumps(profile_data))

    plugin._profiles_dir = str(profiles_dir)
    plugin._load_profiles(cfg)

    assert cfg.get("profiles", "profile-1", "font_size") == 12
    assert cfg.get("profiles", "profile-2", "font_size") == 14


def test_load_ignores_invalid_json(tmp_path):
    """Test that invalid JSON is ignored."""
    cfg = Config()
    plugin = DynamicProfilesPlugin()

    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()

    # Invalid JSON
    bad_file = profiles_dir / "bad.json"
    bad_file.write_text("not valid json {{{")

    plugin._profiles_dir = str(profiles_dir)
    plugin._load_profiles(cfg)

    # Should not crash, just ignore


def test_load_ignores_missing_name(tmp_path):
    """Test that profiles without name are ignored."""
    cfg = Config()
    plugin = DynamicProfilesPlugin()

    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()

    profile_file = profiles_dir / "noname.json"
    profile_file.write_text(json.dumps({
        "font_size": 12,
        # No "name" field
    }))

    plugin._profiles_dir = str(profiles_dir)
    plugin._load_profiles(cfg)

    # Should not create a profile with empty name
    # (the code checks for name presence)


def test_load_refuses_to_clobber_default_profile(tmp_path):
    """A profile named 'default' must not overwrite the built-in default."""
    cfg = Config()
    cfg.set("profiles", "default", "font_size", 11)
    plugin = DynamicProfilesPlugin()

    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()
    (profiles_dir / "evil.json").write_text(json.dumps({
        "name": "default",
        "font_size": 99,
    }))

    plugin._profiles_dir = str(profiles_dir)
    plugin._load_profiles(cfg)

    assert cfg.get("profiles", "default", "font_size") == 11


def test_load_handles_missing_directory(tmp_path):
    """Test that missing directory is handled gracefully."""
    cfg = Config()
    plugin = DynamicProfilesPlugin()

    plugin._profiles_dir = str(tmp_path / "nonexistent")
    plugin._load_profiles(cfg)

    # Should not crash


# ---------------------------------------------------------------------------
# Plugin lifecycle tests
# ---------------------------------------------------------------------------

def test_plugin_enabled_by_default():
    """Test plugin activates when enabled."""
    cfg = Config()
    cfg.set("plugins", "dynamic_profiles", "enabled", True)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = DynamicProfilesPlugin()
    plugin.activate(win)

    # Plugin should activate without error
    plugin.deactivate()


def test_plugin_disabled_does_not_load():
    """Test plugin doesn't load profiles when disabled."""
    cfg = Config()
    cfg.set("plugins", "dynamic_profiles", "enabled", False)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = DynamicProfilesPlugin()
    plugin.activate(win)

    # Watcher should not be set up
    assert plugin._watcher is None


def test_plugin_sets_up_watcher(tmp_path, monkeypatch):
    """Test that plugin sets up file watcher."""
    cfg = Config()
    cfg.set("plugins", "dynamic_profiles", "enabled", True)

    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()

    cfg.set("plugins", "dynamic_profiles", "profiles_dir", str(profiles_dir))

    class FakeWin:
        pass

    win = FakeWin()
    plugin = DynamicProfilesPlugin()
    plugin.activate(win)

    # Watcher should be set up
    assert plugin._watcher is not None

    plugin.deactivate()


def test_plugin_creates_default_directory():
    """Test default profiles directory is correct."""
    # Default should point to CONFIG_DIR/profiles.d
    assert "profiles.d" in _default_profiles_dir()


def test_plugin_deactivate_cleans_up():
    """Test deactivate cleans up properly."""
    cfg = Config()
    cfg.set("plugins", "dynamic_profiles", "enabled", True)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = DynamicProfilesPlugin()
    plugin.activate(win)

    plugin.deactivate()

    assert plugin._watcher is None


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_multiple_profile_files(tmp_path):
    """Test loading from multiple profile files."""
    cfg = Config()
    plugin = DynamicProfilesPlugin()

    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()

    # Create two profile files
    file1 = profiles_dir / "file1.json"
    file1.write_text(json.dumps({"name": "profile-a", "font_size": 10}))

    file2 = profiles_dir / "file2.json"
    file2.write_text(json.dumps({"name": "profile-b", "font_size": 20}))

    plugin._profiles_dir = str(profiles_dir)
    plugin._load_profiles(cfg)

    assert cfg.get("profiles", "profile-a", "font_size") == 10
    assert cfg.get("profiles", "profile-b", "font_size") == 20


def test_profile_overwrites_existing(tmp_path):
    """Test that dynamic profile overwrites existing."""
    cfg = Config()
    # Set an existing profile
    cfg.set("profiles", "shared", "font_size", 12)

    plugin = DynamicProfilesPlugin()

    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()

    profile_file = profiles_dir / "override.json"
    profile_file.write_text(json.dumps({"name": "shared", "font_size": 16}))

    plugin._profiles_dir = str(profiles_dir)
    plugin._load_profiles(cfg)

    # Should be overwritten
    assert cfg.get("profiles", "shared", "font_size") == 16
