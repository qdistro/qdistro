"""QFileSystemWatcher wrapper: watch the current page and fire a signal
when it changes on disk.

Owner (MainWindow) subscribes to `fileChanged(path)`; depending on the
editor's dirty state, it reloads silently or prompts the user.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QObject, pyqtSignal


class PageWatcher(QObject):
    """Watch exactly one file at a time. Re-add the watch after reload since
    some editors (and atomic rename saves) remove the inotify watch."""

    fileChanged = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._fsw = QFileSystemWatcher(self)
        self._fsw.fileChanged.connect(self._on_changed)
        self._current: str | None = None

    def watch(self, path: str | Path | None) -> None:
        """Replace the watched path. Pass None to clear."""
        if self._current:
            self._fsw.removePath(self._current)
            self._current = None
        if path is None:
            return
        p = str(path)
        if not Path(p).is_file():
            return
        self._fsw.addPath(p)
        self._current = p

    def rearm(self) -> None:
        """Re-add the current path (call after a save that removed the watch)."""
        if self._current and self._current not in self._fsw.files():
            if Path(self._current).is_file():
                self._fsw.addPath(self._current)

    def _on_changed(self, path: str) -> None:
        self.fileChanged.emit(path)
        # Re-add: atomic rename-based saves invalidate the inotify watch.
        if Path(path).is_file() and path not in self._fsw.files():
            self._fsw.addPath(path)
