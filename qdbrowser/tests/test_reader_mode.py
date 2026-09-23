"""Reader mode plugin."""

from unittest.mock import MagicMock


def test_plugin_has_command(window):
    plug = window.plugins._instances["reader_mode"]
    cmds = plug.get_commands(window)
    assert any("reader" in label.lower() for label, _ in cmds)


def test_toggle_with_none_safe(window):
    plug = window.plugins._instances["reader_mode"]
    plug.toggle(None)  # must not raise


def test_toggle_invokes_runjs(window):
    plug = window.plugins._instances["reader_mode"]
    wv = window._active_webview
    wv.view.page().runJavaScript = MagicMock()
    plug.toggle(wv)
    wv.view.page().runJavaScript.assert_called_once()
    arg = wv.view.page().runJavaScript.call_args[0][0]
    # Sandboxed-iframe overlay markers.
    assert "__qdb_reader_overlay" in arg
    assert "iframe" in arg
    assert "sandbox" in arg
