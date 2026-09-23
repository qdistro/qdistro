"""Plugin system for QTerminator.

Plugins are Python modules placed in:
  - Built-in: qterminator/plugins/
  - User: ~/.config/qterminator/plugins/

Each plugin module should define one or more classes extending:
  - Plugin: base class with activate/deactivate lifecycle
  - URLHandler: regex-based URL detection and handling
  - MenuProvider: adds items to the terminal context menu
  - OutputWatcher: reacts to terminal output
"""

import importlib
import importlib.util
import os
import re
import sys

from qterminator.config import CONFIG_DIR, Config

PLUGIN_DIRS = [
    os.path.join(os.path.dirname(__file__), "plugins"),
    os.path.join(CONFIG_DIR, "plugins"),
]


class Plugin:
    """Base class for all plugins."""

    name = "unnamed"
    description = ""
    version = "0.0"
    capabilities = []

    def activate(self, app_controller):
        """Called when the plugin is enabled."""
        pass

    def deactivate(self):
        """Called when the plugin is disabled."""
        pass


class URLHandler(Plugin):
    """Plugin that matches URLs in terminal output."""

    capabilities = ["url_handler"]
    match_pattern = None  # regex string

    def handle_url(self, url):
        """Return the URL to open, or None to skip."""
        return url


def select_url_handler(handlers, url_str):
    """Return the most specific handler whose pattern fullmatches ``url_str``.

    Among handlers that ``re.fullmatch`` the whole URL, the longest
    pattern string wins (more specific beats an over-broad earlier
    registration). Invalid patterns are skipped. Returns None if
    nothing matches.
    """
    best = None
    best_len = -1
    for handler in handlers:
        pat = getattr(handler, "match_pattern", None)
        if not pat:
            continue
        try:
            m = re.fullmatch(pat, url_str)
        except re.error:
            continue
        if not m:
            continue
        n = len(pat)
        if n > best_len:
            best = handler
            best_len = n
    return best


class MenuProvider(Plugin):
    """Plugin that adds context menu items.

    Plugins set `category` to group their items under a submenu.
    Standard categories (any string is allowed):
      "Edit"       — clipboard, paste, copy variants
      "Transform"  — text transformations (base64, hash, json, etc.)
      "Export"     — PDF, HTML, screenshot, print
      "Process"    — process control (signals)
      "Schedule"   — timers, scheduled commands
      "Workspace"  — workspace switching, sessions
      "View"       — display options
      "Plugins"    — default; everything uncategorized
    """

    capabilities = ["menu_provider"]
    category = "Plugins"

    def get_menu_items(self, terminal):
        """Return list of (label, callback) tuples."""
        return []


class OutputWatcher(Plugin):
    """Plugin that reacts to terminal output."""

    capabilities = ["output_watcher"]

    def on_output(self, terminal, text):
        """Called when new output appears. May be batched."""
        pass


class PluginManager:
    """Discovers, loads, and manages plugins."""

    def __init__(self):
        self._available = {}  # name -> module
        self._instances = {}  # name -> Plugin instance
        self._module_instances = {}  # name -> list[Plugin]
        self._enabled = set()
        self._config = Config()

    def discover(self):
        """Scan plugin directories for available plugins."""
        self._available.clear()
        for plugin_dir in PLUGIN_DIRS:
            if not os.path.isdir(plugin_dir):
                continue
            for filename in os.listdir(plugin_dir):
                if filename.endswith(".py") and not filename.startswith("_"):
                    name = filename[:-3]
                    path = os.path.join(plugin_dir, filename)
                    self._available[name] = path

    def available_plugins(self):
        """Return dict of name -> file path for discovered plugins."""
        return dict(self._available)

    def is_builtin(self, name):
        """Return True when ``name`` resolves inside the bundled plugin dir."""
        path = self._available.get(name)
        if not path:
            return False
        builtin_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), "plugins"))
        try:
            return os.path.commonpath([builtin_dir, os.path.realpath(path)]) == builtin_dir
        except ValueError:
            return False

    def is_enabled_by_config(self, name):
        """User plugins require explicit config opt-in before auto-activation."""
        return self._config.get("plugins", name, "enabled", default=None) is True

    def enabled_plugins(self):
        """Return set of enabled plugin names."""
        return set(self._enabled)

    def load(self, name):
        """Load and instantiate a plugin by name."""
        if name in self._instances:
            return self._instances[name]

        path = self._available.get(name)
        if not path:
            return None

        if self.is_builtin(name):
            full_name = f"qterminator.plugins.{name}"
        else:
            full_name = f"qterminator.user_plugins.{name}"
        # If a built-in plugin has already been imported through the
        # regular import system (e.g. by a test file or by another
        # plugin doing ``from qterminator.plugins.x import …``), reuse
        # that module object — building a parallel module from a spec
        # would create a separate class identity for the same name and
        # break isinstance checks across the boundary.
        existing = sys.modules.get(full_name)
        if existing is not None and getattr(existing, "__file__", None) == path:
            module = existing
        else:
            spec = importlib.util.spec_from_file_location(full_name, path)
            module = importlib.util.module_from_spec(spec)
            # Register before exec so dataclasses with stringified
            # annotations (PEP 563 / ``from __future__ import annotations``)
            # can resolve ``cls.__module__`` via sys.modules during class
            # creation — otherwise dataclass field-type inspection raises
            # AttributeError on the very first plugin load.
            sys.modules[full_name] = module
            spec.loader.exec_module(module)

        # Find Plugin subclasses in the module
        instances = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type)
                    and issubclass(attr, Plugin)
                    and attr is not Plugin
                    and attr is not URLHandler
                    and attr is not MenuProvider
                    and attr is not OutputWatcher):
                instances.append(attr())

        if instances:
            # Store first plugin instance as the main one
            self._instances[name] = instances[0]
            self._module_instances[name] = instances
            # Store all instances for capability lookup
            if not hasattr(self, '_all_instances'):
                self._all_instances = []
            self._all_instances.extend(instances)
            return instances[0]
        return None

    def enable(self, name, app_controller=None):
        """Enable a plugin."""
        instance = self.load(name)
        if instance:
            for inst in self._module_instances.get(name, [instance]):
                inst.activate(app_controller)
            self._enabled.add(name)
            return True
        return False

    def disable(self, name):
        """Disable a plugin."""
        instance = self._instances.get(name)
        if instance:
            for inst in reversed(self._module_instances.get(name, [instance])):
                inst.deactivate()
            self._enabled.discard(name)

    def get_by_capability(self, capability):
        """Return all loaded plugin instances with the given capability."""
        all_instances = getattr(self, '_all_instances', [])
        return [p for p in all_instances
                if capability in getattr(p, 'capabilities', [])]

    def get_url_handlers(self):
        """Return all loaded URLHandler instances."""
        return self.get_by_capability("url_handler")

    def get_menu_providers(self):
        """Return all loaded MenuProvider instances."""
        return self.get_by_capability("menu_provider")

    def get_output_watchers(self):
        """Return all loaded OutputWatcher instances."""
        return self.get_by_capability("output_watcher")
