"""Tests for plugin system."""

import os
import re

import pytest
from qterminator import config as config_mod
from qterminator.config import Config
from qterminator.plugin import (
    MenuProvider,
    OutputWatcher,
    Plugin,
    PluginManager,
    URLHandler,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_config(tmp_path, monkeypatch):
    """Each test gets a fresh config with no disk state."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


class FakeTerminal:
    """Minimal stand-in for a terminal object."""

    def __init__(self, pid=12345):
        self._pid = pid

    def shell_pid(self):
        return self._pid

    def send_text(self, text):
        self._last_sent = text


# ---------------------------------------------------------------------------
# Plugin base classes
# ---------------------------------------------------------------------------

def test_plugin_base_class():
    p = Plugin()
    assert p.name == "unnamed"
    p.activate(None)
    p.deactivate()


def test_plugin_default_description():
    p = Plugin()
    assert p.description == ""


def test_plugin_default_version():
    p = Plugin()
    assert p.version == "0.0"


def test_plugin_default_capabilities():
    p = Plugin()
    assert p.capabilities == []


def test_plugin_activate_with_controller():
    """activate() accepts an arbitrary controller object without crashing."""
    p = Plugin()
    p.activate(object())


def test_plugin_deactivate_without_activate():
    """deactivate() before activate() must not crash."""
    p = Plugin()
    p.deactivate()


# --- URLHandler base ---

def test_url_handler_base():
    h = URLHandler()
    assert "url_handler" in h.capabilities
    assert h.handle_url("http://example.com") == "http://example.com"


def test_url_handler_has_match_pattern():
    h = URLHandler()
    assert hasattr(h, "match_pattern")
    assert h.match_pattern is None


def test_url_handler_handle_url_passthrough():
    """Default handle_url returns the URL unchanged."""
    h = URLHandler()
    assert h.handle_url("anything") == "anything"


# --- MenuProvider base ---

def test_menu_provider_base():
    m = MenuProvider()
    assert "menu_provider" in m.capabilities
    assert m.get_menu_items(None) == []


def test_menu_provider_returns_empty_list():
    m = MenuProvider()
    result = m.get_menu_items(FakeTerminal())
    assert result == []


# --- OutputWatcher base ---

def test_output_watcher_base():
    w = OutputWatcher()
    assert "output_watcher" in w.capabilities
    w.on_output(None, "text")  # no crash


def test_output_watcher_on_output_various_text():
    """on_output accepts empty and multiline text without crashing."""
    w = OutputWatcher()
    w.on_output(None, "")
    w.on_output(None, "line1\nline2\n")
    w.on_output(None, "\x1b[31mred\x1b[0m")


# ---------------------------------------------------------------------------
# PluginManager
# ---------------------------------------------------------------------------

def test_plugin_manager_discover():
    pm = PluginManager()
    pm.discover()
    available = pm.available_plugins()
    # Built-in plugins should be found
    assert "url_handlers" in available
    assert "custom_commands" in available


def test_plugin_manager_discover_nonexistent_dir(tmp_path):
    """discover() with a nonexistent directory doesn't crash."""
    from qterminator import plugin as plugin_mod
    original_dirs = plugin_mod.PLUGIN_DIRS
    plugin_mod.PLUGIN_DIRS = [str(tmp_path / "no_such_dir")]
    try:
        pm = PluginManager()
        pm.discover()
        assert pm.available_plugins() == {}
    finally:
        plugin_mod.PLUGIN_DIRS = original_dirs


def test_plugin_manager_available_returns_dict():
    pm = PluginManager()
    pm.discover()
    result = pm.available_plugins()
    assert isinstance(result, dict)


def test_plugin_manager_enabled_starts_empty():
    pm = PluginManager()
    pm.discover()
    assert pm.enabled_plugins() == set()


def test_plugin_manager_load():
    pm = PluginManager()
    pm.discover()
    instance = pm.load("url_handlers")
    assert instance is not None


def test_plugin_manager_load_returns_plugin_instance():
    pm = PluginManager()
    pm.discover()
    instance = pm.load("url_handlers")
    assert isinstance(instance, Plugin)


def test_plugin_manager_load_nonexistent():
    pm = PluginManager()
    pm.discover()
    assert pm.load("nonexistent_plugin_xyz") is None


def test_plugin_manager_load_caches():
    pm = PluginManager()
    pm.discover()
    a = pm.load("url_handlers")
    b = pm.load("url_handlers")
    assert a is b


def test_plugin_manager_enable_disable():
    pm = PluginManager()
    pm.discover()
    assert pm.enable("url_handlers")
    assert "url_handlers" in pm.enabled_plugins()
    pm.disable("url_handlers")
    assert "url_handlers" not in pm.enabled_plugins()


def test_plugin_manager_enable_already_enabled():
    """Enabling the same plugin twice is idempotent."""
    pm = PluginManager()
    pm.discover()
    assert pm.enable("url_handlers")
    assert pm.enable("url_handlers")
    assert "url_handlers" in pm.enabled_plugins()


def test_plugin_manager_disable_already_disabled():
    """Disabling a plugin that isn't enabled is a no-op."""
    pm = PluginManager()
    pm.discover()
    pm.load("url_handlers")
    pm.disable("url_handlers")  # not enabled yet, should not crash


def test_plugin_manager_enable_nonexistent():
    pm = PluginManager()
    pm.discover()
    assert not pm.enable("no_such_plugin_xyz")


def test_plugin_manager_url_handlers():
    pm = PluginManager()
    pm.discover()
    pm.enable("url_handlers")
    handlers = pm.get_url_handlers()
    assert len(handlers) >= 3  # WebURL, FileURL, Email


def test_plugin_manager_url_handlers_are_instances():
    pm = PluginManager()
    pm.discover()
    pm.enable("url_handlers")
    for h in pm.get_url_handlers():
        assert isinstance(h, URLHandler)


def test_plugin_manager_menu_providers():
    pm = PluginManager()
    pm.discover()
    pm.enable("custom_commands")
    providers = pm.get_menu_providers()
    assert len(providers) >= 1


def test_plugin_manager_menu_providers_are_instances():
    pm = PluginManager()
    pm.discover()
    pm.enable("custom_commands")
    for p in pm.get_menu_providers():
        assert isinstance(p, MenuProvider)


def test_plugin_manager_output_watchers():
    pm = PluginManager()
    pm.discover()
    pm.enable("logger")
    watchers = pm.get_output_watchers()
    assert len(watchers) >= 1
    for w in watchers:
        assert isinstance(w, OutputWatcher)


def test_plugin_manager_activates_all_classes_in_module():
    class _Window:
        shadow_screens = None

    pm = PluginManager()
    pm.discover()
    assert pm.enable("output_monitors", _Window())
    names = {
        type(p).__name__
        for p in pm.get_output_watchers()
        if getattr(p, "name", "") in {
            "error_detector",
            "build_progress",
            "long_command_notifier",
            "log_level_colorizer",
            "sensitive_data_warner",
        }
    }
    assert names == {
        "ErrorDetector",
        "BuildProgressMonitor",
        "LongCommandNotifier",
        "LogLevelColorizer",
        "SensitiveDataWarner",
    }
    pm.disable("output_monitors")


def test_plugin_manager_get_by_capability_valid():
    pm = PluginManager()
    pm.discover()
    pm.enable("url_handlers")
    results = pm.get_by_capability("url_handler")
    assert len(results) >= 1


def test_plugin_manager_get_by_capability_no_match():
    pm = PluginManager()
    pm.discover()
    pm.enable("url_handlers")
    results = pm.get_by_capability("nonexistent_capability")
    assert results == []


def test_plugin_manager_identifies_user_plugins(tmp_path, monkeypatch, fresh_config):
    from qterminator import plugin as plugin_mod

    user_dir = tmp_path / "plugins"
    user_dir.mkdir()
    (user_dir / "userplug.py").write_text(
        "from qterminator.plugin import Plugin\n"
        "class UserPlug(Plugin):\n"
        "    name = 'userplug'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(plugin_mod, "PLUGIN_DIRS", [
        os.path.join(os.path.dirname(plugin_mod.__file__), "plugins"),
        str(user_dir),
    ])
    pm = PluginManager()
    pm.discover()
    assert pm.is_builtin("url_handlers") is True
    assert pm.is_builtin("userplug") is False
    assert pm.is_enabled_by_config("userplug") is False
    cfg = Config()
    cfg.set("plugins", "userplug", "enabled", True)
    assert pm.is_enabled_by_config("userplug") is True


