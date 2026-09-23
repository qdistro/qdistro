"""Trash plugin for QFileMan.

Adds *Move to Trash* — the XDG-compliant cousin of the existing
*Delete* action. Most of QFileMan's previous deletes are irrecoverable
``os.remove`` / ``shutil.rmtree`` calls, which is the single most
dangerous default in the current build. This plugin offers a path
that drops files into ``~/.local/share/Trash/`` so a misclick is
recoverable.

Backend priority:

1. ``gio trash`` — GLib's reference XDG-trash client; almost always
   present on a desktop Linux box (ships in ``glib2``).
2. ``trash-put`` — the ``trash-cli`` package's CLI; preferred when
   GLib is absent (Alpine, minimal containers).
3. ``kioclient5 move … trash:/`` — KDE fallback. Last resort because
   ``kioclient5`` isn't installed outside Plasma sessions.

If none of the three is available, the menu entry is hidden — better
that than offering a "Trash" button that silently fails.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


# Each entry: (binary, argv-builder taking the target path).
_BACKENDS = (
    ("gio",       lambda p: ["gio", "trash", "--", p]),
    ("trash-put", lambda p: ["trash-put", "--", p]),
    ("kioclient5", lambda p: ["kioclient5", "move", p, "trash:/"]),
)


def choose_backend() -> tuple[str, list] | None:
    """Return ``(name, argv-builder)`` for the first installed trash backend."""
    for name, builder in _BACKENDS:
        if shutil.which(name):
            return name, builder
    return None


def trash_argv(path: str) -> list[str] | None:
    """Build an argv for trashing ``path``. ``None`` if no backend installed."""
    backend = choose_backend()
    if backend is None:
        return None
    _name, builder = backend
    return builder(path)


class TrashPlugin(MenuProvider):
    name = "trash"
    description = "Move files to the system trash instead of permanent delete"
    version = "1.0"
    category = "File"

    def get_menu_items(self, path):
        if not path:
            return []
        if choose_backend() is None:
            return []
        return [("Move to Trash", self._trash)]

    def _trash(self, path: str) -> None:
        argv = trash_argv(path)
        if argv is None:
            return
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self._warn(f"Trash failed: {e}")
            return
        if result.returncode != 0:
            self._warn(
                f"Trash failed (exit {result.returncode}):\n"
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            return
        log.info("trashed: %s", path)

    @staticmethod
    def _warn(message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        log.warning(message)
        QMessageBox.warning(None, "Move to Trash", message)
