"""alert_on_mark — notify when a marked command finishes.

Allows the user to "arm" an alert on a terminal tab. When the next
shell command completes (OSC 133 ;D event), a desktop notification
is fired. One-shot — clears automatically after firing.

Configuration (config.toml):
    [plugins.alert_on_mark]
    enabled = false          # default false (opt-in)
    sound = true             # play a sound with the notification

Trigger via:
- Context menu: "Alert me when this finishes"
- Keyboard shortcut: Ctrl+Shift+A
"""

import subprocess
from collections.abc import Callable

from PyQt6.QtGui import QKeySequence, QShortcut

from qterminator.config import Config
from qterminator.plugin import Plugin

# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AlertOnMarkService:
    """Manages armed alerts per terminal. Pure-Python — no Qt deps except
    the notification call, which gracefully degrades if notify-send
    is missing."""

    def __init__(self, window):
        self._window = window
        # terminal id -> whether an alert is armed
        self._armed: dict[int, bool] = {}

    def arm(self, terminal) -> bool:
        """Arm an alert for this terminal. Returns True if armed.

        Also ensures shell_integration is parsing this terminal — the
        OSC 133;D end-of-command event we listen for is only emitted
        once a parser is attached to that terminal's stream.
        """
        tid = id(terminal)
        self._armed[tid] = True
        shell_int = getattr(self._window, "shell_integration", None)
        if shell_int is not None:
            try:
                shell_int.ensure_attached(terminal)
            except Exception:
                pass
        return True

    def disarm(self, terminal) -> bool:
        """Explicitly disarm an alert for this terminal."""
        tid = id(terminal)
        self._armed[tid] = False
        return True

    def is_armed(self, terminal) -> bool:
        """Check if an alert is currently armed for this terminal."""
        return bool(self._armed.get(id(terminal), False))

    def _on_command_finished(self, terminal, record) -> None:
        """Called when a command finishes. If alert is armed, fire it."""
        tid = id(terminal)
        if not self._armed.get(tid, False):
            return
        # Fire the notification
        self._notify(terminal, record)
        # Disarm after firing (one-shot)
        self._armed[tid] = False

    def _notify(self, terminal, record) -> None:
        """Send a desktop notification."""
        # Build notification body
        exit_info = ""
        if record.exit_status is not None:
            if record.exit_status == 0:
                exit_info = " (completed successfully)"
            else:
                exit_info = f" (exit code: {record.exit_status})"

        cmd_preview = ""
        if record.text:
            # Truncate long commands
            cmd_text = record.text.strip()
            if len(cmd_text) > 50:
                cmd_text = cmd_text[:47] + "..."
            cmd_preview = f"\nLast command: {cmd_text}"

        body = f"Command finished in terminal{exit_info}{cmd_preview}"

        try:
            cmd = ['notify-send', 'QTerminator', body]
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class AlertOnMarkPlugin(Plugin):
    name = "alert_on_mark"
    description = (
        "Desktop notification when a marked command finishes. "
        "Arm via context menu or Ctrl+Shift+A."
    )
    version = "0.1"
    capabilities = ["menu_provider"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: AlertOnMarkService | None = None
        self._shell_int_sub: Callable | None = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = cfg.get("plugins", "alert_on_mark", "enabled", default=False)
        if not enabled:
            return

        self._window = app_controller
        self._service = AlertOnMarkService(app_controller)

        # Expose service on window
        if not hasattr(app_controller, "alert_on_mark"):
            app_controller.alert_on_mark = self._service

        # Subscribe to command finished events globally
        shell_int = getattr(app_controller, "shell_integration", None)
        if shell_int is not None:
            def global_callback(terminal, record):
                self._service._on_command_finished(terminal, record)
            shell_int.subscribe_command_finished(global_callback)
            self._shell_int_sub = global_callback

        # Set up keyboard shortcut for arming alert
        self._setup_shortcut(app_controller)

    def _setup_shortcut(self, app_controller):
        """Set up Ctrl+Shift+A to arm alert on focused terminal."""
        try:
            shortcut = QShortcut(
                QKeySequence("Ctrl+Shift+A"),
                app_controller,
            )
            shortcut.activated.connect(lambda: self._arm_focused(app_controller))
            self._shortcut = shortcut
        except Exception:
            self._shortcut = None

    def _arm_focused(self, app_controller):
        """Arm alert on the currently focused terminal."""
        if self._service is None:
            return

        # ``currentWidget`` lives on the QTabWidget at MainWindow._tabs,
        # not on MainWindow itself — calling it on the window raises
        # AttributeError and the shortcut becomes a crash trigger.
        tabs = getattr(app_controller, "_tabs", None)
        if tabs is None:
            return

        focused = tabs.currentWidget()
        if focused is None:
            return

        # Find terminal(s) in the focused widget
        for term in focused.find_terminals():
            self._service.arm(term)
            break  # only arm one terminal

    def deactivate(self):
        # Unsubscribe from shell_integration
        if self._shell_int_sub is not None:
            shell_int = getattr(self._window, "shell_integration", None)
            if shell_int is not None:
                try:
                    shell_int.unsubscribe_command_finished(self._shell_int_sub)
                except Exception:
                    pass
            self._shell_int_sub = None

        # Remove service from window
        if (self._window is not None
                and getattr(self._window, "alert_on_mark", None) is self._service):
            try:
                del self._window.alert_on_mark
            except AttributeError:
                pass

        # Clean up shortcut
        if hasattr(self, '_shortcut') and self._shortcut is not None:
            try:
                self._shortcut.deleteLater()
            except Exception:
                pass
            self._shortcut = None

        self._service = None
        self._window = None
