"""MainWindow smoke + tab/split lifecycle."""

from PyQt6.QtCore import Qt
from qdbrowser.config import Config
from qdbrowser.window import MainWindow


def test_window_starts_with_one_tab(window):
    assert window._tabs.count() == 1


def test_new_tab_focuses_new(window):
    a = window._active_webview
    b = window.new_tab(url="about:blank")
    assert b is not a
    assert window._active_webview is b
    assert window._tabs.count() == 2


def test_close_current_tab_keeps_window_alive(window):
    window.new_tab()
    assert window._tabs.count() == 2
    window._close_current_tab()
    assert window._tabs.count() == 1


def test_split_creates_pane(window):
    before = len(window._tabs.widget(0).find_webviews())
    window._split(Qt.Orientation.Horizontal)
    after = len(window._tabs.widget(0).find_webviews())
    assert after == before + 1


def test_close_split_decrements(window):
    window._split(Qt.Orientation.Horizontal)
    before = len(window._tabs.widget(0).find_webviews())
    window._close_active_split()
    after = len(window._tabs.widget(0).find_webviews())
    assert after == before - 1


def test_default_plugins_enabled(window):
    enabled = window.plugins.enabled_plugins()
    for p in ("bookmarks", "history", "downloads", "notes",
              "command_palette", "content_blocker", "sessions",
              "screenshot", "reader_mode", "dark_mode",
              "picture_in_picture", "tab_list", "translate"):
        assert p in enabled, f"{p} should be enabled by default"


def _window_config_probe():
    win = MainWindow.__new__(MainWindow)
    win._config = Config()
    return win


def test_bridge_adapter_default_follows_daemon_probe(
        fresh_config, monkeypatch):
    import qdbrowser.plugins.bridge_adapter as ba
    win = _window_config_probe()

    monkeypatch.setattr(ba, "_daemons_available", lambda: True)
    assert win._should_enable_bridge_adapter() is True

    monkeypatch.setattr(ba, "_daemons_available", lambda: False)
    assert win._should_enable_bridge_adapter() is False


def test_bridge_adapter_nested_enabled_config_wins(
        fresh_config, monkeypatch):
    import qdbrowser.plugins.bridge_adapter as ba
    win = _window_config_probe()
    monkeypatch.setattr(ba, "_daemons_available", lambda: True)

    Config().set("plugins", "bridge_adapter", "enabled", False)
    assert win._should_enable_bridge_adapter() is False

    Config().set("plugins", "bridge_adapter", "enabled", True)
    monkeypatch.setattr(ba, "_daemons_available", lambda: False)
    assert win._should_enable_bridge_adapter() is True


def test_bridge_adapter_flat_enabled_config_wins(fresh_config, monkeypatch):
    import qdbrowser.plugins.bridge_adapter as ba
    win = _window_config_probe()
    monkeypatch.setattr(ba, "_daemons_available", lambda: True)

    Config().set("plugins", "bridge_adapter", False)
    assert win._should_enable_bridge_adapter() is False

    Config().set("plugins", "bridge_adapter", True)
    monkeypatch.setattr(ba, "_daemons_available", lambda: False)
    assert win._should_enable_bridge_adapter() is True


def test_side_panel_has_panels(window):
    panels = window._side_panel.panel_ids()
    for needed in ("bookmarks", "history", "downloads", "notes",
                   "tab_list", "web_panels"):
        assert needed in panels
