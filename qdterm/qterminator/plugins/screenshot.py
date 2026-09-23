"""Screenshot plugin for QTerminator.

Provides context menu actions to capture the terminal widget as a PNG image:
- Visible area to a file
- Visible area to the clipboard
- Full scrollback buffer to a file (best-effort; see limitations below)

Limitations
-----------
QTermWidget does not expose a documented API to render arbitrary portions of
its scrollback into a pixmap. The "entire buffer" action attempts to render a
composite image by temporarily scrolling through the history and grabbing
each page; if scroll APIs are unavailable this falls back to a visible-area
screenshot and informs the user.
"""

import os
from datetime import datetime

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

from qterminator.plugin import MenuProvider


def _default_screenshot_path():
    save_dir = os.path.expanduser("~/Pictures")
    os.makedirs(save_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(save_dir, f"qterminator-{timestamp}.png")


class ScreenshotPlugin(MenuProvider):
    name = "screenshot"
    description = "Capture terminal screenshots (visible area, buffer, clipboard)"
    version = "1.0"
    category = "Export"

    def get_menu_items(self, terminal):
        return [
            ("Screenshot Visible Area...",
             self._make_save_visible(terminal)),
            ("Screenshot Entire Buffer...",
             self._make_save_buffer(terminal)),
            ("Screenshot to Clipboard",
             self._make_clipboard(terminal)),
        ]

    # -- helpers --

    def _grab_visible(self, terminal):
        """Grab the currently visible terminal widget as a QPixmap."""
        term = getattr(terminal, "_term", None)
        if term is not None:
            try:
                return term.grab()
            except Exception:
                pass
        return terminal.grab()

    def _grab_buffer(self, terminal):
        """Best-effort: grab a pixmap covering the full scrollback.

        Strategy: compute how many visible-page screenshots are needed to
        cover history + screen lines, scroll page-by-page, grab each page,
        and paint them vertically into one composite pixmap. If the widget
        doesn't expose enough scroll API we fall back to the visible grab
        and return a flag indicating truncation.
        """
        term = getattr(terminal, "_term", None)
        visible = self._grab_visible(terminal)
        if term is None:
            return visible, False

        history = 0
        screen_lines = 0
        try:
            if hasattr(term, "historyLinesCount"):
                history = int(term.historyLinesCount())
            if hasattr(term, "screenLinesCount"):
                screen_lines = int(term.screenLinesCount())
        except Exception:
            history, screen_lines = 0, 0

        if history <= 0 or screen_lines <= 0:
            return visible, False

        # Try to find a scroll method. QTermWidget exposes
        # scrollToTop()/scrollToBottom() in some versions; a setHistoryPosition
        # equivalent is not guaranteed. Without fine-grained scroll control we
        # cannot reliably composite — fall back.
        if not (hasattr(term, "scrollToTop")
                and hasattr(term, "scrollToBottom")):
            return visible, True

        # Render only top + bottom if we can't page precisely; composite them.
        try:
            term.scrollToTop()
            top_pix = term.grab()
            term.scrollToBottom()
            bottom_pix = term.grab()
        except Exception:
            return visible, True

        if top_pix.isNull() or bottom_pix.isNull():
            return visible, True

        width = max(top_pix.width(), bottom_pix.width())
        height = top_pix.height() + bottom_pix.height()
        composite = QPixmap(width, height)
        composite.fill()
        painter = QPainter(composite)
        try:
            painter.drawPixmap(QRect(0, 0, top_pix.width(),
                                     top_pix.height()), top_pix)
            painter.drawPixmap(
                QRect(0, top_pix.height(),
                      bottom_pix.width(), bottom_pix.height()),
                bottom_pix,
            )
        finally:
            painter.end()
        # Truncated=True because we only captured top and bottom pages, not
        # every intermediate page.
        return composite, True

    # -- callbacks --

    def _make_save_visible(self, terminal):
        def callback():
            pixmap = self._grab_visible(terminal)
            if pixmap.isNull():
                QMessageBox.warning(
                    terminal, "Screenshot",
                    "Could not capture terminal widget.",
                )
                return
            path, _ = QFileDialog.getSaveFileName(
                terminal, "Save Screenshot",
                _default_screenshot_path(),
                "PNG Images (*.png);;All Files (*)",
            )
            if not path:
                return
            if not pixmap.save(path, "PNG"):
                QMessageBox.critical(
                    terminal, "Screenshot Failed",
                    f"Could not write PNG to {path}",
                )
        return callback

    def _make_save_buffer(self, terminal):
        def callback():
            pixmap, truncated = self._grab_buffer(terminal)
            if pixmap.isNull():
                QMessageBox.warning(
                    terminal, "Screenshot",
                    "Could not capture terminal buffer.",
                )
                return
            path, _ = QFileDialog.getSaveFileName(
                terminal, "Save Buffer Screenshot",
                _default_screenshot_path(),
                "PNG Images (*.png);;All Files (*)",
            )
            if not path:
                return
            if not pixmap.save(path, "PNG"):
                QMessageBox.critical(
                    terminal, "Screenshot Failed",
                    f"Could not write PNG to {path}",
                )
                return
            if truncated:
                QMessageBox.information(
                    terminal, "Screenshot",
                    "Note: full scrollback rendering is not supported by "
                    "QTermWidget's Python bindings; the saved image may be "
                    "incomplete. Use 'Export Buffer to HTML' for a full "
                    "text+color dump.",
                )
        return callback

    def _make_clipboard(self, terminal):
        def callback():
            pixmap = self._grab_visible(terminal)
            if pixmap.isNull():
                QMessageBox.warning(
                    terminal, "Screenshot",
                    "Could not capture terminal widget.",
                )
                return
            QApplication.clipboard().setPixmap(pixmap)
        return callback
