"""agent_control: unit-test helpers that don't need the live socket."""

import pytest
from PyQt6.QtCore import Qt


def test_key_parse_letter():
    from qdbrowser.plugins.agent_control import _parse_key
    qkey, text, mods = _parse_key("a")
    assert text in ("a",)
    assert mods == Qt.KeyboardModifier.NoModifier


def test_key_parse_enter():
    from qdbrowser.plugins.agent_control import _parse_key
    qkey, text, _ = _parse_key("enter")
    assert qkey == Qt.Key.Key_Return


def test_key_parse_tab():
    from qdbrowser.plugins.agent_control import _parse_key
    qkey, _, _ = _parse_key("tab")
    assert qkey == Qt.Key.Key_Tab


def test_key_parse_modifier_combos():
    from qdbrowser.plugins.agent_control import _parse_key
    _, _, mods = _parse_key("ctrl+l")
    assert mods & Qt.KeyboardModifier.ControlModifier


def test_key_parse_shift_combo():
    from qdbrowser.plugins.agent_control import _parse_key
    _, _, mods = _parse_key("shift+tab")
    assert mods & Qt.KeyboardModifier.ShiftModifier


def test_key_parse_unknown_raises():
    from qdbrowser.plugins.agent_control import _parse_key
    with pytest.raises(ValueError):
        _parse_key("supercaliflagilstic")


def test_key_parse_function_keys():
    from qdbrowser.plugins.agent_control import _parse_key
    for i in range(1, 13):
        qkey, _, _ = _parse_key(f"f{i}")
        assert qkey == getattr(Qt.Key, f"Key_F{i}")


def test_key_parse_arrows():
    from qdbrowser.plugins.agent_control import _parse_key
    for name, expected in [("up", Qt.Key.Key_Up),
                           ("down", Qt.Key.Key_Down),
                           ("left", Qt.Key.Key_Left),
                           ("right", Qt.Key.Key_Right)]:
        qkey, _, _ = _parse_key(name)
        assert qkey == expected


def test_button_translation():
    from qdbrowser.plugins.agent_control import _button_to_qt
    assert _button_to_qt("left") == Qt.MouseButton.LeftButton
    assert _button_to_qt("right") == Qt.MouseButton.RightButton
    assert _button_to_qt("middle") == Qt.MouseButton.MiddleButton
    # Unknown falls back to left.
    assert _button_to_qt("bogus") == Qt.MouseButton.LeftButton


def test_modifiers_translation():
    from qdbrowser.plugins.agent_control import _modifiers_to_qt
    m = _modifiers_to_qt(["ctrl", "shift"])
    assert m & Qt.KeyboardModifier.ControlModifier
    assert m & Qt.KeyboardModifier.ShiftModifier


def test_modifiers_empty():
    from qdbrowser.plugins.agent_control import _modifiers_to_qt
    assert _modifiers_to_qt([]) == Qt.KeyboardModifier.NoModifier


def test_socket_path_uses_runtime_dir(monkeypatch):
    import qdbrowser.plugins.agent_control as ac
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    p = ac._socket_path()
    assert p.startswith("/run/user/1000/")
    assert "qdbrowser-agent" in p


def test_socket_path_falls_back_to_tmp(monkeypatch):
    import qdbrowser.plugins.agent_control as ac
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    p = ac._socket_path()
    assert p.startswith("/tmp/")


def test_is_enabled_env(monkeypatch, fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    monkeypatch.setenv("QDBROWSER_AGENT_CONTROL", "1")
    assert AgentControlPlugin._is_enabled() is True


def test_is_enabled_config(fresh_config, monkeypatch):
    monkeypatch.delenv("QDBROWSER_AGENT_CONTROL", raising=False)
    from qdbrowser.config import Config
    Config().set("plugins", "agent_control", True)
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    assert AgentControlPlugin._is_enabled() is True


def test_is_enabled_default_off(fresh_config, monkeypatch):
    monkeypatch.delenv("QDBROWSER_AGENT_CONTROL", raising=False)
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    assert AgentControlPlugin._is_enabled() is False
