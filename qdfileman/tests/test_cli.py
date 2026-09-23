"""Tests for CLI module."""

import os
from unittest.mock import MagicMock

import pytest
from qfileman.__main__ import parse_args, setup_window


def test_parse_args_defaults():
    args = parse_args([])
    assert args.path == os.path.expanduser("~")
    assert args.no_plugins is False


def test_parse_args_with_path():
    args = parse_args(["/tmp"])
    assert args.path == "/tmp"
    assert args.no_plugins is False


def test_parse_args_no_plugins():
    args = parse_args(["--no-plugins"])
    assert args.path == os.path.expanduser("~")
    assert args.no_plugins is True


def test_parse_args_version_exits():
    """--version should print and exit cleanly (exit code 0)."""
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--version"])
    assert exc_info.value.code == 0


def test_parse_args_with_path_and_no_plugins():
    args = parse_args(["/home", "--no-plugins"])
    assert args.path == "/home"
    assert args.no_plugins is True


def test_setup_window_loads_plugins_when_enabled(tmp_path, monkeypatch):
    """Without --no-plugins, setup_window calls discover() and wires the manager."""
    # Isolate Config from the user's real config dir.
    from qfileman import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    config_mod.Config._instance = None
    config_mod.Config._data = None

    args = parse_args([str(tmp_path)])
    window = MagicMock()
    pm = MagicMock()

    setup_window(args, window, pm)

    pm.discover.assert_called_once()
    window.set_plugin_manager.assert_called_once_with(pm)
    window._update_path.assert_called_once_with(str(tmp_path))

    config_mod.Config._instance = None
    config_mod.Config._data = None


def test_setup_window_no_plugins_skips_discover(tmp_path, caplog):
    """--no-plugins must skip discover()/enable() and not call set_plugin_manager."""
    args = parse_args(["--no-plugins", str(tmp_path)])
    window = MagicMock()
    pm = MagicMock()

    with caplog.at_level("INFO", logger="qfileman.__main__"):
        setup_window(args, window, pm)

    pm.discover.assert_not_called()
    pm.enable.assert_not_called()
    window.set_plugin_manager.assert_not_called()
    window._update_path.assert_called_once_with(str(tmp_path))
    assert any("plugin loading disabled" in r.message for r in caplog.records)


def test_setup_window_enables_configured_plugins(tmp_path, monkeypatch):
    """Plugins listed in [plugins].enabled should be enabled in order."""
    from qfileman import config as config_mod

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[plugins]\n"
        'enabled = ["alpha", "beta"]\n'
    )
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_file))
    config_mod.Config._instance = None
    config_mod.Config._data = None

    args = parse_args([str(tmp_path)])
    window = MagicMock()
    pm = MagicMock()

    setup_window(args, window, pm)

    enable_calls = [c.args[0] for c in pm.enable.call_args_list]
    assert enable_calls == ["alpha", "beta"]

    config_mod.Config._instance = None
    config_mod.Config._data = None


def test_main_module_imports():
    """The entry point module must import cleanly without invoking main()."""
    from qfileman import __main__  # noqa: F401
    assert callable(__main__.main)
    assert callable(__main__.setup_window)
    assert callable(__main__.parse_args)
