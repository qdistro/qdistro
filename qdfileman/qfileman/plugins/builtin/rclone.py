"""Rclone plugin for QFileMan.

Adds *Rclone Copy To…* and *Rclone Sync To…* for hauling files to any
backend ``rclone`` knows about (S3, Google Drive, Dropbox, B2,
OneDrive, WebDAV, SFTP, and so on). One plugin gives us every cloud
provider rclone supports — far cheaper than writing a per-provider
plugin.

The user is prompted to pick a configured remote (from
``rclone listremotes``) and then a destination path within it. The
two operations differ in safety:

* **copy** is additive — it never deletes files on the destination.
* **sync** mirrors the source — it WILL delete files on the
  destination that aren't in the source. We mark the menu entry as
  such and add a confirmation dialog before running.

We always pass ``--progress`` so :class:`CommandDialog` can show a
running percentage; rclone emits the same ``NN%`` token rsync does,
which :func:`_runner.parse_progress` already understands.

Argv builders are pure functions exposed for tests.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


def list_remotes() -> list[str]:
    """Return configured rclone remote names (no trailing colon)."""
    if not shutil.which("rclone"):
        return []
    try:
        out = subprocess.check_output(
            ["rclone", "listremotes"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("rclone listremotes failed: %s", e)
        return []
    return [line.rstrip(":") for line in out.splitlines() if line.strip()]


def rclone_argv(operation: str, source: str, dest: str,
                *, dry_run: bool = False) -> list[str]:
    """Build an rclone argv for ``copy`` or ``sync``.

    ``--progress`` so the runner's progress parser sees percentages;
    ``--stats=1s`` to keep those updates frequent enough to feel live;
    ``--transfers=4`` is rclone's own default, restated for clarity.
    """
    argv = [
        "rclone", operation,
        "--progress",
        "--stats=1s",
        "--transfers=4",
    ]
    if dry_run:
        argv.append("--dry-run")
    # ``--`` ends flag parsing (rclone uses pflag, which honors it) so a
    # leading-dash source/dest is a path, not an injected option.
    argv.append("--")
    argv.extend([source, dest])
    return argv


class RclonePlugin(MenuProvider):
    name = "rclone"
    description = "Copy / sync files to any rclone-configured backend"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path or not shutil.which("rclone"):
            return []
        return [
            ("Rclone Copy To…",       lambda p: self._send(p, "copy")),
            ("Rclone Sync To… (mirror)", lambda p: self._send(p, "sync")),
        ]

    def _send(self, path: str, operation: str) -> None:
        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        remotes = list_remotes()
        if not remotes:
            QMessageBox.warning(
                None, "Rclone",
                "No remotes are configured. Run `rclone config` first.",
            )
            return

        remote, ok = QInputDialog.getItem(
            None, "Rclone", "Remote:", remotes, 0, False,
        )
        if not ok:
            return

        suggested = f"{remote}:" + (path.lstrip("/") or "")
        dest, ok = QInputDialog.getText(
            None, "Rclone", "Destination (remote:path):", text=suggested,
        )
        if not ok or not dest.strip():
            return
        dest = dest.strip()

        if operation == "sync":
            answer = QMessageBox.question(
                None, "Confirm sync",
                "Sync MIRRORS the source — files on the destination that\n"
                "don't exist in the source will be deleted.\n\n"
                f"Run\n  {' '.join(rclone_argv(operation, path, dest))}\n"
                "anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        argv = rclone_argv(operation, path, dest)
        title = f"rclone {operation} → {dest}"
        from qfileman.plugins.builtin._runner import run_command_dialog
        run_command_dialog(title, argv)
