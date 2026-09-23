"""Page actions plugin."""


def test_provides_zoom_commands(window):
    plug = window.plugins._instances["page_actions"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("Zoom in" in label for label in labels)
    assert any("Zoom out" in label for label in labels)
    assert any("Reset zoom" in label for label in labels)


def test_provides_mute_and_pin(window):
    plug = window.plugins._instances["page_actions"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("mute" in label.lower() for label in labels)
    assert any("pin" in label.lower() for label in labels)


def test_zoom_in_callback_changes_zoom(window):
    plug = window.plugins._instances["page_actions"]
    cmds = dict(plug.get_commands(window))
    before = window._active_webview.zoom()
    cmds["Zoom in"]()
    assert window._active_webview.zoom() > before


def test_zoom_out_callback_changes_zoom(window):
    plug = window.plugins._instances["page_actions"]
    cmds = dict(plug.get_commands(window))
    window._active_webview.set_zoom(1.5)
    cmds["Zoom out"]()
    assert window._active_webview.zoom() < 1.5


def test_reset_zoom_callback(window):
    plug = window.plugins._instances["page_actions"]
    cmds = dict(plug.get_commands(window))
    window._active_webview.set_zoom(2.0)
    cmds["Reset zoom"]()
    assert window._active_webview.zoom() == 1.0
