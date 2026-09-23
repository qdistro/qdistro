"""Configuration system for QTerminator."""

import copy
import json
import os

try:
    import tomllib
except ImportError:
    import tomli as tomllib


CONFIG_DIR = os.path.expanduser("~/.config/qterminator")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")

DEFAULTS = {
    "general": {
        "window_width": 800,
        "window_height": 500,
        "tab_position": "top",  # top, bottom, left, right
        "confirm_close": True,
        "show_menubar": False,
        "theme_mode": "system",  # "dark", "light", or "system" (follows OS)
        "dark_color_scheme": "Linux",
        "light_color_scheme": "BlackOnLightYellow",
        # SECURITY: Saving scrollback may persist secrets (passwords typed
        # in plaintext, API keys, tokens printed in logs). Opt-in only.
        "save_scrollback": False,
    },
    "profiles": {
        "default": {
            "font_family": "Monospace",
            "font_size": 11,
            "font_ligatures": False,
            "color_scheme": "Linux",
            "cursor_shape": "block",  # block, underline, ibeam
            "cursor_blink": True,
            "scrollback_lines": 5000,
            "scrollback_infinite": False,
            "scroll_on_keystroke": True,
            "scroll_on_output": False,
            "background_opacity": 1.0,
            "allow_bold": True,
            "audible_bell": False,
            "visible_bell": False,
            "mouse_autohide": True,
            "word_chars": "-A-Za-z0-9,./?%&#:_",
            "show_titlebar": True,
            "copy_on_selection": False,
            "exit_action": "close",  # close, restart, hold
        },
    },
    "keybindings": {
        # File / window
        "new_tab": "Ctrl+Shift+T",
        "new_window": "Ctrl+Shift+I",
        "close_terminal": "Ctrl+Shift+W",
        "quit": "Ctrl+Shift+Q",
        # Edit
        "copy": "Ctrl+Shift+C",
        "paste": "Ctrl+Shift+V",
        "search": "Ctrl+Shift+F",
        "reset": "Ctrl+Shift+R",
        "reset_clear": "Ctrl+Shift+G",
        "preferences": "Ctrl+,",
        # View / splits
        "split_horizontal": "Ctrl+Shift+O",
        "split_vertical": "Ctrl+Shift+E",
        "rotate_splits": "Meta+R",
        "maximize_terminal": "Ctrl+Shift+Z",
        "full_screen": "F11",
        "zoom_in": "Ctrl+Shift+=",
        "zoom_out": "Ctrl+Shift+-",
        "zoom_normal": "Ctrl+0",
        "toggle_scrollbar": "Ctrl+Shift+S",
        # Terminal titles
        "edit_terminal_title": "Ctrl+Alt+X",
        "edit_tab_title": "Ctrl+Alt+A",
        "edit_window_title": "Ctrl+Alt+W",
        "read_only": "",
        # Tab navigation
        "next_tab": "Ctrl+PgDown",
        "prev_tab": "Ctrl+PgUp",
        "cycle_next": "Ctrl+Tab",
        "cycle_prev": "Ctrl+Shift+Tab",
        "move_tab_left": "Ctrl+Shift+PgUp",
        "move_tab_right": "Ctrl+Shift+PgDown",
        "switch_to_tab_1": "Alt+1",
        "switch_to_tab_2": "Alt+2",
        "switch_to_tab_3": "Alt+3",
        "switch_to_tab_4": "Alt+4",
        "switch_to_tab_5": "Alt+5",
        "switch_to_tab_6": "Alt+6",
        "switch_to_tab_7": "Alt+7",
        "switch_to_tab_8": "Alt+8",
        "switch_to_tab_9": "Alt+9",
        # Split navigation
        "navigate_left": "Alt+Left",
        "navigate_right": "Alt+Right",
        "navigate_up": "Alt+Up",
        "navigate_down": "Alt+Down",
        "resize_right": "Ctrl+Shift+Right",
        "resize_left": "Ctrl+Shift+Left",
        "resize_up": "Ctrl+Shift+Up",
        "resize_down": "Ctrl+Shift+Down",
        # Scrollback
        "scroll_page_up": "Shift+PgUp",
        "scroll_page_down": "Shift+PgDown",
        # Profile cycling
        "next_profile": "Ctrl+Alt+N",
        "prev_profile": "Ctrl+Alt+P",
        # Menu bar
        "toggle_menubar": "Ctrl+Shift+M",
    },
    "layouts": {},
    "plugins": {},
}


