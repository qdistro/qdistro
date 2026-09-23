"""Debounced live per-block re-parse for inline markdown formatting.

Reapplies character formats to the current block in place, preserving the
cursor position and selection. Leaves block-level formatting (heading/list/
code) untouched. Uses a small hand-rolled inline scanner rather than going
through the full markdown parser, so it stays fast on every keystroke.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QColor, QFont, QTextBlock, QTextCharFormat, QTextCursor

from .md_to_qdoc import (
    BLOCK_KIND,
    CHAR_CODE,
    CHAR_TAG,
    CHAR_WIKILINK,
    TAG_INLINE_RE,
    WIKILINK_RE,
)


def _base_fmt() -> QTextCharFormat:
    return QTextCharFormat()


def _has_inline_markers(text: str) -> bool:
    return bool(
        "**" in text
        or "~~" in text
        or "`" in text
        or "[[" in text
        or "_" in text
        or "#" in text
        or "](" in text
    )


MD_LINK_RE = __import__("re").compile(r"!?\[([^\]]*)\]\(([^)]+)\)")


def _inline_runs(text: str) -> list[tuple[int, int, QTextCharFormat]]:
    """Return (start, end, fmt) runs in block-text coordinates.

    Very simplified inline parser: handles **bold**, _italic_, ~~strike~~,
    `code`, [[wikilink]], #tag. Does NOT handle markdown [links](url) or
    images — those are left to the full load-path parser."""
    runs: list[tuple[int, int, QTextCharFormat]] = []

    # Wikilinks first (consume their span entirely)
    consumed = [False] * len(text)

    def mark(s: int, e: int) -> None:
        for k in range(s, e):
            if 0 <= k < len(consumed):
                consumed[k] = True

    # Markdown links / images: ![alt](url) and [text](url). Style as link span.
    for m in MD_LINK_RE.finditer(text):
        is_image = m.group(0).startswith("!")
        href = m.group(2).strip()
        f = QTextCharFormat()
        if is_image:
            f.setForeground(QColor("#7c3aed"))
        else:
            f.setForeground(QColor("#1a5fb4"))
            f.setFontUnderline(True)
            f.setAnchor(True)
            f.setAnchorHref(href)
        runs.append((m.start(), m.end(), f))
        mark(m.start(), m.end())

    # Wikilink display text replaces `[[Target|alias]]` with alias or target;
    # but in a WYSIWYG editor the user is TYPING `[[Target]]` literally. So
    # we style the whole `[[...]]` as a wikilink span.
    for m in WIKILINK_RE.finditer(text):
        f = QTextCharFormat()
        f.setAnchor(True)
        target = m.group(1).strip()
        f.setAnchorHref(f"qnotebook:{target}")
        f.setForeground(QColor("#1a5fb4"))
        f.setFontUnderline(True)
        f.setProperty(CHAR_WIKILINK, target)
        runs.append((m.start(), m.end(), f))
        mark(m.start(), m.end())

    # Code spans
    i = 0
    while i < len(text):
        if text[i] == "`" and not consumed[i]:
            j = text.find("`", i + 1)
            if j == -1:
                break
            f = QTextCharFormat()
            f.setFontFamilies(["monospace"])
            f.setBackground(QColor("#f4f4f4"))
            f.setProperty(CHAR_CODE, True)
            runs.append((i, j + 1, f))
            mark(i, j + 1)
            i = j + 1
            continue
        i += 1

    # Bold **...**
    _scan_delim(text, "**", _bold_fmt(), runs, consumed)
    # Strike ~~...~~
    _scan_delim(text, "~~", _strike_fmt(), runs, consumed)
    # Italic _..._  (single char delimiter; must not overlap **)
    _scan_single(text, "_", _italic_fmt(), runs, consumed)

    # Tags
    for m in TAG_INLINE_RE.finditer(text):
        s, e = m.start(), m.end()
        if any(consumed[s:e]):
            continue
        f = QTextCharFormat()
        f.setForeground(QColor("#1c71d8"))
        f.setFontWeight(QFont.Weight.DemiBold)
        f.setProperty(CHAR_TAG, m.group(1))
        runs.append((s, e, f))
        mark(s, e)

    return runs


def _bold_fmt() -> QTextCharFormat:
    f = QTextCharFormat()
    f.setFontWeight(QFont.Weight.Bold)
    return f


def _italic_fmt() -> QTextCharFormat:
    f = QTextCharFormat()
    f.setFontItalic(True)
    return f


def _strike_fmt() -> QTextCharFormat:
    f = QTextCharFormat()
    f.setFontStrikeOut(True)
    return f


def _scan_delim(
    text: str,
    delim: str,
    fmt: QTextCharFormat,
    runs: list[tuple[int, int, QTextCharFormat]],
    consumed: list[bool],
) -> None:
    i = 0
    L = len(delim)
    while True:
        s = text.find(delim, i)
        if s == -1 or s >= len(consumed) or consumed[s]:
            break
        e = text.find(delim, s + L)
        if e == -1:
            break
        if any(consumed[s:e + L]):
            i = s + L
            continue
        # Style only the inner text (not the delimiters), so the serializer
        # sees a bold fragment without the surrounding `**` and re-emits as
        # `**inner**`. Delimiters keep default formatting.
        if e > s + L:
            runs.append((s + L, e, fmt))
        for k in range(s, e + L):
            consumed[k] = True
        i = e + L


def _scan_single(
    text: str,
    delim: str,
    fmt: QTextCharFormat,
    runs: list[tuple[int, int, QTextCharFormat]],
    consumed: list[bool],
) -> None:
    # Only match `_x_` with non-word context on outside to avoid snake_case.
    i = 0
    while i < len(text):
        if text[i] == delim and (i == 0 or not text[i - 1].isalnum()):
            if i < len(consumed) and consumed[i]:
                i += 1
                continue
            e = i + 1
            while e < len(text):
                if text[e] == delim and (e + 1 == len(text) or not text[e + 1].isalnum()):
                    break
                e += 1
            if e >= len(text) or text[e] != delim:
                i += 1
                continue
            if any(consumed[i:e + 1]):
                i = e + 1
                continue
            if e > i + 1:
                runs.append((i + 1, e, fmt))
            for k in range(i, e + 1):
                consumed[k] = True
            i = e + 1
            continue
        i += 1


class LiveReparser(QObject):
    """Debounced current-block re-styler."""

    def __init__(self, editor, delay_ms: int = 200) -> None:
        super().__init__(editor)
        self.editor = editor
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._do_reparse)
        editor.textChanged.connect(self._on_text_changed)
        self._enabled = True

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        if not on:
            self._timer.stop()

    def _on_text_changed(self) -> None:
        if not self._enabled:
            return
        if getattr(self.editor, "_loading", False):
            return
        self._timer.start()

    def _do_reparse(self) -> None:
        if not self._enabled:
            return
        block = self.editor.textCursor().block()
        self.reparse_block(block)

    def reparse_block(self, block: QTextBlock) -> None:
        """Reapply inline char formats to `block`, preserving cursor + selection."""
        if not block.isValid():
            return
        # Don't mess with code blocks (already handled on load).
        kind = block.blockFormat().property(BLOCK_KIND) or "p"
        if kind in ("code", "hr"):
            return
        text = block.text()
        if not text:
            return
        # Only reparse when the block contains raw markdown markers; otherwise
        # the load-time parser has already styled the run and we'd lose it.
        if not _has_inline_markers(text):
            return

        # Save cursor/selection
        ed = self.editor
        user_cur = ed.textCursor()
        anchor = user_cur.anchor()
        pos = user_cur.position()

        # Block against modification signals storm
        ed.blockSignals(True)
        doc = ed.document()
        was_modified = doc.isModified()
        try:
            runs = _inline_runs(text)
            # Clear prior inline formatting on this block, then apply runs.
            cur = QTextCursor(block)
            cur.setPosition(block.position())
            cur.setPosition(block.position() + len(text), QTextCursor.MoveMode.KeepAnchor)
            clear_fmt = QTextCharFormat()
            cur.setCharFormat(clear_fmt)
            for s, e, fmt in runs:
                c = QTextCursor(block)
                c.setPosition(block.position() + s)
                c.setPosition(block.position() + e, QTextCursor.MoveMode.KeepAnchor)
                c.mergeCharFormat(fmt)
        finally:
            doc.setModified(was_modified)
            ed.blockSignals(False)

        # Restore cursor + selection
        new_cur = QTextCursor(doc)
        new_cur.setPosition(anchor)
        new_cur.setPosition(pos, QTextCursor.MoveMode.KeepAnchor)
        ed.setTextCursor(new_cur)
