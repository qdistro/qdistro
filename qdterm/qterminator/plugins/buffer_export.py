"""Buffer export and rich-format clipboard plugin for QTerminator.

Adds context menu items for exporting the terminal buffer to HTML or plain
text files, and for copying selections to the clipboard in rich formats
(HTML, Markdown, Rich Text).
"""

import html
import re

from PyQt6.QtCore import QMimeData
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

from qterminator.plugin import MenuProvider

# Match ANSI CSI SGR sequences (the ones that carry color/style info),
# plus generic CSI, OSC, and charset-switch escapes for stripping.
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_STRIP_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]"
)


# Basic ANSI -> CSS color map (xterm-ish palette).
_ANSI_FG = {
    30: "#000000", 31: "#cc0000", 32: "#4e9a06", 33: "#c4a000",
    34: "#3465a4", 35: "#75507b", 36: "#06989a", 37: "#d3d7cf",
    90: "#555753", 91: "#ef2929", 92: "#8ae234", 93: "#fce94f",
    94: "#729fcf", 95: "#ad7fa8", 96: "#34e2e2", 97: "#eeeeec",
}
_ANSI_BG = {
    40: "#000000", 41: "#cc0000", 42: "#4e9a06", 43: "#c4a000",
    44: "#3465a4", 45: "#75507b", 46: "#06989a", 47: "#d3d7cf",
    100: "#555753", 101: "#ef2929", 102: "#8ae234", 103: "#fce94f",
    104: "#729fcf", 105: "#ad7fa8", 106: "#34e2e2", 107: "#eeeeec",
}


def _style_to_css(state):
    parts = []
    if state.get("fg"):
        parts.append(f"color:{state['fg']}")
    if state.get("bg"):
        parts.append(f"background-color:{state['bg']}")
    if state.get("bold"):
        parts.append("font-weight:bold")
    if state.get("italic"):
        parts.append("font-style:italic")
    if state.get("underline"):
        parts.append("text-decoration:underline")
    return ";".join(parts)


def ansi_to_html(text):
    """Convert a string with ANSI SGR escape codes to an HTML fragment.

    Non-SGR escape sequences (cursor moves, OSC titles, etc.) are stripped.
    """
    # Strip non-SGR escapes first so they don't pollute output. We do this by
    # removing anything matched by _ANSI_STRIP_RE that isn't an SGR ...m.
    def _strip_non_sgr(match):
        s = match.group(0)
        if _SGR_RE.fullmatch(s):
            return s
        return ""

    text = _ANSI_STRIP_RE.sub(_strip_non_sgr, text)

    out = []
    state = {"fg": None, "bg": None, "bold": False,
             "italic": False, "underline": False}
    span_open = False

    def close_span():
        nonlocal span_open
        if span_open:
            out.append("</span>")
            span_open = False

    def open_span():
        nonlocal span_open
        css = _style_to_css(state)
        if css:
            out.append(f'<span style="{css}">')
            span_open = True

    pos = 0
    for m in _SGR_RE.finditer(text):
        # Append literal text (HTML-escaped) before this escape.
        chunk = text[pos:m.start()]
        if chunk:
            out.append(html.escape(chunk).replace("\n", "<br>\n"))
        pos = m.end()

        params = m.group(1)
        codes = [int(c) for c in params.split(";") if c != ""] if params else [0]

        # Apply codes; re-open span after any state change.
        close_span()
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                state = {"fg": None, "bg": None, "bold": False,
                         "italic": False, "underline": False}
            elif c == 1:
                state["bold"] = True
            elif c == 3:
                state["italic"] = True
            elif c == 4:
                state["underline"] = True
            elif c == 22:
                state["bold"] = False
            elif c == 23:
                state["italic"] = False
            elif c == 24:
                state["underline"] = False
            elif c in _ANSI_FG:
                state["fg"] = _ANSI_FG[c]
            elif c in _ANSI_BG:
                state["bg"] = _ANSI_BG[c]
            elif c == 39:
                state["fg"] = None
            elif c == 49:
                state["bg"] = None
            elif c == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                # 256-color fg; fall back to a default grey mapping.
                state["fg"] = f"#{codes[i + 2]:02x}{codes[i + 2]:02x}{codes[i + 2]:02x}"
                i += 2
            elif c == 48 and i + 2 < len(codes) and codes[i + 1] == 5:
                state["bg"] = f"#{codes[i + 2]:02x}{codes[i + 2]:02x}{codes[i + 2]:02x}"
                i += 2
            elif c == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
                r, g, b = codes[i + 2], codes[i + 3], codes[i + 4]
                state["fg"] = f"#{r:02x}{g:02x}{b:02x}"
                i += 4
            elif c == 48 and i + 4 < len(codes) and codes[i + 1] == 2:
                r, g, b = codes[i + 2], codes[i + 3], codes[i + 4]
                state["bg"] = f"#{r:02x}{g:02x}{b:02x}"
                i += 4
            i += 1
        open_span()

    # Trailing text after the last escape.
    tail = text[pos:]
    if tail:
        out.append(html.escape(tail).replace("\n", "<br>\n"))
    close_span()

    return "".join(out)


def _wrap_html_document(body_fragment, title="Terminal Buffer"):
    return (
        "<!DOCTYPE html>\n"
        "<html><head><meta charset=\"utf-8\">\n"
        f"<title>{html.escape(title)}</title>\n"
        "<style>\n"
        "body { background:#1e1e1e; color:#d3d7cf; "
        "font-family: 'DejaVu Sans Mono', 'Monaco', monospace; "
        "font-size: 11pt; padding: 1em; white-space: pre-wrap; }\n"
        "</style>\n"
        "</head><body>\n"
        f"{body_fragment}\n"
        "</body></html>\n"
    )


