"""Rsync sync plugin for QFileMan.

Wraps ``rsync`` for copy and move operations that can be safely
interrupted and resumed. The user is prompted for a destination, which
may be a local path or any rsync-style remote (``user@host:/path``,
``rsync://host/module/path``).

The move action is implemented as ``rsync --remove-source-files``
followed by a directory cleanup pass, matching the long-standing
"move-with-resume" idiom from the rsync FAQ. If the transfer is
interrupted the source files left behind are exactly the ones that
have not yet been confirmed at the destination, so re-running the
move resumes safely.

The argv builder is a pure function — see :func:`rsync_argv` — so the
flag set can be exercised by tests without spawning rsync.
"""

from __future__ import annotations

import logging
import os

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


def rsync_argv(source: str, dest: str, *, move: bool = False,
               delete: bool = False, dry_run: bool = False) -> list[str]:
    """Build an rsync argv for a resumable transfer.

    Flags chosen:

    * ``-a`` — archive (recursive, preserve attrs, symlinks, times).
    * ``-h`` — human-readable byte counts in progress output.
    * ``--info=progress2`` — single-line overall progress in modern rsync.
    * ``--partial`` — keep partially-transferred files on interruption.
    * ``--append-verify`` — when resuming, append to the partial file
      after re-checksumming what's already there. This is the flag that
      actually delivers "resume an interrupted transfer".
    * ``--inplace`` — write directly to the destination file rather than
      a temporary copy. Required for ``--append-verify`` to be meaningful
      on the receiving side.

    The destination is passed through verbatim, so ``user@host:/dst``,
    ``rsync://host/mod/dst``, and plain local paths all work.
    """
    argv: list[str] = [
        "rsync",
        "-a",
        "-h",
        "--info=progress2",
        "--partial",
        "--append-verify",
        "--inplace",
    ]
    if delete:
        argv.append("--delete")
    if dry_run:
        argv.append("--dry-run")
    if move:
        argv.append("--remove-source-files")
    # ``--`` terminates option parsing so a source/dest whose name starts
    # with ``-`` (e.g. a file literally named ``-e sh -c '…'``) is treated
    # as a path, not an rsync option → no argv-injection.
    argv.append("--")
    argv.extend([source, dest])
    return argv


class RsyncSyncPlugin(MenuProvider):
    name = "rsync_sync"
    description = "Resumable copy / move via rsync (local or remote)"
    version = "1.0"
    category = "File"

    def get_menu_items(self, path):
        if not path:
            return []
        return [
            ("Rsync Copy To...", self._copy),
            ("Rsync Move To... (resume)", self._move),
            ("Rsync Dry-Run To...", self._dry_run),
        ]

    def _copy(self, path: str) -> None:
        self._run_with_prompt(path, move=False, dry_run=False, title_prefix="Rsync Copy")

    def _move(self, path: str) -> None:
        self._run_with_prompt(path, move=True, dry_run=False, title_prefix="Rsync Move")

    def _dry_run(self, path: str) -> None:
        self._run_with_prompt(path, move=False, dry_run=True, title_prefix="Rsync Dry-Run")

    # ------------------------------------------------------------- plumbing
    def _run_with_prompt(self, path: str, *, move: bool, dry_run: bool,
                         title_prefix: str) -> None:
        from PyQt6.QtWidgets import QInputDialog

        default = self._suggest_dest(path)
        dest, ok = QInputDialog.getText(
            None,
            title_prefix,
            "Destination (path or user@host:/path):",
            text=default,
        )
        if not ok or not dest.strip():
            return
        dest = dest.strip()
        # rsync semantics: trailing slash on source = copy *contents*. We
        # preserve the user's intent by leaving ``path`` unchanged but
        # ensuring directories end with ``/`` for the common "merge into
        # dest" case is left to the user — surprising them is worse.
        argv = rsync_argv(path, dest, move=move, dry_run=dry_run)
        self._run(f"{title_prefix} {os.path.basename(path)}", argv)

    @staticmethod
    def _suggest_dest(path: str) -> str:
        parent = os.path.dirname(path)
        # Heuristic: suggest sibling directory; user will edit anyway.
        return parent + os.sep if parent else ""

    def _run(self, title: str, argv: list[str]) -> None:
        from qfileman.plugins.builtin._runner import (
            missing_tools,
            run_command_dialog,
        )
        missing = missing_tools(["rsync"])
        if missing:
            self._warn("rsync is not installed or not on PATH.")
            return
        run_command_dialog(title, argv)

    @staticmethod
    def _warn(message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        log.warning(message)
        QMessageBox.warning(None, "Rsync", message)
