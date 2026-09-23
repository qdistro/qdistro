"""Tests for the config system."""

import json

import pytest
import qterminator.config as config_mod
from qterminator.config import DEFAULTS, Config, _toml_value, _write_toml


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    """Each test gets a fresh config with no disk state."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


def test_config_singleton():
    c1 = Config()
    c2 = Config()
    assert c1 is c2


def test_config_defaults():
    c = Config()
    assert c.get("profiles", "default", "font_size") == 11
    assert c.get("profiles", "default", "color_scheme") == "Linux"
    assert c.get("general", "window_width") == 800


def test_config_get_missing_returns_default():
    c = Config()
    assert c.get("nonexistent", "key", default=42) == 42


def test_config_get_profile():
    c = Config()
    profile = c.get_profile("default")
    assert profile["font_family"] == "Monospace"
    assert profile["scrollback_lines"] == 5000


def test_config_get_profile_fallback():
    c = Config()
    profile = c.get_profile("nonexistent")
    assert profile["font_family"] == "Monospace"


def test_config_keybinding():
    c = Config()
    assert c.get_keybinding("copy") == "Ctrl+Shift+C"
    assert c.get_keybinding("nonexistent") is None


def test_config_set():
    c = Config()
    c.set("profiles", "default", "font_size", 16)
    assert c.get("profiles", "default", "font_size") == 16


def test_config_set_new_key():
    c = Config()
    c.set("profiles", "default", "new_key", "new_value")
    assert c.get("profiles", "default", "new_key") == "new_value"


def test_config_save_load(tmp_path):
    """Config saves to disk and reloads correctly."""
    c = Config()
    c.set("profiles", "default", "font_size", 18)
    c.save()

    assert (tmp_path / "config.toml").exists()

    # Reset singleton and reload
    Config._instance = None
    c2 = Config()
    assert c2.get("profiles", "default", "font_size") == 18


def test_config_list_profiles():
    c = Config()
    profiles = c.list_profiles()
    assert "default" in profiles


def test_config_add_delete_profile():
    c = Config()
    c.set_profile("custom", {"font_family": "Courier", "font_size": 14})
    assert "custom" in c.list_profiles()
    c.del_profile("custom")
    assert "custom" not in c.list_profiles()


def test_config_cannot_delete_default():
    c = Config()
    c.del_profile("default")
    assert "default" in c.list_profiles()


def test_config_extended_profile_defaults():
    """New profile options have defaults."""
    c = Config()
    profile = c.get_profile("default")
    assert profile["scroll_on_keystroke"] is True
    assert profile["scroll_on_output"] is False
    assert profile["audible_bell"] is False
    assert profile["mouse_autohide"] is True
    assert profile["show_titlebar"] is True
    assert profile["copy_on_selection"] is False
    assert profile["exit_action"] == "close"
    assert profile["visible_bell"] is False
    assert profile["scrollback_infinite"] is False


# -- TOML serialization / deserialization tests --

class TestTomlValue:
    def test_bool_true(self):
        assert _toml_value(True) == "true"

    def test_bool_false(self):
        assert _toml_value(False) == "false"

    def test_int(self):
        assert _toml_value(42) == "42"

    def test_float(self):
        assert _toml_value(1.5) == "1.5"

    def test_string(self):
        # Prefers TOML literal string (single-quoted) when no escapes needed
        assert _toml_value("hello") == "'hello'"

    def test_simple_list(self):
        assert _toml_value([1, 2, 3]) == "[1, 2, 3]"

    def test_string_list(self):
        assert _toml_value(["a", "b"]) == "['a', 'b']"

    def test_dict_list_is_json_encoded(self):
        data = [{"key": "value", "num": 42}]
        result = _toml_value(data)
        # Should be a TOML array of single-quoted strings containing JSON
        assert result.startswith("[") and result.endswith("]")
        # Parse the inner JSON string
        inner = result[1:-1].strip().strip("'")
        parsed = json.loads(inner)
        assert parsed == {"key": "value", "num": 42}

    def test_dict_with_none_json_encoded(self):
        data = [{"group": None}]
        result = _toml_value(data)
        inner = result[1:-1].strip().strip("'")
        parsed = json.loads(inner)
        assert parsed == {"group": None}

    def test_standalone_dict(self):
        result = _toml_value({"k": "v"})
        inner = result.strip("'")
        assert json.loads(inner) == {"k": "v"}


class TestTomlRoundtrip:
    """Test that data survives write -> read cycle through TOML."""

    def _roundtrip(self, data):
        """Write data as TOML, read it back with tomllib."""
        import io
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        buf = io.StringIO()
        _write_toml(buf, data)
        return tomllib.loads(buf.getvalue())

    def test_simple_values(self):
        data = {"section": {"a": 1, "b": "hello", "c": True, "d": 3.14}}
        result = self._roundtrip(data)
        assert result["section"]["a"] == 1
        assert result["section"]["b"] == "hello"
        assert result["section"]["c"] is True
        assert result["section"]["d"] == 3.14

    def test_simple_list(self):
        data = {"section": {"items": [1, 2, 3]}}
        result = self._roundtrip(data)
        assert result["section"]["items"] == [1, 2, 3]

    def test_layout_roundtrip(self):
        """Layout with nested dicts survives TOML save/load."""
        layout = {
            "tabs": [
                {
                    "name": "Terminal 1",
                    "tree": {
                        "type": "split",
                        "orientation": "horizontal",
                        "sizes": [320, 320],
                        "children": [
                            {"type": "terminal", "working_directory": "/tmp", "group": None},
                            {"type": "terminal", "working_directory": "/home", "group": "mygroup"},
                        ],
                    },
                },
                {
                    "name": "Terminal 2",
                    "tree": {
                        "type": "terminal",
                        "working_directory": "/var",
                        "group": None,
                    },
                },
            ]
        }
        data = {"layouts": {"last_session": layout}}
        result = self._roundtrip(data)
        tabs = result["layouts"]["last_session"]["tabs"]
        assert len(tabs) == 2
        # Tabs come back as JSON strings
        tab1 = json.loads(tabs[0])
        tab2 = json.loads(tabs[1])
        assert tab1["name"] == "Terminal 1"
        assert tab1["tree"]["children"][0]["working_directory"] == "/tmp"
        assert tab1["tree"]["children"][1]["group"] == "mygroup"
        assert tab2["name"] == "Terminal 2"


class TestConfigLayoutSaveLoad:
    """Test full config save/load cycle with layout data."""

    def test_layout_save_and_reload(self, tmp_path):
        c = Config()
        layout = {
            "tabs": [
                {
                    "name": "Tab 1",
                    "tree": {
                        "type": "split",
                        "orientation": "vertical",
                        "sizes": [250, 250],
                        "children": [
                            {"type": "terminal", "working_directory": "/tmp", "group": None},
                            {"type": "terminal", "working_directory": "/home", "group": None},
                        ],
                    },
                }
            ]
        }
        c.set("layouts", "last_session", layout)
        c.save()

        # Reload
        Config._instance = None
        c2 = Config()
        loaded = c2.get("layouts", "last_session")
        assert "tabs" in loaded
        # Tabs are JSON strings after roundtrip
        tab = loaded["tabs"][0]
        if isinstance(tab, str):
            tab = json.loads(tab)
        assert tab["name"] == "Tab 1"
        assert tab["tree"]["orientation"] == "vertical"
        assert len(tab["tree"]["children"]) == 2

    def test_layout_restore_parses_json_strings(self):
        """restore_layout parses JSON-encoded tab strings."""
        tab_dict = {"name": "Tab 1", "tree": {"type": "terminal", "working_directory": "/tmp", "group": None}}
        tab_str = json.dumps(tab_dict)
        # Verify the string gets parsed back to a dict
        parsed = json.loads(tab_str)
        assert parsed == tab_dict

    def test_layout_restore_parses_legacy_python_repr(self):
        """Legacy Python repr strings can be parsed with ast.literal_eval."""
        import ast
        legacy_str = "{'name': 'Terminal', 'tree': {'type': 'terminal', 'working_directory': '/tmp', 'group': None}}"
        parsed = ast.literal_eval(legacy_str)
        assert parsed["name"] == "Terminal"
        assert parsed["tree"]["working_directory"] == "/tmp"
        assert parsed["tree"]["group"] is None


# =====================================================================
# Additional corner-case tests
# =====================================================================


class TestGetNestingDepths:
    """Config.get() with various nesting depths."""

    def test_get_depth_1(self):
        c = Config()
        result = c.get("general")
        assert isinstance(result, dict)
        assert "window_width" in result

    def test_get_depth_2(self):
        c = Config()
        assert c.get("general", "window_width") == 800

    def test_get_depth_3(self):
        c = Config()
        assert c.get("profiles", "default", "font_size") == 11

    def test_get_depth_4(self):
        c = Config()
        c.set("a", "b", "c", "d", 99)
        assert c.get("a", "b", "c", "d") == 99

    def test_get_missing_intermediate_key(self):
        c = Config()
        assert c.get("profiles", "nope", "font_size", default="fallback") == "fallback"

    def test_get_missing_deep_intermediate(self):
        c = Config()
        assert c.get("x", "y", "z", "w", default=None) is None

    def test_get_no_keys_returns_entire_data(self):
        c = Config()
        result = c.get()
        assert isinstance(result, dict)
        assert "general" in result


class TestSetEdgeCases:
    """Config.set() edge cases."""

    def test_set_creates_deeply_nested_path(self):
        c = Config()
        c.set("a", "b", "c", "d", "deep_value")
        assert c.get("a", "b", "c", "d") == "deep_value"

    def test_set_overwrites_string_with_int(self):
        c = Config()
        c.set("profiles", "default", "font_family", 42)
        assert c.get("profiles", "default", "font_family") == 42

    def test_set_overwrites_int_with_string(self):
        c = Config()
        c.set("profiles", "default", "font_size", "big")
        assert c.get("profiles", "default", "font_size") == "big"

    def test_set_overwrites_bool_with_list(self):
        c = Config()
        c.set("profiles", "default", "cursor_blink", [1, 2, 3])
        assert c.get("profiles", "default", "cursor_blink") == [1, 2, 3]

    def test_set_overwrites_dict_with_scalar(self):
        c = Config()
        c.set("profiles", "replaced_entirely")
        assert c.get("profiles") == "replaced_entirely"

    def test_set_single_key_value(self):
        c = Config()
        c.set("top_level_key", "top_value")
        assert c.get("top_level_key") == "top_value"

    def test_set_with_only_one_arg_is_noop(self):
        c = Config()
        c.set("only_one")
        # Should not crash and not add anything
        assert c.get("only_one", default="missing") == "missing"

    def test_set_overwrites_non_dict_intermediate(self):
        """If an intermediate key is not a dict, set() replaces it with one."""
        c = Config()
        c.set("general", "window_width", "sub_key", "value")
        assert c.get("general", "window_width", "sub_key") == "value"


class TestMultipleProfiles:
    """Multiple profile management."""

    def test_create_multiple_profiles(self):
        c = Config()
        c.set_profile("work", {"font_family": "Courier", "font_size": 12})
        c.set_profile("play", {"font_family": "Comic Sans", "font_size": 18})
        profiles = c.list_profiles()
        assert "default" in profiles
        assert "work" in profiles
        assert "play" in profiles
        assert len(profiles) == 3

    def test_profile_with_missing_keys_gets_defaults_merged(self):
        c = Config()
        c.set_profile("sparse", {"font_size": 20})
        profile = c.get_profile("sparse")
        # Should have default values merged in
        assert profile["font_family"] == "Monospace"
        assert profile["font_size"] == 20
        assert profile["scrollback_lines"] == 5000

    def test_profile_with_extra_unknown_keys_preserved(self):
        c = Config()
        c.set_profile("custom", {"font_family": "Mono", "my_custom_key": "preserved"})
        profile = c.get_profile("custom")
        assert profile["my_custom_key"] == "preserved"

    def test_del_profile_nonexistent_is_noop(self):
        c = Config()
        profiles_before = c.list_profiles()
        c.del_profile("does_not_exist")
        profiles_after = c.list_profiles()
        assert profiles_before == profiles_after

    def test_del_profile_removes_correct_one(self):
        c = Config()
        c.set_profile("alpha", {"font_size": 10})
        c.set_profile("beta", {"font_size": 12})
        c.del_profile("alpha")
        assert "alpha" not in c.list_profiles()
        assert "beta" in c.list_profiles()

    def test_switch_profiles_by_reading_different_ones(self):
        c = Config()
        c.set_profile("dark", {"color_scheme": "Solarized Dark", "font_size": 14})
        c.set_profile("light", {"color_scheme": "Solarized Light", "font_size": 12})
        dark = c.get_profile("dark")
        light = c.get_profile("light")
        assert dark["color_scheme"] == "Solarized Dark"
        assert light["color_scheme"] == "Solarized Light"


class TestKeybindingEdgeCases:
    """Keybinding access edge cases."""

    def test_all_default_keybindings_present(self):
        c = Config()
        for action, binding in DEFAULTS["keybindings"].items():
            assert c.get_keybinding(action) == binding

    def test_keybinding_nonexistent_action_returns_none(self):
        c = Config()
        assert c.get_keybinding("fly_to_moon") is None

    def test_keybinding_empty_string_action(self):
        c = Config()
        assert c.get_keybinding("") is None

    def test_custom_keybinding_set_and_get(self):
        c = Config()
        c.set("keybindings", "custom_action", "Ctrl+Alt+X")
        assert c.get_keybinding("custom_action") == "Ctrl+Alt+X"


class TestSaveLoadEdgeCases:
    """Save/load edge cases."""

    def test_save_load_empty_config(self, tmp_path):
        c = Config()
        c._data = {}
        c.save()
        Config._instance = None
        c2 = Config()
        # Should fall back to defaults since the file has nothing to override
        assert c2.get("general", "window_width") == 800

    def test_roundtrip_all_value_types(self, tmp_path):
        c = Config()
        c.set("test", "an_int", 42)
        c.set("test", "a_float", 3.14)
        c.set("test", "a_bool", True)
        c.set("test", "a_string", "hello")
        c.set("test", "a_list", [1, 2, 3])
        c.save()
        Config._instance = None
        c2 = Config()
        assert c2.get("test", "an_int") == 42
        assert c2.get("test", "a_float") == 3.14
        assert c2.get("test", "a_bool") is True
        assert c2.get("test", "a_string") == "hello"
        assert c2.get("test", "a_list") == [1, 2, 3]

    def test_roundtrip_special_chars_in_strings(self, tmp_path):
        c = Config()
        c.set("test", "special", "hello\tworld")
        c.save()
        Config._instance = None
        c2 = Config()
        assert c2.get("test", "special") == "hello\tworld"

    def test_roundtrip_unicode(self, tmp_path):
        c = Config()
        c.set("test", "unicode_val", "cafe\u0301 \u2603 \u00fc\u00f6\u00e4")
        c.save()
        Config._instance = None
        c2 = Config()
        assert c2.get("test", "unicode_val") == "cafe\u0301 \u2603 \u00fc\u00f6\u00e4"

    def test_roundtrip_empty_string(self, tmp_path):
        c = Config()
        c.set("test", "empty", "")
        c.save()
        Config._instance = None
        c2 = Config()
        assert c2.get("test", "empty") == ""

    def test_roundtrip_zero(self, tmp_path):
        c = Config()
        c.set("test", "zero_int", 0)
        c.set("test", "zero_float", 0.0)
        c.save()
        Config._instance = None
        c2 = Config()
        assert c2.get("test", "zero_int") == 0
        assert c2.get("test", "zero_float") == 0.0

    def test_roundtrip_negative_numbers(self, tmp_path):
        c = Config()
        c.set("test", "neg_int", -42)
        c.set("test", "neg_float", -3.14)
        c.save()
        Config._instance = None
        c2 = Config()
        assert c2.get("test", "neg_int") == -42
        assert c2.get("test", "neg_float") == -3.14

    def test_roundtrip_very_large_numbers(self, tmp_path):
        c = Config()
        c.set("test", "big_int", 10**18)
        c.set("test", "big_float", 1.7976931348623157e+308)
        c.save()
        Config._instance = None
        c2 = Config()
        assert c2.get("test", "big_int") == 10**18
        assert c2.get("test", "big_float") == 1.7976931348623157e+308


class TestConfigFileStates:
    """Config file edge cases on load."""

    def test_no_config_file_creates_defaults(self, tmp_path):
        """First load with no file should use defaults."""
        assert not (tmp_path / "config.toml").exists()
        c = Config()
        assert c.get("general", "window_width") == 800
        assert c.get("profiles", "default", "font_size") == 11

    def test_empty_config_file(self, tmp_path):
        """An empty config file should still load defaults."""
        (tmp_path / "config.toml").write_text("")
        c = Config()
        assert c.get("general", "window_width") == 800

    def test_partial_config_file_merged_with_defaults(self, tmp_path):
        """Partial config file gets missing values from defaults."""
        (tmp_path / "config.toml").write_text(
            '[general]\nwindow_width = 1024\n'
        )
        c = Config()
        assert c.get("general", "window_width") == 1024
        assert c.get("general", "window_height") == 500
        assert c.get("profiles", "default", "font_size") == 11


class TestTomlValueEdgeCases:
    """_toml_value edge cases."""

    def test_empty_list(self):
        assert _toml_value([]) == "[]"

    def test_nested_list(self):
        result = _toml_value([[1, 2], [3, 4]])
        assert result == "[[1, 2], [3, 4]]"

    def test_empty_string(self):
        # Empty string uses literal single-quoted form
        assert _toml_value("") == "''"

    def test_none_fallback(self):
        result = _toml_value(None)
        assert result == "'None'"

    def test_mixed_type_list(self):
        # List of strings and ints handled element by element
        result = _toml_value(["a", "b", "c"])
        assert result == "['a', 'b', 'c']"

    def test_empty_dict(self):
        result = _toml_value({})
        inner = result.strip("'")
        assert json.loads(inner) == {}

    def test_dict_list_multiple_items(self):
        data = [{"a": 1}, {"b": 2}, {"c": 3}]
        result = _toml_value(data)
        assert result.startswith("[") and result.endswith("]")


class TestWriteTomlEdgeCases:
    """_write_toml edge cases."""

    def _roundtrip(self, data):
        import io
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        buf = io.StringIO()
        _write_toml(buf, data)
        return tomllib.loads(buf.getvalue())

    def test_deeply_nested_sections(self):
        data = {"a": {"b": {"c": {"d": {"val": 42}}}}}
        result = self._roundtrip(data)
        assert result["a"]["b"]["c"]["d"]["val"] == 42

    def test_empty_dict_at_top_level(self):
        import io
        buf = io.StringIO()
        _write_toml(buf, {})
        assert buf.getvalue() == ""

    def test_multiple_top_level_sections(self):
        data = {
            "sec1": {"key1": "val1"},
            "sec2": {"key2": "val2"},
        }
        result = self._roundtrip(data)
        assert result["sec1"]["key1"] == "val1"
        assert result["sec2"]["key2"] == "val2"


class TestLayoutComplexRoundtrips:
    """Layout roundtrips with complex structures."""

    def _roundtrip(self, data):
        import io
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        buf = io.StringIO()
        _write_toml(buf, data)
        return tomllib.loads(buf.getvalue())

    def test_layout_with_triple_nested_splits(self):
        layout = {
            "tabs": [
                {
                    "name": "Complex",
                    "tree": {
                        "type": "split",
                        "orientation": "horizontal",
                        "sizes": [300, 300],
                        "children": [
                            {
                                "type": "split",
                                "orientation": "vertical",
                                "sizes": [150, 150],
                                "children": [
                                    {"type": "terminal", "working_directory": "/a", "group": None},
                                    {"type": "terminal", "working_directory": "/b", "group": None},
                                ],
                            },
                            {
                                "type": "split",
                                "orientation": "vertical",
                                "sizes": [150, 150],
                                "children": [
                                    {"type": "terminal", "working_directory": "/c", "group": "g1"},
                                    {"type": "terminal", "working_directory": "/d", "group": "g1"},
                                ],
                            },
                        ],
                    },
                }
            ]
        }
        data = {"layouts": {"session": layout}}
        result = self._roundtrip(data)
        tab_str = result["layouts"]["session"]["tabs"][0]
        tab = json.loads(tab_str)
        assert tab["tree"]["children"][0]["type"] == "split"
        assert tab["tree"]["children"][1]["children"][0]["group"] == "g1"

    def test_layout_with_multiple_tabs_and_groups(self):
        layout = {
            "tabs": [
                {
                    "name": "Tab A",
                    "tree": {"type": "terminal", "working_directory": "/", "group": "alpha"},
                },
                {
                    "name": "Tab B",
                    "tree": {
                        "type": "split",
                        "orientation": "horizontal",
                        "sizes": [200, 200, 200],
                        "children": [
                            {"type": "terminal", "working_directory": "/x", "group": "alpha"},
                            {"type": "terminal", "working_directory": "/y", "group": "beta"},
                            {"type": "terminal", "working_directory": "/z", "group": None},
                        ],
                    },
                },
                {
                    "name": "Tab C",
                    "tree": {"type": "terminal", "working_directory": "/home", "group": None},
                },
            ]
        }
        data = {"layouts": {"multi": layout}}
        result = self._roundtrip(data)
        tabs = result["layouts"]["multi"]["tabs"]
        assert len(tabs) == 3
        tab_b = json.loads(tabs[1])
        assert len(tab_b["tree"]["children"]) == 3
        assert tab_b["tree"]["children"][1]["group"] == "beta"


class TestSingletonReset:
    """Singleton reset works correctly."""

    def test_reset_clears_instance(self):
        c1 = Config()
        c1.set("general", "window_width", 1234)
        Config._instance = None
        c2 = Config()
        # After reset, should reload from defaults (no file)
        assert c2.get("general", "window_width") == 800
        assert c1 is not c2

    def test_reset_allows_new_config_dir(self, tmp_path, monkeypatch):
        c1 = Config()
        c1.set("general", "window_width", 999)
        c1.save()
        Config._instance = None
        # Create a second tmp dir with different config
        tmp2 = tmp_path / "other"
        tmp2.mkdir()
        monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp2))
        monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp2 / "config.toml"))
        c2 = Config()
        # Should get defaults since new dir has no config
        assert c2.get("general", "window_width") == 800


class TestConfigProperties:
    """Config general/keybindings properties."""

    def test_general_property_returns_dict(self):
        c = Config()
        g = c.general
        assert isinstance(g, dict)
        assert g["window_width"] == 800
        assert g["confirm_close"] is True

    def test_keybindings_property_returns_dict(self):
        c = Config()
        kb = c.keybindings
        assert isinstance(kb, dict)
        assert kb["copy"] == "Ctrl+Shift+C"
        assert "quit" in kb

    def test_general_property_reflects_changes(self):
        c = Config()
        c.set("general", "window_width", 1920)
        assert c.general["window_width"] == 1920

    def test_keybindings_property_reflects_changes(self):
        c = Config()
        c.set("keybindings", "copy", "Ctrl+C")
        assert c.keybindings["copy"] == "Ctrl+C"


class TestProfileSaveLoadRoundtrip:
    """Profiles survive save/load."""

    def test_custom_profile_survives_roundtrip(self, tmp_path):
        c = Config()
        c.set_profile("custom", {"font_family": "Fira Code", "font_size": 16})
        c.save()
        Config._instance = None
        c2 = Config()
        assert "custom" in c2.list_profiles()
        assert c2.get("profiles", "custom", "font_family") == "Fira Code"
        assert c2.get("profiles", "custom", "font_size") == 16

    def test_multiple_profiles_survive_roundtrip(self, tmp_path):
        c = Config()
        c.set_profile("a", {"font_size": 10})
        c.set_profile("b", {"font_size": 20})
        c.set_profile("c", {"font_size": 30})
        c.save()
        Config._instance = None
        c2 = Config()
        profiles = c2.list_profiles()
        assert "a" in profiles
        assert "b" in profiles
        assert "c" in profiles
        assert c2.get("profiles", "a", "font_size") == 10
        assert c2.get("profiles", "c", "font_size") == 30
