"""Picture-in-picture plugin: JS shape + command/shortcut wiring."""

from unittest.mock import MagicMock


def test_pip_js_contains_request_call():
    from qdbrowser.plugins.picture_in_picture import PIP_JS
    assert "requestPictureInPicture" in PIP_JS
    assert "exitPictureInPicture" in PIP_JS


def test_plugin_commands(window):
    plug = window.plugins._instances["picture_in_picture"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("Picture-in-Picture" in label for label in labels)


def test_toggle_calls_runjs(window):
    plug = window.plugins._instances["picture_in_picture"]
    wv = window._active_webview
    wv.view.page().runJavaScript = MagicMock()
    plug.toggle_pip()
    wv.view.page().runJavaScript.assert_called_once()


def test_toggle_safe_without_active_view(window):
    plug = window.plugins._instances["picture_in_picture"]
    window._active_webview = None
    plug.toggle_pip()  # must not raise


def test_shortcut_registered(window):
    sc = {a.shortcut().toString(): a for a in window.actions()
          if a.shortcut().toString()}
    assert "Ctrl+Shift+V" in sc


def test_pip_js_handles_no_video():
    from qdbrowser.plugins.picture_in_picture import PIP_JS
    assert "no_video_found" in PIP_JS
