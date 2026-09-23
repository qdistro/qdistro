"""Deeper coverage of the plugin system."""


import pytest


def test_plugin_base_class_lifecycle():
    from qdbrowser.plugin import Plugin
    p = Plugin()
    # activate / deactivate are no-ops by default but must exist.
    p.activate(None)
    p.deactivate()


def test_url_interceptor_default_intercept_is_noop():
    from qdbrowser.plugin import UrlInterceptor
    UrlInterceptor().intercept(None)  # should not raise


def test_command_provider_returns_empty_by_default():
    from qdbrowser.plugin import CommandProvider
    assert CommandProvider().get_commands(None) == []


def test_page_observer_handlers_are_noop():
    from qdbrowser.plugin import PageObserver
    obs = PageObserver()
    obs.on_navigation(None, "")
    obs.on_load_finished(None, True)
    obs.on_title_changed(None, "")


def test_side_panel_provider_must_implement_build():
    from qdbrowser.plugin import SidePanelProvider
    with pytest.raises(NotImplementedError):
        SidePanelProvider().build_panel(None)


def test_plugin_manager_discover_empty_dir(tmp_path, monkeypatch, fresh_config):
    from qdbrowser import plugin as plug
    monkeypatch.setattr(plug, "PLUGIN_DIRS", [str(tmp_path / "nope")])
    pm = plug.PluginManager()
    pm.discover()
    assert pm.available_plugins() == {}


def test_plugin_manager_load_missing_returns_none(fresh_config):
    from qdbrowser.plugin import PluginManager
    pm = PluginManager()
    assert pm.load("does_not_exist") is None


def test_plugin_manager_load_caches(fresh_config):
    from qdbrowser.plugin import PluginManager
    pm = PluginManager()
    pm.discover()
    a = pm.load("bookmarks")
    b = pm.load("bookmarks")
    assert a is b


def test_plugin_manager_skips_dunder_files(tmp_path, monkeypatch, fresh_config):
    from qdbrowser import plugin as plug
    plugin_dir = tmp_path / "plug"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("")
    (plugin_dir / "_hidden.py").write_text("class X: pass")
    (plugin_dir / "good.py").write_text(
        "from qdbrowser.plugin import Plugin\n"
        "class G(Plugin): name='good'\n")
    monkeypatch.setattr(plug, "PLUGIN_DIRS", [str(plugin_dir)])
    pm = plug.PluginManager()
    pm.discover()
    avail = pm.available_plugins()
    assert "good" in avail
    assert "_hidden" not in avail
    assert "__init__" not in avail


def test_plugin_manager_user_dir_extends_builtins(tmp_path, monkeypatch,
                                                  fresh_config):
    from qdbrowser import plugin as plug
    user_dir = tmp_path / "user_plugins"
    user_dir.mkdir()
    (user_dir / "user_one.py").write_text(
        "from qdbrowser.plugin import Plugin\n"
        "class U(Plugin): name='user_one'\n")
    monkeypatch.setattr(plug, "PLUGIN_DIRS",
                        plug.PLUGIN_DIRS + [str(user_dir)])
    pm = plug.PluginManager()
    pm.discover()
    avail = pm.available_plugins()
    assert "user_one" in avail
    assert "bookmarks" in avail


def test_plugin_manager_enable_disable(fresh_config):
    from qdbrowser.plugin import PluginManager
    pm = PluginManager()
    pm.discover()
    assert pm.enable("bookmarks", app_controller=None)
    assert "bookmarks" in pm.enabled_plugins()
    pm.disable("bookmarks")
    assert "bookmarks" not in pm.enabled_plugins()


def test_plugin_manager_enable_missing_returns_false(fresh_config):
    from qdbrowser.plugin import PluginManager
    pm = PluginManager()
    pm.discover()
    assert pm.enable("does_not_exist", app_controller=None) is False


def test_capabilities_index_filters_correctly(fresh_config):
    from qdbrowser.plugin import PluginManager
    pm = PluginManager()
    pm.discover()
    for name in pm.available_plugins():
        pm.load(name)
    # content_blocker is a url_interceptor.
    names = [type(p).__name__
             for p in pm.get_url_interceptors()]
    assert "ContentBlockerPlugin" in names


def test_plugin_manager_load_doubled_doesnt_reimport(fresh_config):
    import sys

    from qdbrowser.plugin import PluginManager
    pm = PluginManager()
    pm.discover()
    pm.load("bookmarks")
    mod_id = id(sys.modules["qdbrowser.plugins.bookmarks"])
    pm.load("bookmarks")
    assert id(sys.modules["qdbrowser.plugins.bookmarks"]) == mod_id
