"""Open-terminal-here plugin for QFileMan.

Adds *Open Terminal Here* to the context menu. Resolution order for
the terminal is:

1. If QTerminator's agent_control socket is reachable, ask it to open
   a new tab with the right working directory. Keeps the user inside
   their existing QTerminator window instead of spawning a sibling.
2. ``$TERMINAL`` if set.
3. The first installed entry from a small priority list of common
   terminal emulators.

Each candidate is invoked with the ``--working-directory`` flag (or
its equivalent) where supported, falling back to ``-e bash`` with
``cd`` for the few terminals that don't take a cwd flag.

:func:`build_argv` is a pure function — given a terminal name and a
directory it returns the argv to spawn — so the dispatch table can
be exercised in tests without launching anything.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


# Each entry: ``cmd -> (argv builder)``. The builder receives the
# absolute working directory and returns a complete argv. We keep this
# table small and ordered by how commonly the binary actually launches
# something useful (foot/alacritty/kitty are popular on Wayland;
# konsole/gnome-terminal on KDE/GNOME; xterm last as the universal
# fallback). xfce4-terminal and lxterminal are included for the
# desktop environments that ship them by default.
TERMINAL_BUILDERS = {
    "alacritty":      lambda cwd: ["alacritty", "--working-directory", cwd],
    "foot":           lambda cwd: ["foot", "--working-directory=" + cwd],
    "kitty":          lambda cwd: ["kitty", "--directory", cwd],
    "wezterm":        lambda cwd: ["wezterm", "start", "--cwd", cwd],
    "konsole":        lambda cwd: ["konsole", "--workdir", cwd],
    "gnome-terminal": lambda cwd: ["gnome-terminal", "--working-directory", cwd],
    "xfce4-terminal": lambda cwd: ["xfce4-terminal", "--working-directory", cwd],
    "lxterminal":     lambda cwd: ["lxterminal", "--working-directory", cwd],
    "tilix":          lambda cwd: ["tilix", "--working-directory", cwd],
    "terminator":     lambda cwd: ["terminator", "--working-directory", cwd],
    "qterminator":    lambda cwd: ["qterminator", "--working-directory", cwd],
    # xterm has no cwd flag — exec a shell that's already in the right place.
    "xterm":          lambda cwd: ["xterm", "-e",
                                   "sh", "-c", f"cd {_sh_quote(cwd)}; exec $SHELL"],
}


# Ordered preference list when ``$TERMINAL`` is unset.
TERMINAL_PRIORITY = (
    "alacritty", "foot", "kitty", "wezterm",
    "konsole", "gnome-terminal", "xfce4-terminal", "lxterminal",
    "tilix", "terminator", "qterminator",
    "xterm",
)


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def build_argv(terminal: str, cwd: str) -> list[str] | None:
    """Return an argv for ``terminal`` opened in ``cwd``. None if unknown."""
    builder = TERMINAL_BUILDERS.get(terminal)
    if builder is None:
        return None
    return builder(cwd)


def detect_terminal() -> str | None:
    """Return the best available terminal name, honouring ``$TERMINAL``."""
    env_term = os.environ.get("TERMINAL")
    if env_term:
        base = os.path.basename(env_term)
        if shutil.which(env_term):
            return base
    for name in TERMINAL_PRIORITY:
        if shutil.which(name):
            return name
    return None


class OpenTerminalPlugin(MenuProvider):
    name = "open_terminal"
    description = "Open a terminal emulator in the current directory"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path:
            return []
        return [("Open Terminal Here", self._launch)]

    def _launch(self, path: str) -> None:
        cwd = path if os.path.isdir(path) else os.path.dirname(path) or os.getcwd()

        # 1) Try QTerminator's agent_control first.
        if self._launch_via_qterminator(cwd):
            return

        # 2) Fall back to whatever terminal we can find.
        terminal = detect_terminal()
        if terminal is None:
            self._warn("No terminal emulator found on PATH.")
            return
        argv = build_argv(terminal, cwd)
        if argv is None:
            self._warn(f"Unknown terminal: {terminal}")
            return
        try:
            subprocess.Popen(argv)
        except OSError as e:
            self._warn(f"Failed to launch {terminal}: {e}")

    @staticmethod
    def _launch_via_qterminator(cwd: str) -> bool:
        """Open a tab in a running QTerminator. Returns True on success."""
        try:
            from qfileman.plugins.builtin import _qterminator
        except Exception as e:
            log.debug("qterminator helper unavailable: %s", e)
            return False
        if not _qterminator.is_available():
            return False
        try:
            _qterminator.open_tab(working_directory=cwd)
            return True
        except _qterminator.QTerminatorUnavailable:
            return False
        except Exception as e:
            log.debug("qterminator open_tab failed: %s", e)
            return False

    @staticmethod
    def _warn(message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        log.warning(message)
        QMessageBox.warning(None, "Open Terminal", message)
