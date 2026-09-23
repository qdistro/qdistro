"""hotkey_window — quake-style dropdown terminal.

Spawns an extra ``MainWindow`` at the top of the screen on demand,
shown/hidden by a configurable shortcut.

Caveats:
- The shortcut is a plain ``QShortcut``, so it only fires when a
  qterminator window has keyboard focus. A true global hotkey would
  need KGlobalAccel (KDE) or compositor-specific glue on Wayland;
  that work isn't in this plugin.
- ``hide_on_focus_lost`` is read from config but not enforced —
  QMainWindow exposes no ``windowStateChanged`` signal to hang it
  off without a custom event filter, which the current scope skips.

Configuration (config.toml):
    [plugins.hotkey_window]
    enabled = false              # default false (opt-in)
    shortcut = "Ctrl+Shift+D"
    height = 400                 # default 400 pixels
    width_percent = 80           # width as percent of screen
    hide_on_focus_lost = true    # currently advisory only
"""

import sys

from PyQt6.QtGui import QShortcut

from qterminator.config import Config
from qterminator.plugin import Plugin


class HotkeyWindowPlugin(Plugin):
    name = "hotkey_window"
    description = (
        "Quake-style dropdown terminal window. "
        "Use --dropdown CLI flag or shortcut to toggle."
    )
    version = "0.1"
    capabilities = []

    def __init__(self):
        super().__init__()
        self._window = None
        self._dropdown_window: object | None = None
        self._shortcut = None

    #: Class-level flag set by ``_create_dropdown_window`` so the
    #: nested ``MainWindow`` it spawns does not load this plugin and
    #: recursively create another dropdown.
    _spawning_dropdown = False

    def activate(self, app_controller):
        if HotkeyWindowPlugin._spawning_dropdown:
            return
        cfg = Config()
        enabled = cfg.get("plugins", "hotkey_window", "enabled", default=False)
        if not enabled:
            return

        self._window = app_controller

        # Set up shortcut to toggle (Ctrl+Shift+D by default)
        self._setup_shortcut(app_controller)

        # Check if we should start in dropdown mode (via CLI argument)
        if self._should_start_dropdown():
            self._create_dropdown_window()

    def _should_start_dropdown(self) -> bool:
        """Check if --dropdown or --quake was passed on command line."""
        # Check sys.argv for dropdown flags
        args = sys.argv
        return "--dropdown" in args or "--quake" in args or "-q" in args

    def _setup_shortcut(self, app_controller):
        """Set up shortcut to toggle dropdown window."""
        try:
            from PyQt6.QtGui import QKeySequence

            cfg = Config()
            shortcut_str = cfg.get(
                "plugins", "hotkey_window", "shortcut",
                default="Ctrl+Shift+D"
            )

            # Use Qt's built-in parser
            seq = QKeySequence.fromString(shortcut_str)
            if seq.isEmpty():
                seq = QKeySequence("Ctrl+Shift+D")

            self._shortcut = QShortcut(seq, app_controller)
            self._shortcut.activated.connect(lambda: self.toggle())
        except Exception:
            self._shortcut = None

    def toggle(self):
        """Toggle the dropdown window visibility.

        The "live but hidden" branch must also re-show the window:
        the user may have closed it (X button) or another dropdown
        toggle hid it. We probe :py:meth:`QWidget.isVisible` rather
        than just a truthy reference, so the second hotkey press
        reliably resurfaces the dropdown.
        """
        if self._dropdown_window is None:
            self._create_dropdown_window()
            return
        # The window may have been deleted underneath us (user clicked
        # the X, application teardown). Touch a cheap method first to
        # detect the dead-C++ case; on RuntimeError, recreate.
        try:
            visible = self._dropdown_window.isVisible()
        except RuntimeError:
            self._dropdown_window = None
            self._create_dropdown_window()
            return
        try:
            if visible:
                self._hide_dropdown()
            else:
                self._dropdown_window.show()
                self._dropdown_window.raise_()
                self._dropdown_window.activateWindow()
        except RuntimeError:
            self._dropdown_window = None
            self._create_dropdown_window()

    def _create_dropdown_window(self):
        """Create a new dropdown/floating window."""
        try:
            from PyQt6.QtWidgets import QApplication

            from qterminator.window import MainWindow
        except ImportError:
            return

        screen = QApplication.primaryScreen()
        if screen is None:
            return

        screen_geo = screen.geometry()
        cfg = Config()

        width_percent = cfg.get(
            "plugins", "hotkey_window", "width_percent", default=80
        )
        width = int(screen_geo.width() * width_percent / 100)
        height = cfg.get(
            "plugins", "hotkey_window", "height", default=400
        )

        # Mark the next MainWindow as a dropdown spawn so its plugin
        # manager skips hotkey_window — otherwise the dropdown spawns
        # another dropdown spawns another dropdown, recursively.
        try:
            HotkeyWindowPlugin._spawning_dropdown = True
            self._dropdown_window = MainWindow()
        except Exception:
            HotkeyWindowPlugin._spawning_dropdown = False
            return
        finally:
            HotkeyWindowPlugin._spawning_dropdown = False

        x = (screen_geo.width() - width) // 2
        y = 0
        self._dropdown_window.setGeometry(x, y, width, height)
        self._dropdown_window.show()
        self._dropdown_window.raise_()
        try:
            self._dropdown_window.activateWindow()
        except RuntimeError:
            pass

    def _hide_dropdown(self):
        """Hide the dropdown window. Safe against a deleted C++ side."""
        if self._dropdown_window is None:
            return
        try:
            self._dropdown_window.hide()
        except RuntimeError:
            self._dropdown_window = None

    def show(self):
        """Show the dropdown window."""
        if self._dropdown_window is None:
            self._create_dropdown_window()
        else:
            try:
                self._dropdown_window.show()
                self._dropdown_window.raise_()
            except RuntimeError:
                self._dropdown_window = None
                self._create_dropdown_window()

    def hide(self):
        """Hide the dropdown window."""
        self._hide_dropdown()

    def deactivate(self):
        if self._dropdown_window is not None:
            try:
                self._dropdown_window.close()
            except Exception:
                pass
            self._dropdown_window = None

        if self._shortcut is not None:
            try:
                self._shortcut.deleteLater()
            except Exception:
                pass
            self._shortcut = None

        self._window = None


def parse_cli_args():
    """Return True if any dropdown-mode CLI flag was passed.

    Helper for callers that want to gate dropdown behaviour on a
    command-line flag. Kept as a free function so it can be probed
    without instantiating the plugin.
    """
    return "--dropdown" in sys.argv or "--quake" in sys.argv or "-q" in sys.argv
