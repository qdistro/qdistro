"""Configuration management for QFileMan."""

import logging
import os
import tomllib

log = logging.getLogger(__name__)


# Default config directory
CONFIG_DIR = os.path.expanduser("~/.config/qfileman")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")


class Config:
    """Simple TOML-based configuration singleton."""

    _instance = None
    _data = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        """Load config from file or use defaults."""
        self._data = self._default_config()

        if os.path.isfile(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "rb") as f:
                    user_data = tomllib.load(f)
                self._merge(self._data, user_data)
            except (OSError, tomllib.TOMLDecodeError) as e:
                log.warning("failed to load config %s: %s", CONFIG_FILE, e)

    def _default_config(self):
        """Return default configuration."""
        return {
            "general": {
                "show_hidden": False,
                "confirm_delete": True,
                "single_click": False,
                "default_view": "list",  # "list" or "grid"
                "sort_by": "name",  # "name", "size", "date", "type"
                "sort_order": "asc",  # "asc" or "desc"
                "theme_mode": "system",  # "system", "light", "dark"
                "icon_size": 32,
            },
            "window": {
                "width": 900,
                "height": 600,
                "remember_size": True,
                "remember_position": True,
            },
            "plugins": {
                "enabled": [],
            },
            "bookmarks": [],
        }

    def _merge(self, base, overlay):
        """Recursively merge overlay into base."""
        for key, value in overlay.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge(base[key], value)
            else:
                base[key] = value

    def get(self, *keys, default=None):
        """Get a config value by key path (e.g., config.get('general', 'show_hidden'))."""
        data = self._data
        for key in keys:
            if isinstance(data, dict) and key in data:
                data = data[key]
            else:
                return default
        return data

    def set(self, *keys):
        """Set a config value by key path. Last argument is the value."""
        if len(keys) < 2:
            return
        value = keys[-1]
        keys = keys[:-1]  # Remove the value from keys
        data = self._data
        for k in keys[:-1]:  # Skip the last key in navigation
            if k not in data:
                data[k] = {}
            data = data[k]
        data[keys[-1]] = value

    def save(self):
        """Save current config to file.

        ``tomli_w`` is a required runtime dependency; if it is genuinely
        missing (e.g. someone vendored the source without installing
        deps), surface a warning instead of crashing the app.
        """
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            import tomli_w
        except ImportError:
            log.warning(
                "tomli_w is missing despite being declared in dependencies; "
                "config will not be saved"
            )
            return
        with open(CONFIG_FILE, "wb") as f:
            tomli_w.dump(self._data, f)

    def add_bookmark(self, path, name=None):
        """Add a bookmark."""
        bookmarks = self.get("bookmarks", default=[])
        if name is None:
            name = os.path.basename(path)
        bookmarks.append({"path": path, "name": name})
        self.set("bookmarks", bookmarks)

    def remove_bookmark(self, path):
        """Remove a bookmark by path."""
        bookmarks = self.get("bookmarks", default=[])
        bookmarks = [b for b in bookmarks if b.get("path") != path]
        self.set("bookmarks", bookmarks)

    def get_bookmarks(self):
        """Get list of bookmarks."""
        return self.get("bookmarks", default=[])

    @classmethod
    def reset_instance(cls):
        """Reset singleton (for testing)."""
        cls._instance = None
        cls._data = None
