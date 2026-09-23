"""Folder-synchronizer plugin for QFileMan.

Adds *Sync Folders…* — a Krusader Synchronizer-style workflow:

1. The user picks two directories (left, right).
2. We run ``rsync --dry-run --itemize-changes`` left → right to
   collect a per-file action list.
3. A dialog shows the list; the user can review and either click
   *Apply* to run rsync for real, or *Cancel* to back out.

Compared with the existing ``rsync_sync`` plugin, this one:

* Targets a *folder vs folder* compare rather than a single
  file/dir copy.
* Surfaces the dry-run diff before doing anything, which is the part
  Krusader users actually rely on.

The :func:`parse_itemize` and :func:`describe_change` functions
implement the itemize-changes wire format (``YXcstpoguax path``) and
are tested independently so the parsing stays robust as rsync's
output evolves.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


# Rsync itemize-changes prefix decoded (see rsync(1) "INCLUSION/EXCLUSION"):
#   <  remote → local (we never emit this; we're always pushing)
#   >  local → remote: file being updated on the destination
#   c  local change/creation (directory, symlink, devnode, ...)
#   h  hard link
#   .  no change to the destination beyond attributes
#   *  message (typically "deleting") in column 1
def describe_change(prefix: str) -> str:
    """Return a one-word verb for an itemize-changes prefix string."""
    if not prefix:
        return "noop"
    head = prefix[0]
    if head == "*":
        return "delete"
    if head == ">":
        if len(prefix) > 1 and prefix[1] == "f":
            # If the rest is all dots/+ it's a NEW file; otherwise update.
            rest = prefix[2:]
            if "+" in rest or set(rest) == {"+"}:
                return "new"
            return "update"
        return "update"
    if head == "c":
        # Created (directory, symlink, etc).
        return "create"
    if head == "h":
        return "link"
    if head == ".":
        # Attrs-only changes are usually not interesting; the user
        # still wants to see them so we name them explicitly.
        return "attr"
    return prefix[:2]


def parse_itemize(stdout: str) -> list[tuple[str, str]]:
    """Parse ``rsync --itemize-changes`` output into ``(action, path)`` pairs.

    Skips blank lines, summary lines like "sent 12 bytes ...", and the
    final stats footer. The format is always ``XXXXXXXXXXX path`` where
    the first whitespace-separated token is the change marker and the
    rest is the file path.
    """
    out: list[tuple[str, str]] = []
    for raw in stdout.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        # Stats footer lines start with words, not change-marker chars.
        if line[:1].isalpha() and "->" not in line and not line.startswith("c"):
            # ``sent``, ``total``, ``speedup``... — drop them.
            continue
        token, _, path = line.partition(" ")
        path = path.strip()
        if not path or len(token) < 2:
            continue
        # Reject anything that doesn't look like an itemize prefix —
        # the first char must be one of the small known set.
        if token[0] not in "<>.ch*":
            continue
        out.append((describe_change(token), path))
    return out


def dry_run_argv(src: str, dst: str) -> list[str]:
    """Return the rsync argv we'd run for a folder sync dry-run."""
    return [
        "rsync", "-a", "--delete", "--itemize-changes", "--dry-run",
        "--", _trailing_slash(src), _trailing_slash(dst),
    ]


def apply_argv(src: str, dst: str) -> list[str]:
    """Return the rsync argv that actually performs the sync."""
    return [
        "rsync", "-a", "--delete",
        "--partial", "--append-verify", "--inplace",
        "--info=progress2",
        "--", _trailing_slash(src), _trailing_slash(dst),
    ]


def _trailing_slash(path: str) -> str:
    """Ensure ``path`` ends with ``/`` — required for rsync to copy *contents*."""
    return path if path.endswith("/") else path + "/"


class SyncFoldersPlugin(MenuProvider):
    name = "sync_folders"
    description = "Two-pane folder synchronizer with rsync diff preview"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path or not shutil.which("rsync"):
            return []
        if not os.path.isdir(path):
            return []
        return [("Sync Folders…", self._show)]

    def _show(self, path: str) -> None:
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMessageBox,
            QPushButton,
            QTreeWidget,
            QTreeWidgetItem,
            QVBoxLayout,
        )

        dlg = QDialog()
        dlg.setWindowTitle("Sync Folders")
        dlg.resize(800, 600)
        layout = QVBoxLayout(dlg)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source:", dlg))
        src_edit = QLineEdit(path, dlg)
        src_pick = QPushButton("…", dlg)
        src_row.addWidget(src_edit, 1)
        src_row.addWidget(src_pick)
        layout.addLayout(src_row)

        dst_row = QHBoxLayout()
        dst_row.addWidget(QLabel("Destination:", dlg))
        dst_edit = QLineEdit("", dlg)
        dst_pick = QPushButton("…", dlg)
        dst_row.addWidget(dst_edit, 1)
        dst_row.addWidget(dst_pick)
        layout.addLayout(dst_row)

        diff_btn = QPushButton("Compare", dlg)
        layout.addWidget(diff_btn)

        results = QTreeWidget(dlg)
        results.setHeaderLabels(["Action", "Path"])
        results.setRootIsDecorated(False)
        layout.addWidget(results, 1)

        def pick_dir(target: QLineEdit) -> None:
            chosen = QFileDialog.getExistingDirectory(
                dlg, "Pick directory", target.text() or os.getcwd(),
            )
            if chosen:
                target.setText(chosen)

        src_pick.clicked.connect(lambda: pick_dir(src_edit))
        dst_pick.clicked.connect(lambda: pick_dir(dst_edit))

        def do_compare() -> None:
            src = src_edit.text().strip()
            dst = dst_edit.text().strip()
            if not src or not dst:
                QMessageBox.warning(dlg, "Sync Folders",
                                     "Both source and destination are required.")
                return
            try:
                result = subprocess.run(
                    dry_run_argv(src, dst),
                    capture_output=True, text=True, timeout=300,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                QMessageBox.warning(dlg, "Sync Folders", f"rsync failed: {e}")
                return
            if result.returncode != 0 and not result.stdout:
                QMessageBox.warning(
                    dlg, "Sync Folders",
                    f"rsync exit {result.returncode}:\n{result.stderr}",
                )
                return
            results.clear()
            changes = parse_itemize(result.stdout)
            for action, rel in changes:
                results.addTopLevelItem(QTreeWidgetItem([action, rel]))
            if not changes:
                results.addTopLevelItem(QTreeWidgetItem(["", "(already in sync)"]))

        diff_btn.clicked.connect(do_compare)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close,
            parent=dlg,
        )
        apply_btn = buttons.button(QDialogButtonBox.StandardButton.Apply)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        def do_apply() -> None:
            src = src_edit.text().strip()
            dst = dst_edit.text().strip()
            if not src or not dst:
                return
            answer = QMessageBox.question(
                dlg, "Confirm sync",
                f"This will mirror\n  {src}\nonto\n  {dst}\n"
                "Files on the destination that don't exist in the source\n"
                "will be DELETED.\n\nProceed?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            from qfileman.plugins.builtin._runner import run_command_dialog
            run_command_dialog(
                f"Sync {os.path.basename(src.rstrip('/'))} → {dst}",
                apply_argv(src, dst),
            )

        apply_btn.clicked.connect(do_apply)
        dlg.exec()
