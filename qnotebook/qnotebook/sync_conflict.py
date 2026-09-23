"""Syncthing conflict file detection.

Syncthing names diverged copies ``<stem>.sync-conflict-YYYYMMDD-HHMMSS-DEVICEID.<ext>``
next to the original. ``ConflictWatcher`` scans the notebook and emits a
signal whenever one appears — the status-bar badge and resolver plugin
hang off that signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QObject, pyqtSignal

CONFLICT_RE = re.compile(
    r"^(?P<stem>.+)\.sync-conflict-"
    r"(?P<date>\d{8})-(?P<time>\d{6})-"
    r"(?P<device>[A-Z0-9]+)"
    r"(?P<ext>\.[^.]+)$"
)


@dataclass(frozen=True)
class ConflictFile:
    path: Path
    original: Path
    date: str        # YYYYMMDD
    time: str        # HHMMSS
    device: str

    @property
    def iso(self) -> str:
        d, t = self.date, self.time
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]}:{t[4:6]}"


def parse_conflict_name(path: Path) -> ConflictFile | None:
    m = CONFLICT_RE.match(path.name)
    if not m:
        return None
    stem = m.group("stem")
    ext = m.group("ext")
    return ConflictFile(
        path=path,
        original=path.with_name(stem + ext),
        date=m.group("date"),
        time=m.group("time"),
        device=m.group("device"),
    )


def scan(root: Path) -> list[ConflictFile]:
    """Walk ``root`` looking for Syncthing conflict files."""
    out: list[ConflictFile] = []
    if not root.is_dir():
        return out
    for p in root.rglob("*.sync-conflict-*"):
        if ".qnotebook" in p.parts:
            continue
        cf = parse_conflict_name(p)
        if cf is not None:
            out.append(cf)
    return out


class ConflictWatcher(QObject):
    """Emit ``conflictFileFound(path)`` for each conflict file found.

    Call ``rescan()`` on notebook open. Also attach to a QFileSystemWatcher
    on the notebook root so newly created conflicts fire too.
    """

    conflictFileFound = pyqtSignal(object)  # ConflictFile

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._root: Path | None = None
        self._seen: set[str] = set()
        self._fsw = QFileSystemWatcher(self)
        self._fsw.directoryChanged.connect(self._on_dir_changed)

    def set_root(self, root: Path | None) -> None:
        if self._fsw.directories():
            self._fsw.removePaths(self._fsw.directories())
        self._seen.clear()
        self._root = root
        if root is not None and root.is_dir():
            self._fsw.addPath(str(root))
            self.rescan()

    def rescan(self) -> None:
        if self._root is None:
            return
        for cf in scan(self._root):
            key = str(cf.path)
            if key in self._seen:
                continue
            self._seen.add(key)
            self.conflictFileFound.emit(cf)

    def current(self) -> list[ConflictFile]:
        if self._root is None:
            return []
        return scan(self._root)

    def _on_dir_changed(self, _path: str) -> None:
        self.rescan()