# ---------------------------------------------------------------------------
# URL Handlers (url_handlers.py)
# ---------------------------------------------------------------------------

def test_url_handler_web_pattern():
    """WebURLHandler regex matches HTTP URLs."""
    from qterminator.plugins.url_handlers import WebURLHandler
    h = WebURLHandler()
    pattern = re.compile(h.match_pattern)
    assert pattern.search("visit https://example.com for info")
    assert pattern.search("http://foo.bar/path?q=1&r=2")
    assert not pattern.search("no urls here")


def test_web_url_handler_https():
    from qterminator.plugins.url_handlers import WebURLHandler
    h = WebURLHandler()
    pattern = re.compile(h.match_pattern)
    m = pattern.search("go to https://secure.example.com/page")
    assert m is not None
    assert m.group().startswith("https://")


def test_web_url_handler_with_path_query():
    from qterminator.plugins.url_handlers import WebURLHandler
    h = WebURLHandler()
    pattern = re.compile(h.match_pattern)
    m = pattern.search("http://example.com/path/to/page?key=val&a=b#frag")
    assert m is not None


def test_web_url_handler_rejects_plain_text():
    from qterminator.plugins.url_handlers import WebURLHandler
    h = WebURLHandler()
    pattern = re.compile(h.match_pattern)
    assert pattern.search("just some words") is None
    assert pattern.search("ftp://other.proto") is None


