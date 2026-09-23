"""Per-tab page actions: zoom, mute, pin."""

from qdbrowser.plugin import CommandProvider


class PageActionsPlugin(CommandProvider):
    name = "page_actions"
    capabilities = ["command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None

    def activate(self, window):
        self._window = window

    def get_commands(self, window):
        wv = window._active_webview
        return [
            ("Zoom in", lambda: window._zoom_step(0.1)),
            ("Zoom out", lambda: window._zoom_step(-0.1)),
            ("Reset zoom", lambda: window._zoom_set(1.0)),
            ("Toggle mute current tab",
             lambda: wv.set_muted(not wv.muted) if wv else None),
            ("Toggle pin current tab",
             lambda: wv.set_pinned(not wv.pinned) if wv else None),
        ]
