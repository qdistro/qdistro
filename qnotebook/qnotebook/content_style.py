"""Document (content) presentation, independent of chrome UI fonts.

Semantic identity lives on QTextFormat user properties. This module only
applies view presentation: family, point size, and readable code/link
colors. It must not serialize, dirty the document, or push undo commands.
Export keeps :func:`legacy_content_style` without live palette colors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from PyQt6.QtGui import (
    QColor,
    QFont,
    QPalette,
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)

from .md_to_qdoc import BLOCK_KIND, BLOCK_LEVEL, CHAR_CODE

_HEX_COLOR = re.compile(r"^#[0-9a-f]{6}$")

# Legacy heading sizes were 20/17/15/13/12/11 on an 11 pt body.
_HEADING_RATIO = {1: 20 / 11, 2: 17 / 11, 3: 15 / 11, 4: 13 / 11, 5: 12 / 11, 6: 1.0}


@dataclass(frozen=True)
class ContentStyle:
    body_family: str
    body_point_size: float
    code_family: str
    code_point_size: float
    inherit_desktop: bool = False
    code_background: str | None = None
    link_color: str | None = None

    def heading_point_size(self, level: int) -> float:
        return round(self.body_point_size * _HEADING_RATIO.get(int(level), 1.0), 2)

    def body_qfont(self) -> QFont:
        font = QFont()
        if self.body_family:
            font.setFamily(self.body_family)
        font.setPointSizeF(self.body_point_size)
        return font


def _hex_color(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if _HEX_COLOR.fullmatch(text):
        return text
    return None


def document_palette_colors() -> tuple[str | None, str | None]:
    """Code-background and link colors for dark/light readability.

    Independent of the document-font opt-in. A missing controller leaves
    parse-time hardcoded colors in place. Snapshot colors apply when the
    shared palette is active; otherwise the application palette is used
    (explicit Dark/Light/Native, or a missing snapshot).
    """
    from PyQt6.QtWidgets import QApplication

    from .theme import current_controller

    ctrl = current_controller()
    if ctrl is None:
        return None, None
    try:
        state = ctrl.state
    except Exception:  # noqa: BLE001
        state = None
    if state is not None and state.colors is not None:
        bg = _hex_color(state.colors.mSurfaceVariant)
        link = _hex_color(state.colors.mPrimary)
        if bg or link:
            return bg, link
    app = QApplication.instance()
    if app is None:
        return None, None
    pal = app.palette()
    return (
        _hex_color(pal.color(QPalette.ColorRole.AlternateBase).name()),
        _hex_color(pal.color(QPalette.ColorRole.Link).name()),
    )


def with_document_palette(style: ContentStyle) -> ContentStyle:
    bg, link = document_palette_colors()
    if bg is None and link is None:
        return style
    return replace(
        style,
        code_background=bg if bg is not None else style.code_background,
        link_color=link if link is not None else style.link_color,
    )


def legacy_content_style() -> ContentStyle:
    from .editor import BODY_POINT_SIZE, native_body_font

    body = native_body_font()
    family = body.family() or QFont().family()
    return ContentStyle(
        body_family=family,
        body_point_size=float(BODY_POINT_SIZE),
        code_family="monospace",
        code_point_size=float(BODY_POINT_SIZE),
        inherit_desktop=False,
    )


def resolve_content_style() -> ContentStyle:
    """Native body/code fonts unless the document-font opt-in is on.

    Code/link colors follow the active presentation or application
    palette even when fonts stay native. Export must call
    :func:`legacy_content_style` directly.
    """
    from .appearance import default_settings, load_use_desktop_document_fonts

    if load_use_desktop_document_fonts(default_settings()):
        style = desktop_content_style()
    else:
        style = legacy_content_style()
    return with_document_palette(style)


def desktop_content_style() -> ContentStyle:
    """Shared proportional/fixed content sizes; UI scale is not document scale."""
    from .theme import current_controller

    legacy = legacy_content_style()
    ctrl = current_controller()
    if ctrl is None:
        return legacy
    try:
        state = ctrl.state
    except Exception:  # noqa: BLE001
        return legacy
    if state is None or not state.desktop_available:
        return legacy
    return with_document_palette(
        ContentStyle(
            body_family=state.ui_family or legacy.body_family,
            body_point_size=float(state.content_ui_point_size or legacy.body_point_size),
            code_family=state.fixed_family or legacy.code_family,
            code_point_size=float(state.content_fixed_point_size or legacy.code_point_size),
            inherit_desktop=True,
        )
    )


class ContentPresentationHighlighter(QSyntaxHighlighter):
    """Paint inherited document fonts without mutating the QTextDocument."""

    def __init__(self, doc: QTextDocument | None = None) -> None:
        super().__init__(doc)
        self._style: ContentStyle | None = None

    def set_style(self, style: ContentStyle | None) -> None:
        self._style = style
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        highlight_block_content(self, text, self._style)


def highlight_block_content(highlighter: QSyntaxHighlighter, text: str, style: ContentStyle | None) -> None:
    # Always paint the current style (legacy or desktop). Returning only when
    # inherit_desktop is true would leave parse-time desktop fonts on screen
    # after the user turns the opt-in off.
    if style is None:
        return
    from .equations import EQ_LATEX

    block = highlighter.currentBlock()
    kind = str(block.blockFormat().property(BLOCK_KIND) or "p")
    level = int(block.blockFormat().property(BLOCK_LEVEL) or 1)
    block_pos = block.position()
    # Fragment geometry is UTF-16. Do not clamp with len(text) (Python
    # code points): a supplementary character is one Python char and two
    # QTextDocument units, which would skip the rest of the block.
    content_len = max(0, block.length() - 1)
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid() and frag.length() > 0 and not frag.charFormat().isImageFormat():
            rel = frag.position() - block_pos
            if rel >= 0:
                length = min(frag.length(), max(0, content_len - rel))
                if length > 0:
                    highlighter.setFormat(
                        rel,
                        length,
                        _presented_char_format(frag.charFormat(), kind, level, style, EQ_LATEX),
                    )
        it += 1


def apply_document_presentation(doc: QTextDocument, style: ContentStyle) -> None:
    """Bake fonts into a document used for parse or export.

    Disabling undo/redo clears the stack (Qt); do not call this on a live
    editor document. Live restyle uses ContentPresentationHighlighter.
    """
    from .equations import EQ_LATEX

    ranges: list[tuple[int, int, QTextCharFormat]] = []
    block_updates: list[tuple[int, object]] = []
    block = doc.firstBlock()
    while block.isValid():
        bfmt = block.blockFormat()
        kind = str(bfmt.property(BLOCK_KIND) or "p")
        level = int(bfmt.property(BLOCK_LEVEL) or 1)
        if kind == "code" and style.code_background:
            new_block = QTextBlockFormat(bfmt)
            new_block.setBackground(QColor(style.code_background))
            block_updates.append((block.position(), new_block))
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.length() > 0:
                fmt = frag.charFormat()
                if not fmt.isImageFormat():
                    ranges.append(
                        (
                            frag.position(),
                            frag.position() + frag.length(),
                            _presented_char_format(fmt, kind, level, style, EQ_LATEX),
                        )
                    )
            it += 1
        block = block.next()

    modified = doc.isModified()
    undo = doc.isUndoRedoEnabled()
    doc.setUndoRedoEnabled(False)
    cursor = QTextCursor(doc)
    for start, end, fmt in ranges:
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(fmt)
    for pos, block_fmt in block_updates:
        cursor.setPosition(pos)
        cursor.setBlockFormat(block_fmt)
    doc.setDefaultFont(style.body_qfont())
    doc.setUndoRedoEnabled(undo)
    doc.setModified(modified)


def _presented_char_format(
    fmt: QTextCharFormat,
    kind: str,
    level: int,
    style: ContentStyle,
    eq_latex_prop,
) -> QTextCharFormat:
    new = QTextCharFormat(fmt)
    is_code = bool(fmt.property(CHAR_CODE)) or kind == "code" or bool(fmt.property(eq_latex_prop))
    if is_code:
        new.setFontFamilies([style.code_family])
        new.setFontPointSize(style.code_point_size)
        # Equations keep their gold chip; fenced blocks may lack CHAR_CODE.
        if style.code_background and not fmt.property(eq_latex_prop) and (
            bool(fmt.property(CHAR_CODE)) or kind == "code"
        ):
            new.setBackground(QColor(style.code_background))
    elif kind == "h":
        new.setFontFamilies([style.body_family])
        new.setFontPointSize(style.heading_point_size(level))
    else:
        new.setFontFamilies([style.body_family])
        new.setFontPointSize(style.body_point_size)
    if style.link_color and new.isAnchor() and not new.anchorHref().startswith("qnotebook:"):
        new.setForeground(QColor(style.link_color))
    return new