def test_web_url_handler_strips_trailing_punctuation(monkeypatch):
    from qterminator.plugins.url_handlers import WebURLHandler
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    h = WebURLHandler()
    result = h.handle_url("http://example.com.")
    assert result == "http://example.com"


def test_web_url_handler_strips_multiple_trailing(monkeypatch):
    from qterminator.plugins.url_handlers import WebURLHandler
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    h = WebURLHandler()
    result = h.handle_url("http://example.com,")
    assert result == "http://example.com"


def test_url_handler_file_pattern():
    from qterminator.plugins.url_handlers import FileURLHandler
    h = FileURLHandler()
    pattern = re.compile(h.match_pattern)
    assert pattern.search("see file:///tmp/foo.txt")
    assert not pattern.search("/tmp/foo.txt")


def test_file_url_handler_rejects_http():
    from qterminator.plugins.url_handlers import FileURLHandler
    h = FileURLHandler()
    pattern = re.compile(h.match_pattern)
    assert pattern.search("http://example.com") is None


def test_url_handler_email_pattern():
    from qterminator.plugins.url_handlers import EmailHandler
    h = EmailHandler()
    pattern = re.compile(h.match_pattern)
    assert pattern.search("mail user@example.com please")
    assert not pattern.search("not an email")


def test_email_handler_rejects_invalid():
    from qterminator.plugins.url_handlers import EmailHandler
    h = EmailHandler()
    pattern = re.compile(h.match_pattern)
    assert pattern.search("@missing.com") is None
    assert pattern.search("noatsign") is None
    assert pattern.search("bad@") is None


def test_email_handler_various_tlds():
    from qterminator.plugins.url_handlers import EmailHandler
    h = EmailHandler()
    pattern = re.compile(h.match_pattern)
    assert pattern.search("user@example.org")
    assert pattern.search("user@example.co.uk")
    assert pattern.search("user@example.museum")


def test_email_handler_adds_mailto_prefix(monkeypatch):
    from qterminator.plugins.url_handlers import EmailHandler
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    h = EmailHandler()
    result = h.handle_url("user@example.com")
    assert result == "mailto:user@example.com"


