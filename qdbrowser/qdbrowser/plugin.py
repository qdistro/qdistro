"""Plugin system for qdbrowser.

Plugins live in:
  - Built-in: qdbrowser/plugins/
  - User:     ~/.config/qdbrowser/plugins/

A plugin module defines one or more classes extending one of:
  - Plugin            — base lifecycle (activate / deactivate)
  - SidePanelProvider — contributes a panel into the side dock
  - UrlInterceptor    — sees every QWebEngineUrlRequest, can block/redirect
  - CommandProvider   — contributes entries to the command palette (Ctrl+E)
  - PageObserver      — receives page-load / navigation hooks

Same filesystem-discovery model as qterminator/plugin.py.
"""

import importlib
import importlib.util
import os
import sys

# Eagerly import the built-in plugins package so that
# ``import qdbrowser.plugins.<name>`` works even after we register a
# module via spec_from_file_location — otherwise the parent package
# attribute is never set on ``qdbrowser`` and `import a.b.c as x` fails.
import qdbrowser.plugins  # noqa: F401
from qdbrowser.config import CONFIG_DIR, Config

PLUGIN_DIRS = [
    os.path.join(os.path.dirname(__file__), "plugins"),
    os.path.join(CONFIG_DIR, "plugins"),
]


class Plugin:
    name = "unnamed"
    description = ""
    version = "0.0"
    capabilities: list = []

    def activate(self, app_controller):
        pass

    def deactivate(self):
        pass


class SidePanelProvider(Plugin):
    """Plugin contributes a side panel.

    Implement ``build_panel(window) -> QWidget`` and set ``panel_id``,
    ``panel_label``, ``panel_icon``.
    """

    capabilities = ["side_panel"]
    panel_id = "unnamed"
    panel_label = "Panel"
    panel_icon = ""

    def build_panel(self, window):
        raise NotImplementedError


class UrlInterceptor(Plugin):
    """Plugin sees every URL request before it goes out.

    Implement ``intercept(info)`` where ``info`` is a
    QWebEngineUrlRequestInfo. Call ``info.block(True)`` to drop or
    ``info.redirect(QUrl)`` to redirect.
    """

    capabilities = ["url_interceptor"]

    def intercept(self, info):
        pass


class CommandProvider(Plugin):
    """Plugin contributes items to the command palette (Ctrl+E)."""

    capabilities = ["command_provider"]

    def get_commands(self, window):
        """Return list of (label, callback) tuples or list of dicts:
        {label, hint, callback, category}.
        """
        return []


class PageObserver(Plugin):
    """Plugin receives page lifecycle events.

    ``persistent`` declares that this observer records or otherwise
    persists page activity to durable storage (a history log, on-disk
    index, etc.). Persistent observers are NOT wired to off-the-record
    (private) webviews, so private browsing leaves no trace. Ephemeral
    observers (dark-mode injection, clipboard tagging, content blocking)
    leave this False so they keep running in private mode.
    """

    capabilities = ["page_observer"]
    persistent = False

    def on_navigation(self, webview, url):
        pass

    def on_load_finished(self, webview, ok):
        pass

    def on_title_changed(self, webview, title):
        pass


class PluginManager:
    def __init__(self):
        self._available: dict = {}    # name -> path
        self._instances: dict = {}    # name -> first instance (back-compat)
        self._all_instances: list = []
        self._enabled: set = set()
        self._config = Config()

    def discover(self):
        self._available.clear()
        for plugin_dir in PLUGIN_DIRS:
            if not os.path.isdir(plugin_dir):
                continue
            for filename in os.listdir(plugin_dir):
                if filename.endswith(".py") and not filename.startswith("_"):
                    name = filename[:-3]
                    self._available[name] = os.path.join(plugin_dir, filename)

    def available_plugins(self):
        return dict(self._available)

    def enabled_plugins(self):
        return set(self._enabled)

    def load(self, name):
        if name in self._instances:
            return self._instances[name]
        path = self._available.get(name)
        if not path:
            return None

        full_name = f"qdbrowser.plugins.{name}"
        existing = sys.modules.get(full_name)
        if existing is not None and getattr(existing, "__file__", None) == path:
            module = existing
        else:
            spec = importlib.util.spec_from_file_location(full_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                # Drop the half-initialised module so sibling plugins
                # that import names from it don't get a stale shell.
                sys.modules.pop(full_name, None)
                raise
        # Ensure the parent package has a child attribute pointing at
        # the loaded submodule. Python's normal import machinery does
        # this automatically when you write ``import a.b``; the
        # spec_from_file_location path skips the parent-attribute
        # write, which breaks anything that walks the package via
        # ``getattr`` — e.g. pytest's ``monkeypatch.setattr("a.b.c")``
        # which calls ``getattr(a, 'b')`` rather than re-importing.
        setattr(qdbrowser.plugins, name, module)

        instances = []
        base_classes = (Plugin, SidePanelProvider, UrlInterceptor,
                        CommandProvider, PageObserver)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type)
                    and issubclass(attr, Plugin)
                    and attr not in base_classes):
                instances.append(attr())

        if instances:
            self._instances[name] = instances[0]
            self._all_instances.extend(instances)
            return instances[0]
        return None

    def enable(self, name, app_controller=None):
        instance = self.load(name)
        if instance:
            instance.activate(app_controller)
            self._enabled.add(name)
            self._app_controller = app_controller
            return True
        return False

    def disable(self, name):
        instance = self._instances.get(name)
        if instance:
            # Disconnect any window-level signals we wired for this
            # plugin (page observers etc.) before deactivate() so the
            # plugin can't see events firing during teardown.
            ac = getattr(self, "_app_controller", None)
            if ac is not None and hasattr(ac, "disconnect_plugin"):
                try:
                    ac.disconnect_plugin(instance)
                except Exception:
                    pass
            instance.deactivate()
            self._enabled.discard(name)

    def get_by_capability(self, capability):
        return [p for p in self._all_instances
                if capability in getattr(p, "capabilities", [])]

    def get_side_panel_providers(self):
        return self.get_by_capability("side_panel")

    def get_url_interceptors(self):
        return self.get_by_capability("url_interceptor")

    def get_command_providers(self):
        return self.get_by_capability("command_provider")

    def get_page_observers(self):
        return self.get_by_capability("page_observer")
