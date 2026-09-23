"""undo_close — restore recently closed tabs.

iTerm2's Cmd+Z equivalent: restore a recently-closed tab within a
configurable time window. Only restores the working directory and
profile - the shell process is new each time.

Configuration (config.toml):
    [plugins.undo_close]
    enabled = false           # default false; opt-in
    window_seconds = 5        # default 5 seconds
    max_remembered = 10       # default 10 tabs
    shortcut = "Ctrl+Alt+T"   # default — Ctrl+Shift+T is already
                              # taken by new_tab on qterminator
"""

import time
from dataclasses import dataclass

from PyQt6.QtGui import QKeySequence, QShortcut

from qterminator.config import Config
from qterminator.plugin import Plugin

# ---------------------------------------------------------------------------
# Closed tab record
# ---------------------------------------------------------------------------

@dataclass
class ClosedTab:
    """Record of a closed tab that can be restored."""
    working_directory: str
    profile: str
    tab_name: str
    shell_command: list[str] | None = None
    closed_at: float = 0.0


# ---------------------------------------------------------------------------
# Undo close service
# ---------------------------------------------------------------------------

class UndoCloseService:
    """Manages the ring buffer of closed tabs."""

    def __init__(self, window, max_remembered: int = 10, window_seconds: float = 5.0):
        self._window = window
        self._max_remembered = max_remembered
        self._window_seconds = window_seconds
        self._closed_tabs: list[ClosedTab] = []

    def record_close(self, working_directory: str, profile: str = "default",
                     tab_name: str = "Terminal", shell_command: list[str] | None = None):
        """Record a tab close so it can be undone."""
        # Remove expired entries
        self._cleanup()

        # Add new closed tab
        record = ClosedTab(
            working_directory=working_directory,
            profile=profile,
            tab_name=tab_name,
            shell_command=shell_command,
            closed_at=time.monotonic(),
        )

        self._closed_tabs.append(record)

        # Trim to max
        while len(self._closed_tabs) > self._max_remembered:
            self._closed_tabs.pop(0)

    def undo(self) -> bool:
        """Restore the most recently closed tab. Returns True if restored."""
        self._cleanup()

        if not self._closed_tabs:
            return False

        # Get the most recent
        record = self._closed_tabs.pop()

        # Re-spawn with the saved cwd and shell argv if we have one.
        # We can't restore the closed shell's state — just put the
        # user back at the same prompt location.
        try:
            kwargs = {"working_directory": record.working_directory}
            if record.shell_command:
                kwargs["shell_command"] = record.shell_command
            new_term = self._window.new_tab(**kwargs)

            # ``new_tab`` returns the new terminal so we can re-apply
            # the saved profile. Older callers may still return None;
            # fall back to the active terminal in that case.
            if new_term is None:
                new_term = getattr(self._window, "_active_terminal", None)

            if new_term is not None and record.profile and record.profile != "default":
                try:
                    new_term.apply_profile(record.profile)
                except Exception:
                    pass

            return True
        except Exception:
            return False

    def _cleanup(self):
        """Remove expired entries. Uses monotonic time so the window
        survives wall-clock jumps (NTP slew, suspend/resume)."""
        now = time.monotonic()
        self._closed_tabs = [
            t for t in self._closed_tabs
            if now - t.closed_at <= self._window_seconds
        ]

    @property
    def can_undo(self) -> bool:
        """Check if there's anything to undo."""
        self._cleanup()
        return len(self._closed_tabs) > 0

    @property
    def pending_count(self) -> int:
        """Number of tabs that can be restored."""
        self._cleanup()
        return len(self._closed_tabs)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class UndoClosePlugin(Plugin):
    name = "undo_close"
    description = (
        "Restore recently closed tabs with Ctrl+Alt+T. "
        "Remembers working directory and profile."
    )
    version = "0.1"
    capabilities = []

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: UndoCloseService | None = None
        self._shortcut = None
        self._original_close = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = cfg.get("plugins", "undo_close", "enabled", default=False)
        if not enabled:
            return

        self._window = app_controller

        # Get config
        max_remembered = cfg.get("plugins", "undo_close", "max_remembered", default=10)
        window_seconds = cfg.get("plugins", "undo_close", "window_seconds", default=5.0)

        # Create service
        self._service = UndoCloseService(app_controller, max_remembered, window_seconds)

        # Install service on window
        if not hasattr(app_controller, "undo_close"):
            app_controller.undo_close = self._service

        # Set up shortcut
        self._setup_shortcut(app_controller)

        # Hook into tab close
        self._hook_tab_close(app_controller)

    def _setup_shortcut(self, app_controller):
        """Set up the shortcut to undo close."""
        try:
            cfg = Config()
            shortcut_str = cfg.get("plugins", "undo_close", "shortcut",
                                  default="Ctrl+Alt+T")

            # Use Qt's built-in parser; fall back to the documented
            # default if the user typed something Qt couldn't parse.
            seq = QKeySequence.fromString(shortcut_str)
            if seq.isEmpty():
                seq = QKeySequence("Ctrl+Alt+T")

            self._shortcut = QShortcut(seq, app_controller)
            self._shortcut.activated.connect(lambda: self._undo())
        except Exception:
            self._shortcut = None

    def _hook_tab_close(self, app_controller):
        """Hook into the tab close process to record closes.

        We wrap ``MainWindow._on_tab_close_requested`` rather than
        subscribing to ``tabCloseRequested``: MainWindow connects to
        the signal during ``_setup_tabs`` (window.py:145), so any
        slot the plugin connects later runs AFTER MainWindow's slot
        has already removed the tab — the widget would be gone before
        we could read cwd/profile from it. Wrapping lets us snapshot
        before, then check after whether removal actually happened
        (the user can cancel via the running-process dialog).
        """
        orig = getattr(app_controller, "_on_tab_close_requested", None)
        if orig is None:
            return
        self._original_close = orig
        tabs = getattr(app_controller, "_tabs", None)
        if tabs is None:
            return
        plugin = self

        def wrapped(index, _orig=orig, _tabs=tabs):
            split = None
            tab_name = ""
            try:
                split = _tabs.widget(index)
                tab_name = _tabs.tabText(index)
            except Exception:
                pass
            cwd = ""
            profile = "default"
            shell_argv = None
            if split is not None and hasattr(split, "find_terminals"):
                terminals = split.find_terminals()
                if terminals:
                    term = terminals[0]
                    try:
                        cwd = term.working_directory() or ""
                    except Exception:
                        pass
                    try:
                        profile = term._profile_name or "default"
                    except Exception:
                        pass
                    shell_argv = getattr(term, "_shell_command", None)

            _orig(index)

            # If the split is still parented to the tab widget the
            # user cancelled (running-process prompt → No) — skip
            # recording so undo doesn't resurrect a tab that's still
            # open.
            if split is not None:
                try:
                    if _tabs.indexOf(split) != -1:
                        return
                except Exception:
                    pass

            if plugin._service is not None and split is not None:
                plugin._service.record_close(
                    working_directory=cwd,
                    profile=profile,
                    tab_name=tab_name,
                    shell_command=list(shell_argv) if shell_argv else None,
                )

        app_controller._on_tab_close_requested = wrapped

    def _undo(self):
        """Perform the undo action."""
        if self._service is None:
            return

        if self._service.can_undo:
            self._service.undo()
            # Could show feedback if needed

    def deactivate(self):
        if self._original_close is not None and self._window is not None:
            try:
                self._window._on_tab_close_requested = self._original_close
            except AttributeError:
                pass
        self._original_close = None

        if (self._window is not None
                and getattr(self._window, "undo_close", None) is self._service):
            try:
                del self._window.undo_close
            except AttributeError:
                pass

        if self._shortcut is not None:
            try:
                self._shortcut.deleteLater()
            except Exception:
                pass
            self._shortcut = None

        self._service = None
        self._window = None