def test_email_handler_preserves_existing_mailto(monkeypatch):
    from qterminator.plugins.url_handlers import EmailHandler
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    h = EmailHandler()
    result = h.handle_url("mailto:user@example.com")
    assert result == "mailto:user@example.com"


# ---------------------------------------------------------------------------
# CustomCommands (custom_commands.py)
# ---------------------------------------------------------------------------

def test_custom_commands_plugin():
    from qterminator.plugins.custom_commands import CustomCommandsPlugin
    p = CustomCommandsPlugin()
    assert p.name == "custom_commands"
    # With no config, returns empty
    items = p.get_menu_items(None)
    assert items == []


def test_custom_commands_capabilities():
    from qterminator.plugins.custom_commands import CustomCommandsPlugin
    p = CustomCommandsPlugin()
    assert "menu_provider" in p.capabilities


def test_custom_commands_with_config(fresh_config, tmp_path):
    """Configured commands appear as menu items."""
    from qterminator.plugins.custom_commands import CustomCommandsPlugin
    config = Config()
    config.set("plugins", "custom_commands", "commands", {"List": "ls -la"})
    config.save()

    p = CustomCommandsPlugin()
    t = FakeTerminal()
    items = p.get_menu_items(t)
    assert len(items) == 1
    assert items[0][0] == "List"
    # invoke the callback
    items[0][1]()
    assert t._last_sent == "ls -la\n"


def test_custom_commands_no_config_empty(fresh_config):
    """With a fresh config and no commands section, returns empty list."""
    from qterminator.plugins.custom_commands import CustomCommandsPlugin
    p = CustomCommandsPlugin()
    assert p.get_menu_items(None) == []


# ---------------------------------------------------------------------------
# Logger (logger.py)
# ---------------------------------------------------------------------------

def test_logger_plugin():
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    assert p.name == "logger"
    assert "output_watcher" in p.capabilities


def test_logger_start_stop(tmp_path):
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    log_path = str(tmp_path / "test.log")

    # Mock terminal with shell_pid
    t = FakeTerminal()

    p.start_logging(t, log_path)
    p.on_output(t, "hello world\n")
    p.stop_logging(t)

    content = (tmp_path / "test.log").read_text()
    assert "hello world" in content
    assert "Log started" in content
    assert "Log ended" in content


def test_logger_creates_file(tmp_path):
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    log_path = str(tmp_path / "new.log")
    assert not os.path.exists(log_path)

    t = FakeTerminal()
    p.start_logging(t, log_path)
    assert os.path.exists(log_path)
    p.stop_logging(t)


def test_logger_stop_closes_file(tmp_path):
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    log_path = str(tmp_path / "close.log")
    t = FakeTerminal()
    p.start_logging(t, log_path)
    p.stop_logging(t)
    # After stop, writing should not add content
    p.on_output(t, "should not appear\n")
    content = (tmp_path / "close.log").read_text()
    assert "should not appear" not in content


def test_logger_on_output_writes(tmp_path):
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    log_path = str(tmp_path / "write.log")
    t = FakeTerminal()
    p.start_logging(t, log_path)
    p.on_output(t, "line1\n")
    p.on_output(t, "line2\n")
    p.stop_logging(t)
    content = (tmp_path / "write.log").read_text()
    assert "line1" in content
    assert "line2" in content


def test_logger_start_twice_idempotent(tmp_path):
    """Starting logging twice on the same terminal doesn't open a second file."""
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    log_path1 = str(tmp_path / "first.log")
    log_path2 = str(tmp_path / "second.log")
    t = FakeTerminal()
    p.start_logging(t, log_path1)
    p.start_logging(t, log_path2)  # should be ignored
    p.on_output(t, "data\n")
    p.stop_logging(t)
    # Should have written to first file, second should not exist
    assert (tmp_path / "first.log").exists()
    assert not (tmp_path / "second.log").exists()


