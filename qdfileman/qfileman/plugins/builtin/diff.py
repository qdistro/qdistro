"""Diff plugin for QFileMan.

Provides Krusader-style "compare two files" entries. The plugin picks
a graphical diff tool from a preference list (``meld``, ``kdiff3``,
``diffuse``, ``xxdiff``, ``kompare``) and falls back to ``diff``
running in the standard :class:`CommandDialog` if none of the GUI
tools are installed.

Two interaction modes are offered, copying the way Total Commander,
Double Commander, and Krusader handle this:

* **Set as Diff Source** + **Diff Against Source** — pick a first
  file, then right-click a second file to compare. The source is
  process-wide and lives until the next pick.
* **Diff With...** — single-step picker via :class:`QFileDialog`.
"""

from __future__ import annotations

import logging
import shutil

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


# Ordered preference: graphical tools first, plain ``diff`` last.
DIFF_TOOLS: tuple[tuple[str, list[str]], ...] = (
    ("meld", ["meld"]),
    ("kdiff3", ["kdiff3"]),
    ("diffuse", ["diffuse"]),
    ("xxdiff", ["xxdiff"]),
    ("kompare", ["kompare"]),
)


def choose_tool() -> tuple[str, list[str]] | None:
    """Return ``(name, prefix_argv)`` for the first installed diff tool."""
    for name, argv in DIFF_TOOLS:
        if shutil.which(argv[0]):
            return name, list(argv)
    return None


def build_argv(file_a: str, file_b: str) -> list[str]:
    """Build a diff argv. Falls back to plain ``diff`` if no GUI tool is found."""
    tool = choose_tool()
    if tool is None:
        # ``--`` so a file named ``-something`` is a path, not a diff option.
        return ["diff", "-u", "--", file_a, file_b]
    _name, prefix = tool
    return [*prefix, file_a, file_b]


class DiffPlugin(MenuProvider):
    name = "diff"
    description = "Compare two files in a graphical diff tool"
    version = "1.0"
    category = "Tools"

    # Process-wide; persists across pane switches.
    _diff_source: str | None = None

    def get_menu_items(self, path):
        if not path:
            return []
        items = [
            ("Diff With...", self._diff_with_picker),
            ("Set as Diff Source", self._set_source),
        ]
        if DiffPlugin._diff_source and DiffPlugin._diff_source != path:
            label = f"Diff Against Source ({DiffPlugin._diff_source})"
            items.append((label, self._diff_against_source))
        return items

    # ------------------------------------------------------------- actions
    def _set_source(self, path: str) -> None:
        DiffPlugin._diff_source = path
        log.info("diff source set: %s", path)

    def _diff_against_source(self, path: str) -> None:
        src = DiffPlugin._diff_source
        if not src:
            return
        self._run(src, path)

    def _diff_with_picker(self, path: str) -> None:
        import os

        from PyQt6.QtWidgets import QFileDialog
        other, _ = QFileDialog.getOpenFileName(
            None, f"Diff {os.path.basename(path)} with…",
            os.path.dirname(path) or "",
        )
        if not other:
            return
        self._run(path, other)

    # ------------------------------------------------------------- plumbing
    def _run(self, file_a: str, file_b: str) -> None:
        argv = build_argv(file_a, file_b)
        tool = choose_tool()
        if tool is not None:
            # GUI tools have their own windows; we just launch and forget.
            import subprocess
            try:
                subprocess.Popen(argv)
            except OSError as e:
                self._warn(f"Failed to launch {argv[0]}: {e}")
            return
        # Fallback: run plain ``diff`` and show its output in our dialog.
        import os

        from qfileman.plugins.builtin._runner import run_command_dialog
        title = f"diff {os.path.basename(file_a)} {os.path.basename(file_b)}"
        run_command_dialog(title, argv, notify=False)

    @staticmethod
    def _warn(message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        log.warning(message)
        QMessageBox.warning(None, "Diff", message)
