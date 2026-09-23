"""Vim-like modal keybinding plugin for QTerminator.

Adds a normal/insert mode layer on top of the terminal. In normal mode,
unmodified keys are intercepted as commands (navigation, splits, tabs,
etc.) instead of being sent to the shell. In insert mode (the default),
keys pass through normally.

Mode toggle:
    Ctrl+[   — enter normal mode (handled by event filter, not a window action)
    i        — (in normal mode) enter insert mode
    Esc      — (in normal mode) no-op

Normal-mode bindings mirror the usual Ctrl+Shift+... shortcuts but
without modifiers. See MODAL_BINDINGS below for the full list.
"""

from PyQt6.QtCore import QEvent, QObject, Qt

from qterminator.plugin import Plugin


class _ModalEventFilter(QObject):
    """Qt event filter that intercepts key presses while in normal mode."""

    def __init__(self, controller):
        super().__init__()
        self._controller = controller

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.KeyPress:
            return False
        # Always watch for the "enter normal mode" chord, even in insert mode:
        # Ctrl+[ (common vim escape-alternative).
        if not self._controller.is_normal_mode():
            mods = event.modifiers()
            if (event.key() == Qt.Key.Key_BracketLeft
                    and mods & Qt.KeyboardModifier.ControlModifier):
                self._controller.enter_normal()
                return True
            return False
        return self._controller.handle_key(event)


class _ModalController:
    """Per-window modal state and key dispatch."""

    def __init__(self, window):
        self.window = window
        self._normal_mode = False
        self._installed_terminals = set()
        self._filter = _ModalEventFilter(self)

        # Hook into new terminals as they get connected. The window calls
        # _connect_terminal(terminal) on each new terminal; wrap it.
        self._orig_connect_terminal = window._connect_terminal

        def wrapped_connect(terminal):
            self._orig_connect_terminal(terminal)
            self._install_on_terminal(terminal)

        window._connect_terminal = wrapped_connect

        # Install on any terminals that already exist.
        for i in range(window._tabs.count()):
            split = window._tabs.widget(i)
            for term in split.find_terminals():
                self._install_on_terminal(term)

        # Entry to normal mode is handled inside the event filter (Ctrl+[)
        # to avoid registering a window-level QAction that would collide
        # with the terminal's own key handling or with shortcut-coverage tests.

    # -- Installation --

    def _install_on_terminal(self, terminal):
        if terminal in self._installed_terminals:
            return
        self._installed_terminals.add(terminal)
        # QTermWidget's inner widget is where key presses arrive.
        terminal.term.installEventFilter(self._filter)

    def uninstall(self):
        for terminal in list(self._installed_terminals):
            try:
                terminal.term.removeEventFilter(self._filter)
            except RuntimeError:
                pass  # terminal already deleted
        self._installed_terminals.clear()
        # Restore original _connect_terminal
        if self._orig_connect_terminal:
            self.window._connect_terminal = self._orig_connect_terminal
        # Clear any mode prefix on titles
        if self._normal_mode:
            self._normal_mode = False
            self._refresh_titles()

    # -- Mode state --

    def is_normal_mode(self):
        return self._normal_mode

    def enter_normal(self):
        if self._normal_mode:
            return
        self._normal_mode = True
        self._refresh_titles()

    def enter_insert(self):
        if not self._normal_mode:
            return
        self._normal_mode = False
        self._refresh_titles()

    def _refresh_titles(self):
        """Add/remove the [N] prefix on every terminal titlebar."""
        for i in range(self.window._tabs.count()):
            split = self.window._tabs.widget(i)
            for term in split.find_terminals():
                base = term.title()
                prefix = "[N] " if self._normal_mode else ""
                # Use set_title on titlebar directly (bypasses term.title())
                term._titlebar.set_title(prefix + base)

    # -- Key dispatch --

    def handle_key(self, event):
        """Return True to swallow the event, False to pass through."""
        key = event.key()
        text = event.text()
        mods = event.modifiers()
        window = self.window

        # Don't intercept if any modifier (other than Shift/Keypad) is held —
        # those are OS shortcuts the user probably wants.
        interesting_mods = (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        )
        if mods & interesting_mods:
            return False

        # Escape: stay in normal mode, swallow so it doesn't hit shell.
        if key == Qt.Key.Key_Escape:
            return True

        # Digit keys switch to tab N.
        if Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            window._switch_to_tab(key - Qt.Key.Key_1)
            return True

        # Character bindings (case-sensitive).
        handler = MODAL_BINDINGS.get(text)
        if handler is None:
            # Unknown key: swallow to avoid accidentally typing into shell.
            return True
        handler(self, window)
        return True


# -- Bindings table -----------------------------------------------------------
# Each value is a function (controller, window) -> None.

MODAL_BINDINGS = {
    "i": lambda c, w: c.enter_insert(),

    # Navigation
    "h": lambda c, w: w._navigate("left"),
    "j": lambda c, w: w._navigate("down"),
    "k": lambda c, w: w._navigate("up"),
    "l": lambda c, w: w._navigate("right"),

    # Resize
    "H": lambda c, w: w._resize_split("left"),
    "J": lambda c, w: w._resize_split("down"),
    "K": lambda c, w: w._resize_split("up"),
    "L": lambda c, w: w._resize_split("right"),

    # Tabs / terminals
    "t": lambda c, w: w.new_tab(),
    "w": lambda c, w: w._close_active_terminal(),
    "s": lambda c, w: w._split_horizontal(),
    "v": lambda c, w: w._split_vertical(),
    "n": lambda c, w: w._next_tab(),
    "p": lambda c, w: w._prev_tab(),

    # View toggles
    "z": lambda c, w: w._toggle_zoom(),
    "f": lambda c, w: w._toggle_fullscreen(),
    "m": lambda c, w: w._toggle_menubar(),
    "/": lambda c, w: w._search(),
    "r": lambda c, w: w._rotate_splits(),
}


class ModalPlugin(Plugin):
    """Enables vim-style modal keybindings on a window."""

    name = "modal"
    description = "Vim-like modal keybindings (Normal/Insert mode)"
    version = "0.1"
    capabilities = ["modal"]

    def __init__(self):
        self._controllers = []

    def activate(self, app_controller):
        # app_controller is the MainWindow (see window._setup_plugins).
        if app_controller is None:
            return
        controller = _ModalController(app_controller)
        self._controllers.append(controller)

    def deactivate(self):
        for controller in self._controllers:
            controller.uninstall()
        self._controllers.clear()
