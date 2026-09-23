"""Tmux integration plugin — start shells inside tmux, reattach sessions.

When enabled, new terminals start inside a tmux session instead of a bare
shell. If the tmux session already exists (e.g., after a crash or restart),
it reattaches instead of creating a new one.

This gives you persistent sessions that survive terminal restarts:
- Close QTerminator, reopen it, and your processes are still running
- Each tab gets its own tmux session named qterminator-<N>

Configuration (config.toml):
    [plugins.tmux]
    enabled = true
    session_prefix = "qterminator"
    # If true, detach on close instead of killing the session
    detach_on_close = true
"""

import shutil
import subprocess

from qterminator.config import Config
from qterminator.plugin import MenuProvider, Plugin


def _tmux_available():
    """Check if tmux is installed."""
    return shutil.which("tmux") is not None


def _tmux_session_exists(name):
    """Check if a tmux session with this name exists."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _tmux_list_sessions():
    """List all tmux sessions."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return [s.strip() for s in result.stdout.strip().split('\n') if s.strip()]
    except FileNotFoundError:
        pass
    return []


class TmuxSessionPlugin(Plugin):
    """Manages tmux sessions for persistent terminal sessions.

    Call get_shell_command() to get the command that should be run
    in a new terminal pane instead of the bare shell.
    """

    name = "tmux_integration"
    description = "Start terminals inside tmux for persistent sessions"
    version = "1.0"
    capabilities = ["tmux"]

    def __init__(self):
        super().__init__()
        self._prefix = "qterminator"
        self._detach_on_close = True
        self._session_counter = 0
        self._enabled = False

    def activate(self, app_controller):
        config = Config()
        self._enabled = config.get(
            "plugins", "tmux", "enabled", default=False,
        )
        self._prefix = config.get(
            "plugins", "tmux", "session_prefix", default="qterminator",
        )
        self._detach_on_close = config.get(
            "plugins", "tmux", "detach_on_close", default=True,
        )

    def is_enabled(self):
        return self._enabled and _tmux_available()

    def get_shell_command(self, session_name=None):
        """Get the tmux command to run in a new terminal.

        Returns (program, args) tuple suitable for QTermWidget.
        If session exists, reattaches; otherwise creates new.
        """
        if not self.is_enabled():
            return None

        if session_name is None:
            self._session_counter += 1
            session_name = f"{self._prefix}-{self._session_counter}"

        if _tmux_session_exists(session_name):
            # Reattach to existing session
            return ("tmux", ["tmux", "attach-session", "-t", session_name])
        else:
            # Create new session
            return ("tmux", ["tmux", "new-session", "-s", session_name])


class TmuxMenuPlugin(MenuProvider):
    """Context menu items for tmux session management."""

    name = "tmux_menu"
    description = "Tmux session management in context menu"
    version = "1.0"
    category = "Workspace"
    capabilities = ["menu_provider"]

    def get_menu_items(self, terminal):
        if not _tmux_available():
            return []

        items = []

        sessions = _tmux_list_sessions()
        if sessions:
            items.append((
                f"Tmux Sessions ({len(sessions)})",
                lambda: self._show_sessions(terminal),
            ))
            for session in sessions[:10]:  # limit to 10
                items.append((
                    f"  Attach: {session}",
                    lambda s=session: terminal.send_text(f"tmux attach -t {s}\n"),
                ))
        else:
            items.append((
                "New Tmux Session",
                lambda: terminal.send_text("tmux new-session\n"),
            ))

        items.append((
            "Detach Current Tmux",
            lambda: terminal.send_text("tmux detach\n"),
        ))

        return items

    def _show_sessions(self, terminal):
        """Show tmux sessions in a message box."""
        from PyQt6.QtWidgets import QMessageBox
        sessions = _tmux_list_sessions()
        if sessions:
            msg = "Active tmux sessions:\n\n" + "\n".join(f"  {s}" for s in sessions)
        else:
            msg = "No active tmux sessions."
        QMessageBox.information(terminal, "Tmux Sessions", msg)
