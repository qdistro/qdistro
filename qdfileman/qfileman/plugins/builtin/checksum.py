"""Checksum plugin for QFileMan.

Adds MD5 / SHA-1 / SHA-256 entries that hash the selected file with
``hashlib`` (no subprocess) and show the digest in a dialog with a
copy-to-clipboard button. Big files are streamed in chunks rather
than slurped into memory.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


_CHUNK = 1 << 20  # 1 MiB


def hash_file(
    path: str,
    algorithm: str = "sha256",
    *,
    is_cancelled: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Return the hex digest of ``path`` using ``algorithm``.

    ``algorithm`` is anything :func:`hashlib.new` accepts. Raises
    ``OSError`` if the file can't be read.

    ``is_cancelled`` is polled per 1 MiB chunk; on cancel a
    :class:`qfileman.worker.Cancelled` is raised so a worker thread unwinds
    cleanly without delivering a half-computed digest. ``on_progress`` is
    called with ``(bytes_done, total_bytes)`` after each chunk; ``total`` is
    ``-1`` when the file size can't be determined up front.
    """
    h = hashlib.new(algorithm)
    try:
        total = os.path.getsize(path)
    except OSError:
        total = -1
    done = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            if is_cancelled is not None and is_cancelled():
                from qfileman.worker import Cancelled

                raise Cancelled()
            h.update(chunk)
            done += len(chunk)
            if on_progress is not None:
                on_progress(done, total)
    return h.hexdigest()


class ChecksumPlugin(MenuProvider):
    name = "checksum"
    description = "Compute MD5 / SHA-1 / SHA-256 of a file"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path or not os.path.isfile(path):
            return []
        return [
            ("MD5 Sum", lambda p: self._show(p, "md5")),
            ("SHA-1 Sum", lambda p: self._show(p, "sha1")),
            ("SHA-256 Sum", lambda p: self._show(p, "sha256")),
        ]

    def _show(self, path: str, algorithm: str) -> None:
        """Hash ``path`` off the GUI thread, then show the digest.

        Streaming a multi-gigabyte file through ``hashlib`` can take many
        seconds, so it runs on a :class:`~qfileman.worker.ProgressRunner`
        worker with a cancellable, byte-accurate progress bar; the digest
        dialog only appears once hashing completes.
        """
        from qfileman.worker import ProgressRunner

        def work(cancel, progress):
            def report(done: int, total: int) -> None:
                progress(done, total, "")

            return hash_file(
                path,
                algorithm,
                is_cancelled=cancel.is_set,
                on_progress=report,
            )

        def on_result(digest: str) -> None:
            self._show_digest(path, algorithm, digest)

        def on_error(message: str) -> None:
            log.warning("hash %s failed: %s", path, message)
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.warning(None, "Checksum", f"Read failed: {message}")

        runner = ProgressRunner(
            work,
            title=f"{algorithm.upper()} checksum",
            label=f"Hashing {os.path.basename(path)}…",
            on_result=on_result,
            on_error=on_error,
        )
        self._runner = runner
        runner.start()

    def _show_digest(self, path: str, algorithm: str, digest: str) -> None:
        from PyQt6.QtWidgets import (
            QApplication,
            QDialog,
            QDialogButtonBox,
            QLabel,
            QLineEdit,
            QPushButton,
            QVBoxLayout,
        )

        dlg = QDialog()
        dlg.setWindowTitle(f"{algorithm.upper()} — {os.path.basename(path)}")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(path, dlg))
        field = QLineEdit(digest, dlg)
        field.setReadOnly(True)
        field.selectAll()
        layout.addWidget(field)
        copy_btn = QPushButton("Copy", dlg)
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(digest))
        layout.addWidget(copy_btn)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()