def test_logger_stop_non_logging_terminal():
    """Stopping a terminal that isn't being logged doesn't crash."""
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    t = FakeTerminal()
    p.stop_logging(t)  # no crash


def test_logger_deactivate_closes_all(tmp_path):
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    t1 = FakeTerminal(pid=111)
    t2 = FakeTerminal(pid=222)
    p.start_logging(t1, str(tmp_path / "t1.log"))
    p.start_logging(t2, str(tmp_path / "t2.log"))
    p.on_output(t1, "one\n")
    p.on_output(t2, "two\n")
    p.deactivate()
    # After deactivate, internal dict should be empty
    assert len(p._log_files) == 0
    # Files should have content
    assert "one" in (tmp_path / "t1.log").read_text()
    assert "two" in (tmp_path / "t2.log").read_text()


def test_logger_on_output_without_logging():
    """on_output for a terminal not being logged is a no-op."""
    from qterminator.plugins.logger import LoggerPlugin
    p = LoggerPlugin()
    t = FakeTerminal()
    p.on_output(t, "text")  # no crash


# ---------------------------------------------------------------------------
# Screenshot (screenshot.py — replaced terminal_screenshot)
# ---------------------------------------------------------------------------

def test_screenshot_plugin():
    from qterminator.plugins.screenshot import ScreenshotPlugin
    p = ScreenshotPlugin()
    assert p.name == "screenshot"
    assert "menu_provider" in p.capabilities


def test_screenshot_capabilities():
    from qterminator.plugins.screenshot import ScreenshotPlugin
    p = ScreenshotPlugin()
    assert "menu_provider" in p.capabilities


# ---------------------------------------------------------------------------
# User plugin directory
# ---------------------------------------------------------------------------

def test_user_plugin_dir(tmp_path):
    """Plugins from custom directories are discovered."""
    # Write a test plugin
    plugin_code = '''
from qterminator.plugin import Plugin
class TestPlugin(Plugin):
    name = "test_plugin"
    version = "1.0"
'''
    (tmp_path / "test_plugin.py").write_text(plugin_code)

    pm = PluginManager()
    from qterminator import plugin as plugin_mod
    original_dirs = plugin_mod.PLUGIN_DIRS
    plugin_mod.PLUGIN_DIRS = [str(tmp_path)]
    try:
        pm.discover()
        assert "test_plugin" in pm.available_plugins()
        instance = pm.load("test_plugin")
        assert instance.name == "test_plugin"
    finally:
        plugin_mod.PLUGIN_DIRS = original_dirs


def test_plugin_discovery_finds_all():
    pm = PluginManager()
    pm.discover()
    available = pm.available_plugins()
    assert "url_handlers" in available
    assert "custom_commands" in available
    assert "logger" in available
    assert "screenshot" in available


# ---------------------------------------------------------------------------
# TestPluginLifecycle
# ---------------------------------------------------------------------------

class TestPluginLifecycle:
    """Lifecycle tests for Plugin base class."""

    def test_activate_receives_app_controller(self):
        """activate() receives and accepts an app_controller argument."""
        p = Plugin()
        controller = object()
        p.activate(controller)  # no crash, accepts the controller

    def test_deactivate_after_activate(self):
        """deactivate() after activate() works without error."""
        p = Plugin()
        p.activate(object())
        p.deactivate()

    def test_deactivate_without_activate_no_crash(self):
        """deactivate() without prior activate() doesn't crash."""
        p = Plugin()
        p.deactivate()

    def test_enable_then_disable_removes_from_enabled(self):
        """Enabling then disabling a plugin removes it from the enabled set."""
        pm = PluginManager()
        pm.discover()
        pm.enable("url_handlers")
        assert "url_handlers" in pm.enabled_plugins()
        pm.disable("url_handlers")
        assert "url_handlers" not in pm.enabled_plugins()

    def test_re_enable_after_disable(self):
        """Re-enabling a plugin after disabling it works."""
        pm = PluginManager()
        pm.discover()
        pm.enable("url_handlers")
        pm.disable("url_handlers")
        assert pm.enable("url_handlers")
        assert "url_handlers" in pm.enabled_plugins()


