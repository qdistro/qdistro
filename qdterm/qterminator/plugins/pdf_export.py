"""PDF export plugin for QTerminator.

Adds context menu actions to export the terminal as a PDF file. The visible
area is rendered by painting the widget to a QPrinter (PDF output). The full
scrollback is rendered by converting the buffer's ANSI output to HTML (via
the existing `buffer_export.ansi_to_html`) and printing a QTextDocument.
"""

import os
from datetime import datetime

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QPainter, QTextDocument
from PyQt6.QtPrintSupport import QPrinter
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from qterminator.plugin import MenuProvider
from qterminator.plugins.buffer_export import ansi_to_html


def _default_pdf_path():
    save_dir = os.path.expanduser("~/Documents")
    os.makedirs(save_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(save_dir, f"qterminator-{timestamp}.pdf")


def _wrap_html(body_fragment, title="Terminal Buffer"):
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>"
        "<style>"
        "body { background:#1e1e1e; color:#d3d7cf; "
        "font-family: 'DejaVu Sans Mono', 'Monaco', monospace; "
        "font-size: 10pt; white-space: pre-wrap; }"
        "</style></head><body>"
        f"{body_fragment}"
        "</body></html>"
    )


def _buffer_text(terminal):
    """Best-effort read of full buffer (visible + scrollback)."""
    term = getattr(terminal, "_term", None)
    if term is None:
        return terminal.selected_text() or ""
    try:
        if (hasattr(term, "setSelectionStart")
                and hasattr(term, "setSelectionEnd")
                and hasattr(term, "screenColumnsCount")
                and hasattr(term, "screenLinesCount")):
            history = 0
            if hasattr(term, "historyLinesCount"):
                try:
                    history = int(term.historyLinesCount())
                except Exception:
                    history = 0
            cols = int(term.screenColumnsCount())
            rows = int(term.screenLinesCount())
            if cols > 0 and rows > 0:
                # Select the whole buffer. Selection rows are ABSOLUTE (row 0 is
                # the top of the scrollback), so the last cell is
                # (history + visible_rows - 1, columns). The end MUST be bounded
                # to the real extent: QTermWidget's selectedText() SEGFAULTS on
                # an out-of-range selection end (it walks past the allocated
                # screen image), so the old (100000, 100000) end crashed the app
                # on the real SIP binding. Relies on the screenLinesCount()
                # binding added to the vendored SIP.
                term.setSelectionStart(0, 0)
                term.setSelectionEnd(history + rows - 1, cols)
                text = term.selectedText()
                if hasattr(term, "clearSelection"):
                    term.clearSelection()
                if text:
                    return text
    except Exception:
        pass
    return terminal.selected_text() or ""


def _make_printer(path):
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    return printer


def _print_document(html, path):
    """Render ``html`` to a PDF at ``path``. Returns True on success."""
    printer = _make_printer(path)
    doc = QTextDocument()
    doc.setHtml(html)
    # Qt 6: QTextDocument.print exists (print_ was the Qt5 rename).
    if hasattr(doc, "print"):
        doc.print(printer)
    else:
        doc.print_(printer)
    return os.path.exists(path) and os.path.getsize(path) > 0


def _print_widget(widget, path):
    """Paint a QWidget onto a PDF-output QPrinter."""
    printer = _make_printer(path)
    pixmap = widget.grab()
    painter = QPainter(printer)
    try:
        if pixmap.isNull():
            return False
        page_rect = painter.viewport()
        # Fit pixmap into page, preserving aspect.
        pm_w, pm_h = pixmap.width(), pixmap.height()
        if pm_w == 0 or pm_h == 0:
            return False
        scale = min(page_rect.width() / pm_w,
                    page_rect.height() / pm_h)
        target_w = pm_w * scale
        target_h = pm_h * scale
        target = QRectF(0.0, 0.0, target_w, target_h)
        painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
    finally:
        painter.end()
    return os.path.exists(path) and os.path.getsize(path) > 0


class PdfExportPlugin(MenuProvider):
    name = "pdf_export"
    description = "Export terminal contents to PDF"
    version = "1.0"
    category = "Export"

    def get_menu_items(self, terminal):
        return [
            ("Export to PDF (visible)...",
             self._make_export_visible(terminal)),
            ("Export to PDF (full buffer)...",
             self._make_export_buffer(terminal)),
        ]

    def _make_export_visible(self, terminal):
        def callback():
            path, _ = QFileDialog.getSaveFileName(
                terminal, "Export Visible Terminal to PDF",
                _default_pdf_path(),
                "PDF Files (*.pdf);;All Files (*)",
            )
            if not path:
                return
            widget = getattr(terminal, "_term", None) or terminal
            try:
                ok = _print_widget(widget, path)
            except Exception as e:
                QMessageBox.critical(terminal, "PDF Export Failed", str(e))
                return
            if not ok:
                QMessageBox.critical(
                    terminal, "PDF Export Failed",
                    "Could not produce PDF output.",
                )
        return callback

    def _make_export_buffer(self, terminal):
        def callback():
            text = _buffer_text(terminal)
            if not text:
                QMessageBox.information(
                    terminal, "Export to PDF",
                    "Terminal buffer is empty.",
                )
                return
            path, _ = QFileDialog.getSaveFileName(
                terminal, "Export Terminal Buffer to PDF",
                _default_pdf_path(),
                "PDF Files (*.pdf);;All Files (*)",
            )
            if not path:
                return
            html = _wrap_html(ansi_to_html(text),
                              title=terminal.title())
            try:
                ok = _print_document(html, path)
            except Exception as e:
                QMessageBox.critical(terminal, "PDF Export Failed", str(e))
                return
            if not ok:
                QMessageBox.critical(
                    terminal, "PDF Export Failed",
                    "Could not produce PDF output.",
                )
        return callback
