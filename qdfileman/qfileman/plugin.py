"""Plugin system for QFileMan.

Plugins are Python modules placed in:
  - Built-in: qfileman/plugins/
  - User: ~/.config/qfileman/plugins/

Each plugin module should define one or more classes extending:
  - Plugin: base class with activate/deactivate lifecycle
  - MenuProvider: adds items to the file context menu
  - NavigationHook: hooks into navigation (enter directory, etc.)
  - FileFilter: provides custom file filtering logic
"""

import importlib
import importlib.util
import logging
import os
import sys

from qfileman.config import CONFIG_DIR, Config

log = logging.getLogger(__name__)


PLUGIN_DIRS = [
    os.path.join(os.path.dirname(__file__), "plugins"),
    os.path.join(os.path.dirname(__file__), "plugins", "builtin"),
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


class MenuProvider(Plugin):
    """Plugin that adds context menu items.

    Plugins set `category` to group their items under a submenu.
    Standard categories:
      "File"       — file operations (copy, move, delete, rename)
      "View"       — display options (sort, filter, show hidden)
      "Tools"      — utilities (checksum, compare, archive)
      "Plugins"    — default; everything uncategorized
    """

    capabilities = ["menu_provider"]
    category = "Plugins"

    def get_menu_items(self, path):
        """Return ``(label, callback)`` tuples for the context menu.

        ``path`` is the absolute filesystem path of the right-clicked entry
        as a string. Each ``callback`` will be invoked with that same path
        when the corresponding action is triggered.
        """
        return []


class NavigationHook(Plugin):
    """Plugin that hooks into navigation events."""

    capabilities = ["navigation_hook"]

    def on_enter_directory(self, path):
        """Called when entering a directory. Return False to prevent."""
        return True

    def on_leave_directory(self, path):
        """Called when leaving a directory."""
        pass

    def on_double_click(self, file_item):
        """Called on double-click. Return True if handled."""
        return False


class FileFilter(Plugin):
    """Plugin that provides custom file filtering."""

    capabilities = ["file_filter"]

    def filter_files(self, files):
        """Return filtered list of files (path strings)."""
        return files

    def filter_name(self, filename):
        """Return True if file should be shown."""
        return True


class PluginManager:
    """Discovers, loads, and manages plugins."""

    def __init__(self):
        self._available = {}  # name -> module
        self._instances = {}  # name -> Plugin instance
        self._all_instances = []  # every loaded Plugin instance (for capability lookups)
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

        full_name = f"qfileman.plugins.{name}"
        # Reuse existing module if already imported
        existing = sys.modules.get(full_name)
        if existing is not None and getattr(existing, "__file__", None) == path:
            module = existing
        else:
            spec = importlib.util.spec_from_file_location(full_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as e:
                # A broken third-party plugin must not take down the manager.
                log.warning("failed to load plugin %s from %s: %s", name, path, e)
                sys.modules.pop(full_name, None)
                return None

        # Find Plugin subclasses in the module
        instances = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type)
                    and issubclass(attr, Plugin)
                    and attr is not Plugin
                    and attr is not MenuProvider
                    and attr is not NavigationHook
                    and attr is not FileFilter):
                instances.append(attr())

        if instances:
            self._instances[name] = instances[0]
            self._all_instances.extend(instances)
            return instances[0]
        return None

    def enable(self, name, app_controller=None):
        """Enable a plugin."""
        instance = self.load(name)
        if instance:
            instance.activate(app_controller)
            self._enabled.add(name)
            return True
        return False

    def disable(self, name):
        """Disable a plugin."""
        instance = self._instances.get(name)
        if instance:
            instance.deactivate()
            self._enabled.discard(name)

    def get_by_capability(self, capability):
        """Return all loaded plugin instances with the given capability."""
        return [
            p for p in self._all_instances
            if capability in getattr(p, 'capabilities', [])
        ]

    def get_menu_providers(self):
        """Return all loaded MenuProvider instances."""
        return self.get_by_capability("menu_provider")

    def get_navigation_hooks(self):
        """Return all loaded NavigationHook instances."""
        return self.get_by_capability("navigation_hook")

    def get_file_filters(self):
        """Return all loaded FileFilter instances."""
        return self.get_by_capability("file_filter")
