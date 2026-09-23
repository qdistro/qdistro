"""Text transformation plugin for QTerminator.

Adds context menu items for common text transformations on selected text:
base64, JSON, URL encoding, hashing, case conversion, line operations, etc.
"""

import base64
import datetime
import hashlib
import json
import shlex
import urllib.parse

from PyQt6.QtWidgets import QMessageBox

from qterminator.plugin import MenuProvider


class TextTransformsPlugin(MenuProvider):
    name = "text_transforms"
    description = "Text transformation tools in the context menu"
    version = "1.0"
    category = "Transform"

    def get_menu_items(self, terminal):
        text = terminal.selected_text()
        if not text:
            return []

        def _apply(transform):
            """Return a callback that transforms selected text and sends it."""
            def callback():
                selected = terminal.selected_text()
                if not selected:
                    return
                try:
                    result = transform(selected)
                except Exception as exc:
                    QMessageBox.warning(
                        terminal, "Transform Error", str(exc)
                    )
                    return
                if result is not None:
                    terminal.send_text(result)
            return callback

        items = [
            ("Base64 Decode", _apply(self._base64_decode)),
            ("Base64 Encode", _apply(self._base64_encode)),
            ("JSON Pretty-Print", _apply(self._json_pretty)),
            ("URL Decode", _apply(self._url_decode)),
            ("URL Encode", _apply(self._url_encode)),
            ("Hex to ASCII", _apply(self._hex_to_ascii)),
            ("Epoch to Date", _apply(self._epoch_to_date)),
            ("MD5 Hash", _apply(self._md5)),
            ("SHA256 Hash", _apply(self._sha256)),
            ("Shell Escape", _apply(self._shell_escape)),
            ("Uppercase", _apply(lambda t: t.upper())),
            ("Lowercase", _apply(lambda t: t.lower())),
            ("Title Case", _apply(lambda t: t.title())),
            ("Sort Lines", _apply(self._sort_lines)),
            ("Unique Lines", _apply(self._unique_lines)),
            ("Count Lines/Words/Chars", self._make_count_callback(terminal)),
            ("Trim Whitespace", _apply(self._trim_whitespace)),
            ("Reverse", _apply(lambda t: t[::-1])),
        ]
        return items

    # -- Transform methods --

    @staticmethod
    def _base64_decode(text):
        return base64.b64decode(text.strip()).decode("utf-8")

    @staticmethod
    def _base64_encode(text):
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    @staticmethod
    def _json_pretty(text):
        obj = json.loads(text)
        return json.dumps(obj, indent=2, ensure_ascii=False)

    @staticmethod
    def _url_decode(text):
        return urllib.parse.unquote(text)

    @staticmethod
    def _url_encode(text):
        return urllib.parse.quote(text, safe="")

    @staticmethod
    def _hex_to_ascii(text):
        cleaned = text.strip().replace(" ", "").replace("0x", "").replace("\\x", "")
        return bytes.fromhex(cleaned).decode("ascii")

    @staticmethod
    def _epoch_to_date(text):
        ts = float(text.strip())
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")

    @staticmethod
    def _md5(text):
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _sha256(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _shell_escape(text):
        return shlex.quote(text)

    @staticmethod
    def _sort_lines(text):
        lines = text.splitlines()
        lines.sort()
        return "\n".join(lines)

    @staticmethod
    def _unique_lines(text):
        seen = set()
        result = []
        for line in text.splitlines():
            if line not in seen:
                seen.add(line)
                result.append(line)
        return "\n".join(result)

    @staticmethod
    def _trim_whitespace(text):
        return "\n".join(line.strip() for line in text.splitlines())

    def _make_count_callback(self, terminal):
        """Return a callback that shows line/word/char stats in a message box."""
        def callback():
            selected = terminal.selected_text()
            if not selected:
                return
            lines = len(selected.splitlines())
            words = len(selected.split())
            chars = len(selected)
            QMessageBox.information(
                terminal,
                "Text Statistics",
                f"Lines: {lines}\nWords: {words}\nChars: {chars}",
            )
        return callback
