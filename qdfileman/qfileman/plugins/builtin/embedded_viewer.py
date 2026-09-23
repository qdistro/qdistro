"""Embedded F3-style viewer plugin for QFileMan.

Adds *Quick View…* — a built-in viewer that pops the file's contents
in a dialog without spawning an external app. Patterned after Total
Commander's Lister and Krusader's QuickView. Three modes:

* **Text** — UTF-8 decoded into a ``QPlainTextEdit``. Best for source
  code, logs, configs.
* **Image** — Qt-supported formats rendered with ``QPixmap`` inside a
  scrollable ``QLabel``.
* **Hex** — 16-byte rows with offset + ASCII gutter, for binaries
  where text mode would be meaningless.

The mode is auto-detected from the extension and a sniff of the first
4 KiB (NUL bytes ⇒ binary ⇒ hex). The user can flip modes with three
radio buttons at the top of the dialog.

Big files are read in full up to :data:`MAX_BYTES`; past that they're
truncated and the dialog says so. We deliberately don't try to stream:
the value of a quick viewer is that it shows you everything at once,
and 8 MiB is enough for the inspection workflow.

:func:`detect_kind` and :func:`format_hex` are pure functions exposed
for tests.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


MAX_BYTES = 8 * 1024 * 1024  # 8 MiB
_SNIFF_BYTES = 4096


Kind = Literal["text", "image", "hex"]


_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
    ".tif", ".tiff", ".ico", ".xpm", ".pbm", ".pgm", ".ppm",
})


def detect_kind(path: str, sample: bytes | None = None) -> Kind:
    """Pick a viewer mode for ``path``.

    Extension match takes precedence for images; everything else is
    "text" unless the first :data:`_SNIFF_BYTES` bytes contain a NUL,
    in which case we treat it as binary and switch to hex.

    Passing ``sample`` short-circuits the file read — handy for tests.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if sample is None:
        try:
            with open(path, "rb") as f:
                sample = f.read(_SNIFF_BYTES)
        except OSError:
            return "text"
    if b"\x00" in sample:
        return "hex"
    return "text"


def format_hex(data: bytes, *, base_offset: int = 0) -> str:
    """Render ``data`` as a hex dump: ``OFFSET  HEX BYTES  ASCII``.

    16 bytes per row, ``base_offset`` lets the caller produce continued
    chunks if it ever needs to. Pure function so the formatting can be
    pinned with tests.
    """
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        offset = base_offset + i
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        # Pad short final line so the ASCII column stays aligned.
        hex_part = hex_part.ljust(16 * 3 - 1)
        ascii_part = "".join(
            chr(b) if 32 <= b < 127 else "." for b in chunk
        )
        lines.append(f"{offset:08x}  {hex_part}  {ascii_part}")
    return "\n".join(lines)


class EmbeddedViewerPlugin(MenuProvider):
    name = "embedded_viewer"
    description = "Quick text / image / hex viewer (Total Commander Lister-style)"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path or not os.path.isfile(path):
            return []
        return [("Quick View…", self._view)]

    def _view(self, path: str) -> None:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtWidgets import (
            QButtonGroup,
            QDialog,
            QDialogButtonBox,
            QHBoxLayout,
            QLabel,
            QPlainTextEdit,
            QRadioButton,
            QScrollArea,
            QStackedWidget,
            QVBoxLayout,
        )

        try:
            with open(path, "rb") as f:
                raw = f.read(MAX_BYTES + 1)
        except OSError as e:
            log.warning("read %s: %s", path, e)
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(None, "Quick View", f"Read failed: {e}")
            return

        truncated = len(raw) > MAX_BYTES
        if truncated:
            raw = raw[:MAX_BYTES]

        initial_kind = detect_kind(path, sample=raw[:_SNIFF_BYTES])

        dlg = QDialog()
        dlg.setWindowTitle(
            f"Quick View — {os.path.basename(path)}"
            + (" (truncated)" if truncated else "")
        )
        dlg.resize(800, 600)
        layout = QVBoxLayout(dlg)

        # Mode toggle row.
        mode_row = QHBoxLayout()
        text_rb = QRadioButton("Text", dlg)
        image_rb = QRadioButton("Image", dlg)
        hex_rb = QRadioButton("Hex", dlg)
        group = QButtonGroup(dlg)
        group.addButton(text_rb)
        group.addButton(image_rb)
        group.addButton(hex_rb)
        mode_row.addWidget(text_rb)
        mode_row.addWidget(image_rb)
        mode_row.addWidget(hex_rb)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        stack = QStackedWidget(dlg)
        layout.addWidget(stack, 1)

        # Text pane.
        text_view = QPlainTextEdit(dlg)
        text_view.setReadOnly(True)
        text_view.setPlainText(raw.decode("utf-8", errors="replace"))
        stack.addWidget(text_view)

        # Image pane.
        image_label = QLabel(dlg)
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea(dlg)
        scroll.setWidget(image_label)
        scroll.setWidgetResizable(True)
        pixmap = QPixmap()
        pixmap.loadFromData(raw)
        if pixmap.isNull():
            image_label.setText("(unsupported image format)")
        else:
            image_label.setPixmap(pixmap)
        stack.addWidget(scroll)

        # Hex pane — only render the head; full files are slow in a
        # QPlainTextEdit at 8 MiB.
        hex_view = QPlainTextEdit(dlg)
        hex_view.setReadOnly(True)
        hex_view.setStyleSheet("font-family: monospace;")
        hex_view.setPlainText(format_hex(raw[: 64 * 1024]))
        stack.addWidget(hex_view)

        kind_to_index = {"text": 0, "image": 1, "hex": 2}
        kind_to_button = {"text": text_rb, "image": image_rb, "hex": hex_rb}
        kind_to_button[initial_kind].setChecked(True)
        stack.setCurrentIndex(kind_to_index[initial_kind])

        text_rb.toggled.connect(
            lambda checked: checked and stack.setCurrentIndex(0)
        )
        image_rb.toggled.connect(
            lambda checked: checked and stack.setCurrentIndex(1)
        )
        hex_rb.toggled.connect(
            lambda checked: checked and stack.setCurrentIndex(2)
        )

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        close.rejected.connect(dlg.reject)
        close.accepted.connect(dlg.accept)
        layout.addWidget(close)
        dlg.exec()