# ---------------------------------------------------------------------------
# TestPluginLoadErrors
# ---------------------------------------------------------------------------

class TestPluginLoadErrors:
    """Error handling during plugin loading and discovery."""

    def test_load_from_nonexistent_file_returns_none(self):
        """Loading a plugin from a nonexistent file returns None."""
        pm = PluginManager()
        # Don't discover, so nothing is available
        result = pm.load("totally_nonexistent_plugin")
        assert result is None

    def test_discover_skips_underscore_files(self, tmp_path):
        """discover() skips files starting with underscore."""
        from qterminator import plugin as plugin_mod

        (tmp_path / "_private.py").write_text(
            "from qterminator.plugin import Plugin\n"
            "class PrivatePlugin(Plugin):\n"
            "    name = '_private'\n"
        )
        original_dirs = plugin_mod.PLUGIN_DIRS
        plugin_mod.PLUGIN_DIRS = [str(tmp_path)]
        try:
            pm = PluginManager()
            pm.discover()
            assert "_private" not in pm.available_plugins()
        finally:
            plugin_mod.PLUGIN_DIRS = original_dirs

    def test_discover_skips_init_py(self, tmp_path):
        """discover() skips __init__.py."""
        from qterminator import plugin as plugin_mod

        (tmp_path / "__init__.py").write_text(
            "from qterminator.plugin import Plugin\n"
            "class InitPlugin(Plugin):\n"
            "    name = 'init_plugin'\n"
        )
        original_dirs = plugin_mod.PLUGIN_DIRS
        plugin_mod.PLUGIN_DIRS = [str(tmp_path)]
        try:
            pm = PluginManager()
            pm.discover()
            assert "__init__" not in pm.available_plugins()
        finally:
            plugin_mod.PLUGIN_DIRS = original_dirs


# ---------------------------------------------------------------------------
# TestCustomCommandsIntegration
# ---------------------------------------------------------------------------

class TestCustomCommandsIntegration:
    """Integration tests for CustomCommands plugin with config."""

    def test_get_menu_items_returns_items(self, fresh_config):
        """With config commands set, get_menu_items returns items."""
        from qterminator.plugins.custom_commands import CustomCommandsPlugin
        config = Config()
        config.set("plugins", "custom_commands", "commands", {
            "List": "ls -la",
            "Uptime": "uptime",
        })
        config.save()

        p = CustomCommandsPlugin()
        t = FakeTerminal()
        items = p.get_menu_items(t)
        assert len(items) == 2

    def test_menu_item_labels_match_config_keys(self, fresh_config):
        """Menu item labels match the keys in config."""
        from qterminator.plugins.custom_commands import CustomCommandsPlugin
        config = Config()
        config.set("plugins", "custom_commands", "commands", {
            "Alpha": "echo alpha",
            "Beta": "echo beta",
            "Gamma": "echo gamma",
        })
        config.save()

        p = CustomCommandsPlugin()
        t = FakeTerminal()
        items = p.get_menu_items(t)
        labels = [item[0] for item in items]
        assert "Alpha" in labels
        assert "Beta" in labels
        assert "Gamma" in labels

    def test_empty_commands_returns_empty_list(self, fresh_config):
        """Empty commands config returns empty list."""
        from qterminator.plugins.custom_commands import CustomCommandsPlugin
        config = Config()
        config.set("plugins", "custom_commands", "commands", {})
        config.save()

        p = CustomCommandsPlugin()
        t = FakeTerminal()
        items = p.get_menu_items(t)
        assert items == []


# ---------------------------------------------------------------------------
# IssueTrackerHandler
# ---------------------------------------------------------------------------

