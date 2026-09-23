"""Dark mode plugin: effective-mode logic + JS injection."""

from unittest.mock import MagicMock


def _make_plugin(fresh_config, desktop_dark=True):
    from qdbrowser.plugins.dark_mode import DarkModePlugin

    class FakeWin:
        _resolved_theme = "dark" if desktop_dark else "light"
        # Set by ``_set_override`` after a state change; default to None
        # so the plugin's "reapply on change" branch becomes a no-op.
        _active_webview = None

    plug = DarkModePlugin()
    plug.activate(FakeWin())
    return plug


def test_auto_with_dark_desktop(fresh_config):
    plug = _make_plugin(fresh_config, desktop_dark=True)
    assert plug._effective_mode("example.com") == "always"


def test_auto_with_light_desktop(fresh_config):
    plug = _make_plugin(fresh_config, desktop_dark=False)
    assert plug._effective_mode("example.com") == "off"


def test_explicit_always(fresh_config):
    plug = _make_plugin(fresh_config, desktop_dark=False)
    plug._set_override("example.com", "always")
    assert plug._effective_mode("example.com") == "always"


def test_explicit_never(fresh_config):
    plug = _make_plugin(fresh_config, desktop_dark=True)
    plug._set_override("example.com", "never")
    assert plug._effective_mode("example.com") == "off"


def test_contrast_mode(fresh_config):
    plug = _make_plugin(fresh_config, desktop_dark=False)
    plug._set_override("example.com", "contrast")
    assert plug._effective_mode("example.com") == "contrast"


def test_override_persists(fresh_config):
    from qdbrowser.config import Config
    plug = _make_plugin(fresh_config)
    plug._set_override("foo.com", "never")
    saved = Config().get("dark_mode", "site_overrides")
    assert saved.get("foo.com") == "never"


def test_override_suffix_match(fresh_config):
    plug = _make_plugin(fresh_config, desktop_dark=True)
    plug._set_override("news.test", "never")
    assert plug._effective_mode("sub.news.test") == "off"


def test_override_auto_clears(fresh_config):
    plug = _make_plugin(fresh_config)
    plug._set_override("foo.com", "never")
    plug._set_override("foo.com", "auto")
    assert "foo.com" not in plug._site_overrides


def test_cycle_global(fresh_config):
    plug = _make_plugin(fresh_config)
    plug._window = None  # prevent webview apply
    plug._global_default = "auto"
    plug._cycle_global()
    assert plug._global_default == "always"
    plug._cycle_global()
    assert plug._global_default == "never"


def test_apply_runs_js(fresh_config):
    plug = _make_plugin(fresh_config, desktop_dark=True)
    wv = MagicMock()
    wv.url.return_value = "https://example.com"
    plug.apply(wv)
    wv.view.page().runJavaScript.assert_called_once()
    js = wv.view.page().runJavaScript.call_args[0][0]
    assert "__qdb_force_dark_style" in js
    assert '"always"' in js or "'always'" in js


def test_apply_emits_off_when_never(fresh_config):
    plug = _make_plugin(fresh_config, desktop_dark=True)
    # Set override directly so we skip the auto-apply on _active_webview.
    plug._site_overrides["example.com"] = "never"
    wv = MagicMock()
    wv.url.return_value = "https://example.com"
    plug.apply(wv)
    js = wv.view.page().runJavaScript.call_args[0][0]
    assert '"off"' in js


def test_apply_none_safe(fresh_config):
    plug = _make_plugin(fresh_config)
    plug.apply(None)  # no exception


def test_commands_include_global_cycle(fresh_config, window):
    plug = window.plugins._instances["dark_mode"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("Force dark" in label for label in labels)


def test_on_load_finished_calls_apply(fresh_config, window):
    from unittest.mock import patch
    plug = window.plugins._instances["dark_mode"]
    with patch.object(plug, "apply") as a:
        plug.on_load_finished(window._active_webview, True)
        a.assert_called_once_with(window._active_webview)


def test_on_load_finished_ignored_when_not_ok(fresh_config, window):
    from unittest.mock import patch
    plug = window.plugins._instances["dark_mode"]
    with patch.object(plug, "apply") as a:
        plug.on_load_finished(window._active_webview, False)
        a.assert_not_called()
