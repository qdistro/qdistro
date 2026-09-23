"""Bundled plugin: status-bar widget showing total word count of the notebook.

Refreshes when a page is saved (via existing notebook plumbing).
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QLabel


def _total_words(notebook) -> int:
    if notebook is None:
        return 0
    total = 0
    for ref in notebook.pages():
        try:
            text = notebook.get_page(ref.path)
        except Exception:
            continue
        total += len(text.split())
    return total


class Plugin:
    name = "Word Count (status bar)"
    description = "Shows total notebook word count in the status bar."

    def __init__(self) -> None:
        self.label: QLabel | None = None
        self.window = None
        self._timer: QTimer | None = None

    def setup(self, window) -> None:
        self.window = window
        self.label = QLabel("words: 0", window)
        bar = window.statusBar()
        if bar is not None:
            bar.addPermanentWidget(self.label)
        self.refresh()
        # Hook into editor saves: rely on dirtyChanged → False as a save signal.
        ed = getattr(window, "editor", None)
        if ed is not None:
            ed.dirtyChanged.connect(self._on_dirty)
        # Also schedule periodic refresh.
        self._timer = QTimer(window)
        self._timer.setInterval(15_000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def _on_dirty(self, dirty: bool) -> None:
        if not dirty:
            self.refresh()

    def refresh(self) -> None:
        if self.window is None or self.label is None:
            return
        n = _total_words(getattr(self.window, "notebook", None))
        self.label.setText(f"words: {n}")
