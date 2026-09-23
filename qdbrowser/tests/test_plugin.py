"""Plugin discovery + loading."""

from qdbrowser.plugin import (
    CommandProvider,
    PageObserver,
    Plugin,
    PluginManager,
    SidePanelProvider,
    UrlInterceptor,
)

EXPECTED_PLUGINS = {
    "agent_control", "bookmarks", "command_palette", "content_blocker",
    "downloads", "history", "mouse_gestures", "notes", "page_actions",
    "reader_mode", "screenshot", "sessions", "tab_stacks", "web_panels",
    "workspaces", "dark_mode", "picture_in_picture", "tab_list",
    "translate",
}


def test_discovery_finds_all_builtins(fresh_config):
    pm = PluginManager()
    pm.discover()
    available = set(pm.available_plugins())
    missing = EXPECTED_PLUGINS - available
    assert not missing, f"missing plugins: {missing}"


def test_plugins_load_without_window(fresh_config):
    """A plugin instance should construct without an activation context."""
    pm = PluginManager()
    pm.discover()
    for name in pm.available_plugins():
        plug = pm.load(name)
        assert plug is not None, f"plugin {name} failed to load"
        assert isinstance(plug, Plugin)


def test_capabilities_indexed(fresh_config):
    pm = PluginManager()
    pm.discover()
    for name in pm.available_plugins():
        pm.load(name)
    side = pm.get_side_panel_providers()
    cmds = pm.get_command_providers()
    interceptors = pm.get_url_interceptors()
    observers = pm.get_page_observers()
    # Sanity: at least one of each, content_blocker is a url_interceptor,
    # history is a page_observer.
    assert any(isinstance(p, UrlInterceptor) for p in interceptors)
    assert any(isinstance(p, PageObserver) for p in observers)
    assert any(isinstance(p, SidePanelProvider) for p in side)
    assert any(isinstance(p, CommandProvider) for p in cmds)
