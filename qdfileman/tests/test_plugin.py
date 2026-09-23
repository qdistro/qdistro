"""Tests for the plugin system."""


from qfileman.plugin import (
    FileFilter,
    MenuProvider,
    NavigationHook,
    Plugin,
    PluginManager,
)


class FakeFileItem:
    """Minimal stand-in for a file item."""

    def __init__(self, path="/fake/path"):
        self.path = path


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


# --- MenuProvider base ---

def test_menu_provider_base():
    m = MenuProvider()
    assert "menu_provider" in m.capabilities
    assert m.get_menu_items(None) == []


def test_menu_provider_returns_empty_list():
    m = MenuProvider()
    assert m.get_menu_items("/some/path") == []


def test_bookmarks_plugin_remove_appears_only_when_bookmarked(tmp_path, monkeypatch):
    """Regression: BookmarksPlugin must add the Remove entry when the
    right-clicked path is bookmarked (previously compared a QListWidgetItem
    against a path string and never matched)."""
    from qfileman import config as config_mod
    from qfileman.config import Config
    from qfileman.plugins.builtin.bookmarks import BookmarksPlugin

    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    Config._data = None
    try:
        Config().add_bookmark("/home/user/Docs", "Docs")
        plugin = BookmarksPlugin()
        items = plugin.get_menu_items("/home/user/Docs")
        labels = [label for label, _cb in items]
        assert "Add to Bookmarks" in labels
        assert any(label.startswith("Remove Bookmark") for label in labels), (
            f"Remove entry should appear for bookmarked path; got {labels}"
        )

        # Different path → only Add, no Remove.
        items_other = plugin.get_menu_items("/elsewhere")
        labels_other = [label for label, _cb in items_other]
        assert labels_other == ["Add to Bookmarks"]
    finally:
        Config._instance = None
        Config._data = None


def test_menu_provider_category():
    m = MenuProvider()
    assert m.category == "Plugins"


# --- NavigationHook base ---

def test_navigation_hook_base():
    h = NavigationHook()
    assert "navigation_hook" in h.capabilities


def test_navigation_hook_on_enter_directory():
    """Default on_enter_directory returns True."""
    h = NavigationHook()
    assert h.on_enter_directory("/path") is True


def test_navigation_hook_on_leave_directory():
    """on_leave_directory doesn't crash."""
    h = NavigationHook()
    h.on_leave_directory("/path")  # no crash


def test_navigation_hook_on_double_click():
    """Default on_double_click returns False."""
    h = NavigationHook()
    assert h.on_double_click(FakeFileItem()) is False


# --- FileFilter base ---

def test_file_filter_base():
    f = FileFilter()
    assert "file_filter" in f.capabilities


def test_file_filter_filter_files():
    """Default filter_files returns input unchanged."""
    f = FileFilter()
    files = ["/path/file1.txt", "/path/file2.txt"]
    assert f.filter_files(files) == files


def test_file_filter_filter_name():
    """Default filter_name returns True."""
    f = FileFilter()
    assert f.filter_name("anything") is True


# ---------------------------------------------------------------------------
# PluginManager
# ---------------------------------------------------------------------------

def test_plugin_manager_discover():
    pm = PluginManager()
    pm.discover()
    available = pm.available_plugins()
    # Built-in plugins should be found
    assert "bookmarks" in available
    assert "file_info" in available
    assert "quick_nav" in available
    assert "filter" in available


def test_plugin_manager_discover_nonexistent_dir(tmp_path):
    """discover() with a nonexistent directory doesn't crash."""
    from qfileman import plugin as plugin_mod
    original_dirs = plugin_mod.PLUGIN_DIRS
    plugin_mod.PLUGIN_DIRS = [str(tmp_path / "no_such_dir")]
    try:
        pm = PluginManager()
        pm.discover()
        assert pm.available_plugins() == {}
    finally:
        plugin_mod.PLUGIN_DIRS = original_dirs