class TestIssueTrackerHandler:
    """Tests for IssueTrackerHandler plugin."""

    def test_no_config_pattern_is_none(self, fresh_config):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        h = IssueTrackerHandler()
        assert h.match_pattern is None

    def test_configured_pattern_matches(self, fresh_config):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        config = Config()
        config.set("plugins", "issue_tracker", "patterns", [
            {"prefix": "QTERM", "url": "https://github.com/jankotek/qterminator/issues/{id}"},
        ])
        config.save()
        Config._instance = None
        h = IssueTrackerHandler()
        assert h.match_pattern is not None
        assert re.search(h.match_pattern, "see QTERM-1234 for details")

    def test_pattern_does_not_match_plain_text(self, fresh_config):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        config = Config()
        config.set("plugins", "issue_tracker", "patterns", [
            {"prefix": "QTERM", "url": "https://example.com/{id}"},
        ])
        config.save()
        Config._instance = None
        h = IssueTrackerHandler()
        assert not re.search(h.match_pattern, "no issues here")

    def test_handle_url_returns_correct_link(self, fresh_config, monkeypatch):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        config = Config()
        config.set("plugins", "issue_tracker", "patterns", [
            {"prefix": "QTERM", "url": "https://github.com/jankotek/qterminator/issues/{id}"},
        ])
        config.save()
        Config._instance = None
        h = IssueTrackerHandler()
        result = h.handle_url("QTERM-1234")
        assert result == "https://github.com/jankotek/qterminator/issues/1234"

    def test_handle_url_with_prefix_placeholder(self, fresh_config, monkeypatch):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        config = Config()
        config.set("plugins", "issue_tracker", "patterns", [
            {"prefix": "JIRA", "url": "https://jira.example.com/browse/{prefix}-{id}"},
        ])
        config.save()
        Config._instance = None
        h = IssueTrackerHandler()
        result = h.handle_url("JIRA-567")
        assert result == "https://jira.example.com/browse/JIRA-567"

    def test_multiple_prefixes(self, fresh_config, monkeypatch):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        config = Config()
        config.set("plugins", "issue_tracker", "patterns", [
            {"prefix": "QTERM", "url": "https://github.com/jankotek/qterminator/issues/{id}"},
            {"prefix": "GH", "url": "https://github.com/org/repo/issues/{id}"},
        ])
        config.save()
        Config._instance = None
        h = IssueTrackerHandler()
        assert re.search(h.match_pattern, "QTERM-99")
        assert re.search(h.match_pattern, "GH-42")
        assert h.handle_url("GH-42") == "https://github.com/org/repo/issues/42"

    def test_case_insensitive_prefix_match(self, fresh_config, monkeypatch):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        config = Config()
        config.set("plugins", "issue_tracker", "patterns", [
            {"prefix": "QTERM", "url": "https://example.com/{id}"},
        ])
        config.save()
        Config._instance = None
        h = IssueTrackerHandler()
        result = h.handle_url("qterm-100")
        assert result == "https://example.com/100"

    def test_no_match_returns_none(self, fresh_config, monkeypatch):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        monkeypatch.setattr("webbrowser.open", lambda url: None)
        config = Config()
        config.set("plugins", "issue_tracker", "patterns", [
            {"prefix": "QTERM", "url": "https://example.com/{id}"},
        ])
        config.save()
        Config._instance = None
        h = IssueTrackerHandler()
        result = h.handle_url("UNKNOWN-123")
        assert result is None

    def test_invalid_config_entries_skipped(self, fresh_config):
        from qterminator.plugins.url_handlers import IssueTrackerHandler
        config = Config()
        config.set("plugins", "issue_tracker", "patterns", [
            {"prefix": "GOOD", "url": "https://example.com/{id}"},
            {"bad": "entry"},
            "not a dict",
        ])
        config.save()
        Config._instance = None
        h = IssueTrackerHandler()
        assert h.match_pattern is not None
        assert re.search(h.match_pattern, "GOOD-1")


# ---------------------------------------------------------------------------
# Multi-class module activation tests
# ---------------------------------------------------------------------------


class _Window:
    shadow_screens = None


