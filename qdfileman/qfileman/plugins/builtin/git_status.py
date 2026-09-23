"""Git plugin for QFileMan.

Adds Git actions to files and directories that live inside a Git
working tree. The plugin doesn't decorate the file list with status
badges — that would need a new ``RowDecorator`` hook the plugin
system doesn't expose yet — but it does the next most useful thing:
right-click → Status / Diff / Log / Blame / Stage / Commit, scoped to
the clicked path. Menu items are hidden outside a repo and Blame is
hidden on directories.

``find_repo_root`` walks up the tree looking for ``.git``; it's pure
filesystem traversal and cached only as far as one call. We do NOT
shell out to ``git rev-parse`` for the detection — that would fork
on every right-click — but we *do* shell out for the actual actions,
piping output through :class:`CommandDialog`.
"""

from __future__ import annotations

import logging
import os
import shutil

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


def find_repo_root(path: str) -> str | None:
    """Walk up from ``path`` looking for ``.git``. Return the dir holding it.

    ``.git`` can be a directory (normal repo) or a file (worktree /
    submodule); either counts. Returns ``None`` once we hit the
    filesystem root without finding one.
    """
    if not path:
        return None
    current = os.path.abspath(path)
    # If the user right-clicked on a file, start looking from its dir.
    if os.path.isfile(current):
        current = os.path.dirname(current)
    while True:
        candidate = os.path.join(current, ".git")
        if os.path.exists(candidate):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


class GitStatusPlugin(MenuProvider):
    name = "git_status"
    description = "Git actions (status / diff / log / blame / stage / commit) inside a repo"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path or not shutil.which("git"):
            return []
        root = find_repo_root(path)
        if root is None:
            return []
        items = [
            ("Git: Status", lambda p: self._run(p, root, ["status"])),
            ("Git: Diff",   lambda p: self._run(p, root, ["diff", "--", p])),
            ("Git: Log",    lambda p: self._run(p, root, ["log", "--oneline", "-n", "50", "--", p])),
        ]
        if os.path.isfile(path):
            items.append(
                ("Git: Blame", lambda p: self._run(p, root, ["blame", "--", p]))
            )
        items.append(
            ("Git: Stage", lambda p: self._run(p, root, ["add", "--", p]))
        )
        items.append(
            ("Git: Commit…", lambda p: self._commit(p, root))
        )
        return items

    def _run(self, path: str, repo_root: str, git_args: list[str]) -> None:
        from qfileman.plugins.builtin._runner import run_command_dialog
        argv = ["git", *git_args]
        title = "git " + " ".join(git_args[:1]) + f" — {os.path.basename(path)}"
        # Don't ping qdshell for read-only inspection commands — only
        # ``add`` and ``commit`` mutate state.
        notify = git_args[0] in ("add", "commit")
        run_command_dialog(title, argv, cwd=repo_root, notify=notify)

    def _commit(self, path: str, repo_root: str) -> None:
        from PyQt6.QtWidgets import QInputDialog
        message, ok = QInputDialog.getMultiLineText(
            None, "Git Commit", "Commit message:",
        )
        if not ok or not message.strip():
            return
        # Stage the right-clicked path so the commit captures it even
        # if the user hasn't run Git: Stage explicitly.
        self._run(path, repo_root, ["add", "--", path])
        self._run(path, repo_root, ["commit", "-m", message.strip()])
