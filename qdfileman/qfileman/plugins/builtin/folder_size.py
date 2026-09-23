"""Folder-size plugin for QFileMan.

Adds *Folder Size…* — a recursive size walker that pops a sortable
table of the immediate children of the selected directory plus a
grand total at the top. Mirrors Dolphin's "Show Folder Size" and the
classic ``ncdu`` workflow without depending on either.

The walker is :func:`directory_size` — a tight ``os.scandir``-based
loop that recurses without following symlinks (which would risk
loops) and silently skips entries that error out (broken symlinks,
unreadable directories). It's exposed at module level so the
plugin's pure logic can be exercised under tests.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


def directory_size(
    path: str,
    *,
    follow_symlinks: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
) -> int:
    """Return the total byte size of ``path``, recursing into subdirs.

    Errors from individual ``stat`` / ``scandir`` calls are swallowed
    rather than propagated; that matches the behaviour of ``du`` and
    avoids breaking a long walk because of one permissions-denied
    directory.

    ``is_cancelled``, when supplied, is polled once per directory; if it
    returns ``True`` the walk stops and the partial total accumulated so
    far is returned, so a worker thread can abandon a huge tree promptly.
    """
    total = 0
    stack: list[str] = [path]
    while stack:
        if is_cancelled is not None and is_cancelled():
            break
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as e:
            log.debug("scandir(%s): %s", current, e)
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_symlink() and not follow_symlinks:
                        # Count the symlink itself (its lstat size),
                        # not the target.
                        total += entry.stat(follow_symlinks=False).st_size
                        continue
                    if entry.is_dir(follow_symlinks=follow_symlinks):
                        stack.append(entry.path)
                    else:
                        total += entry.stat(
                            follow_symlinks=follow_symlinks
                        ).st_size
                except OSError as e:
                    log.debug("stat(%s): %s", entry.path, e)
    return total


def format_size(n: int) -> str:
    """Return a human-readable byte count, IEC units (1024-based)."""
    if n < 1024:
        return f"{n} B"
    units = ("KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(n)
    for unit in units:
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} EiB"


def child_sizes(
    path: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[tuple[str, int, bool]]:
    """Return ``(name, size, is_dir)`` for each immediate child of ``path``.

    Children that error out during stat/scan are skipped silently.
    Sizes for subdirectories are recursive (call :func:`directory_size`).
    Order follows :func:`os.scandir` (unspecified).

    ``is_cancelled`` is polled before each child and threaded into the
    recursive sub-walk; on cancel the children gathered so far are returned.
    ``on_progress``, if given, is called with each child's name as it is
    about to be measured, so a worker can report which entry it's on.
    """
    out: list[tuple[str, int, bool]] = []
    try:
        it = os.scandir(path)
    except OSError:
        return out
    with it:
        for entry in it:
            if is_cancelled is not None and is_cancelled():
                break
            if on_progress is not None:
                on_progress(entry.name)
            try:
                if entry.is_dir(follow_symlinks=False):
                    out.append((
                        entry.name,
                        directory_size(entry.path, is_cancelled=is_cancelled),
                        True,
                    ))
                else:
                    size = entry.stat(follow_symlinks=False).st_size
                    out.append((entry.name, size, False))
            except OSError:
                continue
    return out


class FolderSizePlugin(MenuProvider):
    name = "folder_size"
    description = "Recursive folder size breakdown (du-style)"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path or not os.path.isdir(path):
            return []
        return [("Folder Size…", self._show)]

    def _show(self, path: str) -> None:
        """Walk ``path`` off the GUI thread, then show the size breakdown.

        The recursive ``child_sizes`` scan can take a long time on a big
        tree, so it runs on a :class:`~qfileman.worker.ProgressRunner` worker
        with a cancellable progress dialog; the result table is only built
        once the walk completes (cancel just dismisses the progress dialog).
        """
        from qfileman.worker import ProgressRunner

        def work(cancel, progress):
            def report(name: str) -> None:
                progress(0, -1, name)

            children = child_sizes(
                path, is_cancelled=cancel.is_set, on_progress=report
            )
            cancel.raise_if_cancelled()
            return children

        def on_result(children: list[tuple[str, int, bool]]) -> None:
            self._show_results(path, children)

        # Hold the runner alive on the plugin instance until it finishes.
        runner = ProgressRunner(
            work,
            title="Folder Size",
            label=f"Scanning {os.path.basename(path) or path}…",
            on_result=on_result,
        )
        self._runner = runner
        runner.start()

    def _show_results(
        self, path: str, children: list[tuple[str, int, bool]]
    ) -> None:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QLabel,
            QTreeWidget,
            QTreeWidgetItem,
            QVBoxLayout,
        )

        total = sum(s for _n, s, _d in children)

        dlg = QDialog()
        dlg.setWindowTitle(f"Size: {os.path.basename(path) or path}")
        dlg.resize(550, 450)
        layout = QVBoxLayout(dlg)
        layout.addWidget(
            QLabel(f"<b>Total:</b> {format_size(total)} ({total:,} bytes)", dlg)
        )
        tree = QTreeWidget(dlg)
        tree.setHeaderLabels(["Name", "Size", "Bytes"])
        tree.setSortingEnabled(True)
        tree.setRootIsDecorated(False)

        for name, size, is_dir in sorted(children, key=lambda c: -c[1]):
            display = name + "/" if is_dir else name
            item = QTreeWidgetItem([display, format_size(size), f"{size:,}"])
            # Right-align numeric columns.
            item.setTextAlignment(1, int(Qt.AlignmentFlag.AlignRight))
            item.setTextAlignment(2, int(Qt.AlignmentFlag.AlignRight))
            # Sort by the bytes column numerically; Qt sorts strings by
            # default, so stash the int as user data on column 2.
            item.setData(2, Qt.ItemDataRole.UserRole, size)
            tree.addTopLevelItem(item)

        # Sort by bytes descending by default.
        tree.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        tree.resizeColumnToContents(0)
        layout.addWidget(tree, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()