class Config:
    """Application configuration backed by TOML file."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self):
        if self._loaded:
            return
        self._data = copy.deepcopy(DEFAULTS)
        self._load()
        self._loaded = True

    def _load(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "rb") as f:
                user_config = tomllib.load(f)
            self._merge(self._data, user_config)

    def _merge(self, base, override):
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge(base[key], value)
            else:
                base[key] = value

    def get(self, *keys, default=None):
        """Get a nested config value. Example: config.get('profiles', 'default', 'font_size')"""
        node = self._data
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def get_profile(self, name="default"):
        """Get a profile dict, falling back to default profile."""
        profiles = self._data.get("profiles", {})
        profile = copy.deepcopy(DEFAULTS["profiles"]["default"])
        if name in profiles:
            profile.update(profiles[name])
        return profile

    def get_keybinding(self, action):
        """Get the key sequence for an action."""
        return self._data.get("keybindings", {}).get(action)

    def set(self, *keys_and_value):
        """Set a nested config value. Last arg is the value.
        Example: config.set('profiles', 'default', 'font_size', 14)
        """
        if len(keys_and_value) < 2:
            return
        keys = keys_and_value[:-1]
        value = keys_and_value[-1]
        node = self._data
        for key in keys[:-1]:
            if key not in node or not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value

    def set_profile(self, name, profile_dict):
        """Set an entire profile."""
        if "profiles" not in self._data:
            self._data["profiles"] = {}
        self._data["profiles"][name] = profile_dict

    def list_profiles(self):
        """Return list of profile names."""
        return list(self._data.get("profiles", {}).keys())

    def del_profile(self, name):
        """Delete a profile (cannot delete 'default')."""
        if name != "default" and name in self._data.get("profiles", {}):
            del self._data["profiles"][name]

    @property
    def general(self):
        return self._data.get("general", {})

    @property
    def keybindings(self):
        return self._data.get("keybindings", {})

    def save(self):
        """Save config to TOML file."""
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            _write_toml(f, self._data)


def _write_toml(f, data, prefix=""):
    """Simple TOML writer for nested dicts."""
    simple = {}
    tables = {}
    for k, v in data.items():
        if isinstance(v, dict):
            tables[k] = v
        else:
            simple[k] = v

    for k, v in simple.items():
        f.write(f"{k} = {_toml_value(v)}\n")

    for k, v in tables.items():
        section = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        f.write(f"\n[{section}]\n")
        _write_toml(f, v, section)


def _toml_string(s):
    """TOML-encode a string. Uses literal 'single-quote' form if safe
    (no single quotes or control chars), otherwise double-quoted with
    escapes."""
    if "'" not in s and '\n' not in s and '\r' not in s and '\t' not in s:
        return f"'{s}'"
    # Escape for double-quoted TOML string
    escaped = (s.replace('\\', '\\\\')
                .replace('"', '\\"')
                .replace('\n', '\\n')
                .replace('\r', '\\r')
                .replace('\t', '\\t'))
    return f'"{escaped}"'


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    elif isinstance(v, int):
        return str(v)
    elif isinstance(v, float):
        return str(v)
    elif isinstance(v, str):
        return _toml_string(v)
    elif isinstance(v, list):
        # If list contains dicts, JSON-encode them as strings
        if v and isinstance(v[0], dict):
            items = ", ".join(_toml_string(json.dumps(i)) for i in v)
        else:
            items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    elif isinstance(v, dict):
        return _toml_string(json.dumps(v))
    return _toml_string(str(v))
