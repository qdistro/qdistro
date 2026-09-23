"""Smart clipboard plugin for QTerminator.

Adds context menu items for enhanced clipboard operations:
paste with escaping, single-line paste, paste with confirmation,
copy as path, copy as markdown code block, copy without ANSI colors.
"""

import os
import re
import shlex

from PyQt6.QtWidgets import QApplication, QMessageBox

from qterminator.plugin import MenuProvider

# Regex matching ANSI escape sequences (CSI, OSC, and simple escapes)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]")


class SmartClipboardPlugin(MenuProvider):
    name = "smart_clipboard"
    description = "Smart clipboard operations in the context menu"
    version = "1.0"
    category = "Edit"

    def get_menu_items(self, terminal):
        items = []

        # -- Paste operations (always shown) --
        items.append(("Paste Escaped", self._make_paste_escaped(terminal)))
        items.append(("Paste as Single Line", self._make_paste_single_line(terminal)))
        items.append((
            "Paste with Confirmation",
            self._make_paste_with_confirmation(terminal),
        ))

        # -- Copy operations (only when text is selected) --
        if terminal.selected_text():
            items.append(("Copy Path", self._make_copy_path(terminal)))
            items.append((
                "Copy as Markdown Code Block",
                self._make_copy_markdown(terminal),
            ))
            items.append(("Copy Without Colors", self._make_copy_no_colors(terminal)))

        return items

    # -- Paste callbacks --

    def _make_paste_escaped(self, terminal):
        def callback():
            text = QApplication.clipboard().text()
            if not text:
                return
            escaped = shlex.quote(text)
            terminal.send_text(escaped)
        return callback

    def _make_paste_single_line(self, terminal):
        def callback():
            text = QApplication.clipboard().text()
            if not text:
                return
            single = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
            terminal.send_text(single)
        return callback

    def _make_paste_with_confirmation(self, terminal):
        def callback():
            text = QApplication.clipboard().text()
            if not text:
                return
            preview = text[:200]
            if len(text) > 200:
                preview += "..."
            result = QMessageBox.question(
                terminal,
                "Paste Confirmation",
                f"Paste the following text?\n\n{preview}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result == QMessageBox.StandardButton.Yes:
                terminal.send_text(text)
        return callback

    # -- Copy callbacks --

    def _make_copy_path(self, terminal):
        def callback():
            text = terminal.selected_text()
            if not text:
                return
            path = text.strip()
            path = os.path.expanduser(path)
            QApplication.clipboard().setText(path)
        return callback

    def _make_copy_markdown(self, terminal):
        def callback():
            text = terminal.selected_text()
            if not text:
                return
            result = f"```\n{text}\n```"
            QApplication.clipboard().setText(result)
        return callback

    def _make_copy_no_colors(self, terminal):
        def callback():
            text = terminal.selected_text()
            if not text:
                return
            cleaned = _ANSI_RE.sub("", text)
            QApplication.clipboard().setText(cleaned)
        return callback