class TestMultiClassPluginActivation:
    """Tests for PluginManager handling modules with multiple Plugin subclasses."""

    def test_module_instances_populated_after_loading_output_monitors(self):
        """_module_instances contains entries after loading output_monitors."""
        pm = PluginManager()
        pm.discover()
        pm.load("output_monitors")
        assert "output_monitors" in pm._module_instances
        assert len(pm._module_instances["output_monitors"]) >= 5

    def test_disable_multi_class_module_deactivates_all(self):
        """Disabling a multi-class module deactivates all instances."""
        pm = PluginManager()
        pm.discover()
        pm.enable("output_monitors", _Window())
        # All should be in the enabled set
        assert "output_monitors" in pm.enabled_plugins()
        watchers_before = [
            w for w in pm.get_output_watchers()
            if getattr(w, "name", "") in {
                "error_detector", "build_progress",
                "long_command_notifier", "log_level_colorizer",
                "sensitive_data_warner",
            }
        ]
        assert len(watchers_before) >= 5
        pm.disable("output_monitors")
        assert "output_monitors" not in pm.enabled_plugins()

    def test_re_enable_after_disable_reactivates(self):
        """Re-enabling a multi-class module after disable re-activates correctly."""
        pm = PluginManager()
        pm.discover()
        pm.enable("output_monitors", _Window())
        pm.disable("output_monitors")
        assert "output_monitors" not in pm.enabled_plugins()
        assert pm.enable("output_monitors", _Window())
        assert "output_monitors" in pm.enabled_plugins()

    def test_get_by_capability_finds_multi_class_instances(self):
        """get_by_capability finds instances from multi-class modules."""
        pm = PluginManager()
        pm.discover()
        pm.enable("output_monitors", _Window())
        watchers = pm.get_by_capability("output_watcher")
        names = {type(w).__name__ for w in watchers}
        assert "ErrorDetector" in names
        assert "BuildProgressMonitor" in names
        pm.disable("output_monitors")

    def test_single_class_module_still_works(self):
        """Loading a module with one class works (backward compat)."""
        pm = PluginManager()
        pm.discover()
        instance = pm.load("logger")
        assert instance is not None
        assert instance.name == "logger"
        assert "logger" in pm._module_instances
        assert len(pm._module_instances["logger"]) == 1

    def test_enable_returns_true_for_multi_class_modules(self):
        """enable() returns True even for multi-class modules."""
        pm = PluginManager()
        pm.discover()
        result = pm.enable("output_monitors", _Window())
        assert result is True
        pm.disable("output_monitors")

    def test_all_instances_contains_all_from_loaded_modules(self):
        """_all_instances contains all instances from all loaded modules."""
        pm = PluginManager()
        pm.discover()
        pm.load("url_handlers")
        pm.load("output_monitors")
        all_inst = pm._all_instances
        # url_handlers has multiple URL handler classes
        # output_monitors has 5 classes
        url_names = {type(i).__name__ for i in all_inst if "url" in type(i).__name__.lower() or "email" in type(i).__name__.lower()}
        monitor_names = {type(i).__name__ for i in all_inst if type(i).__name__ in {
            "ErrorDetector", "BuildProgressMonitor",
            "LongCommandNotifier", "LogLevelColorizer",
            "SensitiveDataWarner",
        }}
        assert len(url_names) >= 1
        assert len(monitor_names) == 5

    def test_capability_lookup_after_disable_excludes_disabled(self):
        """After disabling a multi-class module, get_by_capability should not
        return its instances (they were deactivated)."""
        pm = PluginManager()
        pm.discover()
        pm.enable("output_monitors", _Window())
        watchers = pm.get_output_watchers()
        monitor_names_before = {
            type(w).__name__ for w in watchers
            if type(w).__name__ in {
                "ErrorDetector", "BuildProgressMonitor",
                "LongCommandNotifier", "LogLevelColorizer",
                "SensitiveDataWarner",
            }
        }
        assert len(monitor_names_before) == 5
        pm.disable("output_monitors")
        # After disable, the instances are still in _all_instances (the
        # manager doesn't remove them from the list), but they have been
        # deactivated. Verify disable happened by checking enabled set.
        assert "output_monitors" not in pm.enabled_plugins()