def _strip_ansi(text):
    return _ANSI_STRIP_RE.sub("", text)


class BufferExportPlugin(MenuProvider):
    name = "buffer_export"
    description = "Export terminal buffer and copy in rich formats"
    version = "1.0"
    category = "Export"

    def get_menu_items(self, terminal):
        items = [
            ("Export Buffer to HTML", self._make_export_html(terminal)),
            ("Export Buffer to Plain Text", self._make_export_text(terminal)),
            ("Copy as HTML", self._make_copy_html(terminal)),
            ("Copy as Markdown", self._make_copy_markdown(terminal)),
            ("Copy as Rich Text", self._make_copy_rich(terminal)),
        ]
        if terminal.selected_text():
            items.append((
                "Save Selection as File",
                self._make_save_selection(terminal),
            ))
        return items

    # -- Helpers --

    def _buffer_text(self, terminal):
        """Return best-effort full buffer text (visible + scrollback).

        QTermWidget's SIP bindings expose selectedText() but no documented
        'get whole buffer' API; we try selecting the entire buffer, reading
        the selection, then clearing it. Falls back to selected text, then
        the empty string.
        """
        term = terminal._term
        try:
            # Best-effort: ask for the entire screen + history range.
            if (
                hasattr(term, "setSelectionStart")
                and hasattr(term, "setSelectionEnd")
                and hasattr(term, "screenColumnsCount")
                and hasattr(term, "screenLinesCount")
            ):
                history = 0
                if hasattr(term, "historyLinesCount"):
                    try:
                        history = int(term.historyLinesCount())
                    except Exception:
                        history = 0
                cols = int(term.screenColumnsCount())
                rows = int(term.screenLinesCount())
                if cols > 0 and rows > 0:
                    # Select the whole buffer. Selection rows are ABSOLUTE
                    # (row 0 is the top of the scrollback), so the last cell is
                    # (history + visible_rows - 1, columns). The end MUST be
                    # bounded to the real extent: QTermWidget's selectedText()
                    # SEGFAULTS on an out-of-range selection end (it walks past
                    # the allocated screen image), so the old (100000, 100000)
                    # end crashed the app on the real SIP binding. Relies on the
                    # screenLinesCount() binding added to the vendored SIP.
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

    def _selection_or_buffer(self, terminal):
        sel = terminal.selected_text()
        if sel:
            return sel
        return self._buffer_text(terminal)

    # -- Callbacks --

    def _make_export_html(self, terminal):
        def callback():
            text = self._buffer_text(terminal)
            if not text:
                QMessageBox.information(
                    terminal, "Export Buffer", "Terminal buffer is empty."
                )
                return
            path, _ = QFileDialog.getSaveFileName(
                terminal, "Export Buffer to HTML",
                "terminal.html", "HTML Files (*.html *.htm);;All Files (*)"
            )
            if not path:
                return
            body = ansi_to_html(text)
            doc = _wrap_html_document(body, title=terminal.title())
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(doc)
            except OSError as e:
                QMessageBox.critical(terminal, "Export Failed", str(e))
        return callback

    def _make_export_text(self, terminal):
        def callback():
            text = self._buffer_text(terminal)
            if not text:
                QMessageBox.information(
                    terminal, "Export Buffer", "Terminal buffer is empty."
                )
                return
            path, _ = QFileDialog.getSaveFileName(
                terminal, "Export Buffer to Plain Text",
                "terminal.txt", "Text Files (*.txt);;All Files (*)"
            )
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(_strip_ansi(text))
            except OSError as e:
                QMessageBox.critical(terminal, "Export Failed", str(e))
        return callback

    def _make_copy_html(self, terminal):
        def callback():
            text = self._selection_or_buffer(terminal)
            if not text:
                return
            html_frag = ansi_to_html(text)
            full_html = _wrap_html_document(html_frag, title="Terminal Selection")
            plain = _strip_ansi(text)
            mime = QMimeData()
            mime.setText(plain)
            mime.setHtml(full_html)
            QApplication.clipboard().setMimeData(mime)
        return callback

    def _make_copy_markdown(self, terminal):
        def callback():
            text = terminal.selected_text() or self._buffer_text(terminal)
            if not text:
                return
            cleaned = _strip_ansi(text)
            md = f"```bash\n{cleaned}\n```"
            QApplication.clipboard().setText(md)
        return callback

    def _make_copy_rich(self, terminal):
        def callback():
            text = self._selection_or_buffer(terminal)
            if not text:
                return
            html_frag = ansi_to_html(text)
            # Rich text: inline monospace/dark styling so target app keeps look.
            rich = (
                '<div style="font-family:monospace; background:#1e1e1e; '
                'color:#d3d7cf; white-space:pre-wrap;">'
                f"{html_frag}</div>"
            )
            plain = _strip_ansi(text)
            mime = QMimeData()
            mime.setText(plain)
            mime.setHtml(rich)
            QApplication.clipboard().setMimeData(mime)
        return callback

    def _make_save_selection(self, terminal):
        def callback():
            text = terminal.selected_text()
            if not text:
                return
            path, _ = QFileDialog.getSaveFileName(
                terminal, "Save Selection as File",
                "selection.txt", "Text Files (*.txt);;All Files (*)"
            )
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(_strip_ansi(text))
            except OSError as e:
                QMessageBox.critical(terminal, "Save Failed", str(e))
        return callback
