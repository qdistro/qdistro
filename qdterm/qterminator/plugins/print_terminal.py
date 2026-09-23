"""Print plugin for QTerminator.

Adds context menu actions to print the visible terminal or the entire
scrollback to a real printer, picked via QPrintDialog.
"""

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QPainter, QTextDocument
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from PyQt6.QtWidgets import QDialog, QMessageBox

from qterminator.plugin import MenuProvider
from qterminator.plugins.buffer_export import ansi_to_html
from qterminator.plugins.pdf_export import _buffer_text, _wrap_html


class PrintPlugin(MenuProvider):
    name = "print_terminal"
    description = "Print terminal contents to a physical printer"
    version = "1.0"
    category = "Export"

    def get_menu_items(self, terminal):
        return [
            ("Print Terminal...", self._make_print_visible(terminal)),
            ("Print Buffer...", self._make_print_buffer(terminal)),
        ]

    def _run_print_dialog(self, terminal):
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dialog = QPrintDialog(printer, terminal)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return printer

    def _make_print_visible(self, terminal):
        def callback():
            printer = self._run_print_dialog(terminal)
            if printer is None:
                return
            widget = getattr(terminal, "_term", None) or terminal
            pixmap = widget.grab()
            if pixmap.isNull():
                QMessageBox.warning(
                    terminal, "Print", "Could not capture terminal widget.",
                )
                return
            painter = QPainter(printer)
            try:
                page = painter.viewport()
                pm_w, pm_h = pixmap.width(), pixmap.height()
                if pm_w == 0 or pm_h == 0:
                    return
                scale = min(page.width() / pm_w, page.height() / pm_h)
                target = QRectF(0.0, 0.0, pm_w * scale, pm_h * scale)
                painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
            finally:
                painter.end()
        return callback

    def _make_print_buffer(self, terminal):
        def callback():
            text = _buffer_text(terminal)
            if not text:
                QMessageBox.information(
                    terminal, "Print", "Terminal buffer is empty.",
                )
                return
            printer = self._run_print_dialog(terminal)
            if printer is None:
                return
            html = _wrap_html(ansi_to_html(text), title=terminal.title())
            doc = QTextDocument()
            doc.setHtml(html)
            if hasattr(doc, "print"):
                doc.print(printer)
            else:
                doc.print_(printer)
        return callback
