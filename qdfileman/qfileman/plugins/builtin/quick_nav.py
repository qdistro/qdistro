"""Quick navigation plugin for QFileMan.

Currently a stub — the plugin registers as a ``navigation_hook`` so it
appears in the plugin manager, but its hooks all return the defaults.
Useful as a scaffolding example for third-party NavigationHook plugins.
"""

from qfileman.plugin import NavigationHook


class QuickNavPlugin(NavigationHook):
    name = "quick_nav"
    description = "Quick navigation shortcuts (stub)"
    version = "1.0"

    def on_enter_directory(self, path):
        return True

    def on_leave_directory(self, path):
        return None

    def on_double_click(self, file_item):
        return False