def test_plugin_manager_load():
    pm = PluginManager()
    pm.discover()
    plugin = pm.load("bookmarks")
    assert plugin is not None
    assert isinstance(plugin, MenuProvider)


def test_plugin_manager_load_nonexistent():
    pm = PluginManager()
    pm.discover()
    plugin = pm.load("nonexistent_plugin_xyz")
    assert plugin is None


def test_plugin_manager_enable():
    pm = PluginManager()
    pm.discover()
    result = pm.enable("bookmarks", None)
    assert result is True
    assert "bookmarks" in pm.enabled_plugins()


def test_plugin_manager_disable():
    pm = PluginManager()
    pm.discover()
    pm.enable("bookmarks", None)
    pm.disable("bookmarks")
    assert "bookmarks" not in pm.enabled_plugins()


def test_plugin_manager_get_by_capability():
    """get_menu_providers returns exactly the enabled bookmarks plugin."""
    pm = PluginManager()
    pm.discover()
    pm.enable("bookmarks")
    providers = pm.get_menu_providers()
    names = [p.name for p in providers]
    assert "bookmarks" in names
    # All returned providers must actually carry the menu_provider capability.
    assert all("menu_provider" in p.capabilities for p in providers)


def test_plugin_manager_get_navigation_hooks():
    """get_navigation_hooks returns the enabled quick_nav hook."""
    pm = PluginManager()
    pm.discover()
    pm.enable("quick_nav")
    hooks = pm.get_navigation_hooks()
    assert any(h.name == "quick_nav" for h in hooks), \
        f"quick_nav should be in hooks, got {[h.name for h in hooks]}"
    assert all("navigation_hook" in h.capabilities for h in hooks)


def test_plugin_manager_get_file_filters():
    """get_file_filters returns the enabled filter plugin."""
    pm = PluginManager()
    pm.discover()
    pm.enable("filter")
    filters = pm.get_file_filters()
    assert any(f.name == "filter" for f in filters), \
        f"filter should be in filters, got {[f.name for f in filters]}"
    assert all("file_filter" in f.capabilities for f in filters)


def test_plugin_manager_twice_enable():
    """Enabling a plugin twice doesn't break anything."""
    pm = PluginManager()
    pm.discover()
    pm.enable("bookmarks")
    pm.enable("bookmarks")  # Should not raise
    assert "bookmarks" in pm.enabled_plugins()
    # The cached instance should still be the singleton.
    assert pm.load("bookmarks") is pm.load("bookmarks")


def test_plugin_manager_caching():
    """Loading a plugin twice returns the same instance."""
    pm = PluginManager()
    pm.discover()
    p1 = pm.load("bookmarks")
    p2 = pm.load("bookmarks")
    assert p1 is p2


def test_plugin_manager_load_broken_module_logs_and_returns_none(tmp_path, caplog):
    """A plugin module that raises during import should not crash the manager."""
    from qfileman import plugin as plugin_mod

    broken_dir = tmp_path / "broken_plugins"
    broken_dir.mkdir()
    (broken_dir / "kaboom.py").write_text("raise RuntimeError('boom')\n")

    original_dirs = plugin_mod.PLUGIN_DIRS
    plugin_mod.PLUGIN_DIRS = [str(broken_dir)]
    try:
        pm = PluginManager()
        pm.discover()
        assert "kaboom" in pm.available_plugins()

        with caplog.at_level("WARNING", logger="qfileman.plugin"):
            result = pm.load("kaboom")

        assert result is None, "Broken plugin should not load to an instance"
        assert any("kaboom" in r.message and "failed to load" in r.message
                   for r in caplog.records), \
            f"Expected failure warning, got {[r.message for r in caplog.records]}"
    finally:
        plugin_mod.PLUGIN_DIRS = original_dirs


def test_plugin_manager_disable_unknown_is_noop():
    """Disabling a plugin that isn't loaded must not raise."""
    pm = PluginManager()
    pm.discover()
    # Should be a no-op, not an exception.
    pm.disable("nonexistent_plugin_xyz")
    assert "nonexistent_plugin_xyz" not in pm.enabled_plugins()
