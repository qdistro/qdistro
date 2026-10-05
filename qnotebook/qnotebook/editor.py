"""Single-surface WYSIWYG markdown editor."""

from __future__ import annotations

import json
import re
from pathlib import Path

from PyQt6.QtCore import QByteArray, QMimeData, QStringListModel, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QMouseEvent,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextImageFormat,
)
from PyQt6.QtWidgets import QCompleter, QTextEdit

from .md_to_qdoc import (
    BLOCK_KIND,
    BLOCK_LEVEL,
    BLOCK_TASK_STATE,
    CHAR_CODE,
    CHAR_IMAGE_ALT,
    CHAR_STRONG,
    CHAR_WIKILINK,
    IMAGE_MAX_WIDTH,
    markdown_to_qdoc,
    register_image_resource,
)
from .qdoc_to_md import qdoc_to_markdown

BODY_POINT_SIZE = 11
_PINNED_BODY_FONT: QFont | None = None


def pin_native_body_font(font: QFont) -> None:
    """Remember the platform body family captured before app.setFont."""
    global _PINNED_BODY_FONT
    pinned = QFont(font)
    pinned.setPointSize(BODY_POINT_SIZE)
    _PINNED_BODY_FONT = pinned


def native_body_font() -> QFont:
    """11 pt native body family; never the shared UI chrome font."""
    if _PINNED_BODY_FONT is not None:
        return QFont(_PINNED_BODY_FONT)
    base = QFont()
    base.setPointSize(BODY_POINT_SIZE)
    return base


def reset_pinned_body_font_for_tests() -> None:
    global _PINNED_BODY_FONT
    _PINNED_BODY_FONT = None


class MarkdownEditor(QTextEdit):
    """WYSIWYG markdown editor.

    - `load(md_text)` populates the document.
    - `text()` re-serializes to markdown.
    - `linkActivated(target)` fires on click on a link (wikilink target without
      the `qnotebook:` prefix, or the URL for external links).
    - `dirtyChanged(bool)` fires when the dirty flag flips.
    """

    linkActivated = pyqtSignal(str)
    dirtyChanged = pyqtSignal(bool)
    imageDropped = pyqtSignal(str)  # absolute source path
    imagePasted = pyqtSignal(object)  # QImage
    fileDropped = pyqtSignal(str)  # absolute source path (non-image)
    autoSaveRequested = pyqtSignal()
    escapePressed = pyqtSignal()
    zoomStepRequested = pyqtSignal(int)  # +1 / -1 (Ctrl+wheel)

    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
    _WHEEL_NOTCH = 120  # angleDelta units per zoom step
    _WHEEL_PIXELS = 60  # pixelDelta per zoom step (touchpads)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setTabChangesFocus(True)
        self.setFont(native_body_font())
        self._dirty = False
        self._loading = False
        self._current_path: str | None = None
        self._base_path: Path | None = None
        self.document().modificationChanged.connect(self._on_modification_changed)
        # Auto-save: idle timer (restarts on every text edit)
        self._autosave_ms = 30_000
        self._autosave_enabled = True
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._emit_autosave)
        self.textChanged.connect(self._on_text_changed_for_autosave)
        # Autocomplete state
        self._completer: QCompleter | None = None
        self._completer_mode: str | None = None  # "wiki" | "tag"
        self._completer_prefix_start: int = -1
        self._all_pages: list[str] = []
        self._all_tags: list[str] = []
        self._setup_completer()
        from .live_reparse import LiveReparser
        self._live_reparser = LiveReparser(self, delay_ms=200)
        from .content_style import ContentPresentationHighlighter

        self._content_style = None
        self._content_highlighter = ContentPresentationHighlighter(self.document())
        self._spell_highlighter = None
        self._external_selections: list[QTextEdit.ExtraSelection] = []
        self._code_selections: list[QTextEdit.ExtraSelection] = []

    def set_live_reparse_enabled(self, on: bool) -> None:
        self._live_reparser.set_enabled(on)

    def live_reparse_now(self) -> None:
        self._live_reparser._do_reparse()

    def set_autosave_interval_ms(self, ms: int) -> None:
        self._autosave_ms = int(ms)

    def set_autosave_enabled(self, enabled: bool) -> None:
        self._autosave_enabled = enabled
        if not enabled:
            self._autosave_timer.stop()

    def _on_text_changed_for_autosave(self) -> None:
        if self._loading or not self._autosave_enabled:
            return
        if self._dirty:
            self._autosave_timer.start(self._autosave_ms)

    def _emit_autosave(self) -> None:
        if self._dirty and self._autosave_enabled:
            self.autoSaveRequested.emit()

    def focusOutEvent(self, e) -> None:
        if self._dirty and self._autosave_enabled:
            self.autoSaveRequested.emit()
        super().focusOutEvent(e)

    # ---- load / save text ----

    def load_markdown(
        self,
        md_text: str,
        page_path: str | None = None,
        base_path: Path | None = None,
        transclusion_resolver=None,
    ) -> None:
        self._loading = True
        try:
            from .content_style import resolve_content_style

            markdown_to_qdoc(
                md_text or "", self.document(), base_path=base_path,
                transclusion_resolver=transclusion_resolver,
                content_style=resolve_content_style(),
            )
            self.apply_content_presentation()
            self.document().setModified(False)
            self._current_path = page_path
            self._base_path = base_path
            self._set_dirty(False)
        finally:
            self._loading = False

    def markdown(self) -> str:
        return qdoc_to_markdown(self.document())

    # Backwards-compatible alias
    def text(self) -> str:
        return self.markdown()

    # ---- dirty tracking ----

    def is_dirty(self) -> bool:
        return self._dirty

    def clear_dirty(self) -> None:
        self.document().setModified(False)
        self._set_dirty(False)

    def _set_dirty(self, value: bool) -> None:
        if self._dirty != value:
            self._dirty = value
            self.dirtyChanged.emit(value)

    def _on_modification_changed(self, modified: bool) -> None:
        if self._loading:
            return
        self._set_dirty(modified)

    # ---- autocomplete ----

    def _setup_completer(self) -> None:
        c = QCompleter(self)
        c.setWidget(self)
        c.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        c.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        c.setModel(QStringListModel([], self))
        c.activated.connect(self._on_completer_activated)
        self._completer = c

    def set_completion_sources(self, pages: list[str], tags: list[str]) -> None:
        self._all_pages = list(pages)
        self._all_tags = list(tags)

    def _active_prefix(self) -> tuple[str, str, int] | None:
        """Return (mode, prefix, start_pos) if cursor is inside a completable token.

        - wiki: starts when we see `[[` to the left of cursor on the current line,
          and there's no `]]` between `[[` and cursor.
        - tag: starts when we see `#` preceded by start-of-word, and no whitespace
          between `#` and cursor.
        """
        cursor = self.textCursor()
        block_text = cursor.block().text()
        pos_in_block = cursor.positionInBlock()
        before = block_text[:pos_in_block]
        # wiki
        idx = before.rfind("[[")
        if idx != -1:
            between = before[idx + 2:]
            if "]]" not in between and "\n" not in between and "[[" not in between:
                return ("wiki", between, cursor.position() - len(between))
        # tag
        m = None
        for mm in re.finditer(r"(?:(?<=^)|(?<=[\s(\[]))#([A-Za-z][\w-]*)", before):
            m = mm
        if m is not None and m.end() == len(before):
            prefix = m.group(1)
            return ("tag", prefix, cursor.position() - len(prefix))
        return None

    def _update_completer(self) -> None:
        if self._completer is None:
            return
        info = self._active_prefix()
        if info is None:
            self._completer.popup().hide()
            self._completer_mode = None
            return
        mode, prefix, start = info
        source = self._all_pages if mode == "wiki" else self._all_tags
        if not source:
            self._completer.popup().hide()
            return
        self._completer_mode = mode
        self._completer_prefix_start = start
        self._completer.model().setStringList(source)
        self._completer.setCompletionPrefix(prefix)
        popup = self._completer.popup()
        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(
            popup.sizeHintForColumn(0)
            + popup.verticalScrollBar().sizeHint().width()
        )
        self._completer.complete(rect)

    def _on_completer_activated(self, text: str) -> None:
        if self._completer_mode is None or self._completer_prefix_start < 0:
            return
        cur = self.textCursor()
        # Replace from prefix_start to current cursor with the full completion.
        cur.setPosition(self._completer_prefix_start)
        cur.movePosition(
            QTextCursor.MoveOperation.Right,
            QTextCursor.MoveMode.KeepAnchor,
            self.textCursor().position() - self._completer_prefix_start,
        )
        cur.insertText(text)
        if self._completer_mode == "wiki":
            # Ensure closing ]] present after the insertion
            pos = cur.position()
            block = cur.block()
            block_text = block.text()
            pos_in_block = pos - block.position()
            after = block_text[pos_in_block:]
            if not after.startswith("]]"):
                cur.insertText("]]")
        self._completer_mode = None

    def wheelEvent(self, e) -> None:  # noqa: N802 (Qt override)
        # QTextEdit's own Ctrl+wheel zoom changes the widget font only, which
        # explicit fragment sizes ignore; route it to the editor zoom instead.
        # Wheels report angleDelta (120 per notch, less on high-resolution
        # wheels); touchpads may report only pixelDelta. Accumulate either
        # and step once per notch-equivalent so neither is lost or too fast.
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            angle = e.angleDelta().y()
            if angle:
                delta, threshold = angle, self._WHEEL_NOTCH
            else:
                delta, threshold = e.pixelDelta().y(), self._WHEEL_PIXELS
            if delta:
                acc = getattr(self, "_zoom_wheel_acc", 0)
                if (acc > 0) != (delta > 0):
                    acc = 0  # direction changed
                acc += delta
                while abs(acc) >= threshold:
                    step = 1 if acc > 0 else -1
                    acc -= step * threshold
                    self.zoomStepRequested.emit(step)
                self._zoom_wheel_acc = acc
            e.accept()
            return
        super().wheelEvent(e)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        popup_visible = (
            self._completer is not None
            and self._completer.popup().isVisible()
        )
        if popup_visible and e.key() in (
            Qt.Key.Key_Enter,
            Qt.Key.Key_Return,
            Qt.Key.Key_Tab,
        ):
            idx = self._completer.popup().currentIndex()
            if idx.isValid():
                self._on_completer_activated(idx.data())
                self._completer.popup().hide()
                e.accept()
                return
        if popup_visible and e.key() == Qt.Key.Key_Escape:
            self._completer.popup().hide()
            e.accept()
            return
        if e.key() == Qt.Key.Key_Escape:
            self.escapePressed.emit()
        super().keyPressEvent(e)
        if self._loading:
            return
        self._update_completer()

    # ---- formatting toggles ----

    def toggle_bold(self) -> None:
        cur = self.textCursor()
        current = cur.charFormat()
        heading = str(cur.blockFormat().property(BLOCK_KIND) or "") == "h"
        explicit = bool(current.property(CHAR_STRONG))
        fmt = QTextCharFormat()
        if heading:
            on = not explicit
            fmt.setProperty(CHAR_STRONG, on)
            fmt.setFontWeight(QFont.Weight.Bold)
        else:
            on = not (explicit or current.fontWeight() >= QFont.Weight.Bold)
            fmt.setProperty(CHAR_STRONG, on)
            fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
        self._apply_char_format(fmt)

    def toggle_italic(self) -> None:
        cur = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setFontItalic(not cur.charFormat().fontItalic())
        self._apply_char_format(fmt)

    def toggle_strike(self) -> None:
        cur = self.textCursor()
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(not cur.charFormat().fontStrikeOut())
        self._apply_char_format(fmt)

    def _code_char_format(self) -> QTextCharFormat:
        """CHAR_CODE format with the family/background a presentation restyle
        gives code spans; the light literal is only the no-controller fallback."""
        from .content_style import document_palette

        fmt = QTextCharFormat()
        fmt.setProperty(CHAR_CODE, True)
        style = self._content_style
        fmt.setFontFamilies([style.code_family if style is not None else "monospace"])
        background = (
            (style.code_background if style is not None else None)
            or document_palette().code_background
            or "#f4f4f4"
        )
        fmt.setBackground(QColor(background))
        return fmt

    def toggle_code(self) -> None:
        cur = self.textCursor()
        on = not bool(cur.charFormat().property(CHAR_CODE))
        if on:
            fmt = self._code_char_format()
        else:
            fmt = QTextCharFormat()
            fmt.setProperty(CHAR_CODE, False)
            fmt.setFontFamilies([self.font().family()])
            fmt.setBackground(QBrush())
        self._apply_char_format(fmt)

    def set_heading(self, level: int) -> None:
        """Set (or clear with level=0) heading level for the cursor block or selection."""
        cur = self.textCursor()
        doc = self.document()
        now_heading = level > 0
        if cur.hasSelection():
            start = min(cur.selectionStart(), cur.selectionEnd())
            end = max(cur.selectionStart(), cur.selectionEnd())
        else:
            start = end = cur.position()
        start_block = doc.findBlock(start)
        end_block = doc.findBlock(end)
        if (
            cur.hasSelection()
            and end_block.isValid()
            and end_block.position() == end
            and end_block.blockNumber() > start_block.blockNumber()
        ):
            end_block = end_block.previous()
        positions: list[int] = []
        block = start_block
        while block.isValid() and block.blockNumber() <= end_block.blockNumber():
            positions.append(block.position())
            block = block.next()

        grouped = QTextCursor(doc)
        grouped.beginEditBlock()
        try:
            for pos in positions:
                block = doc.findBlock(pos)
                if not block.isValid():
                    continue
                was_heading = str(block.blockFormat().property(BLOCK_KIND) or "") == "h"
                block_fmt = block.blockFormat()
                if level == 0:
                    block_fmt.setHeadingLevel(0)
                    block_fmt.setProperty(BLOCK_KIND, "p")
                    block_fmt.setProperty(BLOCK_LEVEL, 0)
                else:
                    block_fmt.setHeadingLevel(level)
                    block_fmt.setProperty(BLOCK_KIND, "h")
                    block_fmt.setProperty(BLOCK_LEVEL, level)
                block_cur = QTextCursor(block)
                block_cur.setBlockFormat(block_fmt)
                if was_heading != now_heading:
                    self._sync_heading_default_weight(doc.findBlock(pos), heading=now_heading)
        finally:
            grouped.endEditBlock()

    def _sync_heading_default_weight(self, block, heading: bool) -> None:
        """Heading default is Bold without CHAR_STRONG; paragraphs must not inherit it."""
        ranges: list[tuple[int, int, QTextCharFormat]] = []
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.length() > 0 and not frag.charFormat().isImageFormat():
                fmt = QTextCharFormat(frag.charFormat())
                strong = bool(fmt.property(CHAR_STRONG))
                fmt.setFontWeight(
                    QFont.Weight.Bold if heading or strong else QFont.Weight.Normal
                )
                ranges.append((frag.position(), frag.position() + frag.length(), fmt))
            it += 1
        cursor = QTextCursor(self.document())
        for start, end, fmt in ranges:
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(fmt)

    def setExtraSelections(self, selections) -> None:  # noqa: N802
        """Keep search highlights without dropping fenced-code restyle."""
        self._external_selections = list(selections)
        self._publish_extra_selections()

    def apply_content_presentation(self) -> None:
        """Paint inherited document fonts without dirtying or touching undo."""
        from .appearance import load_editor_zoom
        from .content_style import resolve_content_style, zoomed

        percent = load_editor_zoom()
        base = resolve_content_style()
        style = zoomed(base, percent)
        self._content_style = style
        font = base.body_qfont() if base.inherit_desktop else native_body_font()
        if percent != 100 and font.pointSizeF() > 0:
            font.setPointSizeF(round(font.pointSizeF() * percent / 100.0, 2))
        self.setFont(font)
        spell = getattr(self, "_spell_highlighter", None)
        if spell is not None:
            set_style = getattr(spell, "set_content_style", None)
            if callable(set_style):
                set_style(style)
            else:
                spell.rehighlight()
        elif getattr(self, "_content_highlighter", None) is not None:
            self._content_highlighter.set_style(style)
        self._rebuild_code_block_selections()

    def _rebuild_code_block_selections(self) -> None:
        """Full-width extras cover empty fenced lines the highlighter cannot."""
        style = self._content_style
        bg = getattr(style, "code_background", None) if style is not None else None
        color = QColor(bg) if bg else QColor()
        out: list[QTextEdit.ExtraSelection] = []
        if color.isValid():
            block = self.document().firstBlock()
            while block.isValid():
                if str(block.blockFormat().property(BLOCK_KIND) or "") == "code":
                    sel = QTextEdit.ExtraSelection()
                    fmt = QTextCharFormat()
                    fmt.setBackground(color)
                    fmt.setProperty(int(QTextFormat.Property.FullWidthSelection), True)
                    sel.format = fmt
                    sel.cursor = QTextCursor(block)
                    out.append(sel)
                block = block.next()
        self._code_selections = out
        self._publish_extra_selections()

    def _publish_extra_selections(self) -> None:
        super().setExtraSelections(
            list(self._code_selections) + list(self._external_selections)
        )

    def _apply_char_format(self, fmt: QTextCharFormat) -> None:
        cur = self.textCursor()
        if cur.hasSelection():
            cur.mergeCharFormat(fmt)
        else:
            self.mergeCurrentCharFormat(fmt)

    # ---- link handling ----

    def _link_at(self, pos) -> str | None:
        cur = self.cursorForPosition(pos)
        cfmt = cur.charFormat()
        # Footnote reference click: scroll to the matching definition block.
        from .md_to_qdoc import BLOCK_FOOTNOTE_DEF, CHAR_FOOTNOTE_REF
        fn_ref = cfmt.property(CHAR_FOOTNOTE_REF)
        if fn_ref:
            doc = self.document()
            block = doc.firstBlock()
            while block.isValid():
                if str(block.blockFormat().property(BLOCK_FOOTNOTE_DEF) or "") == str(fn_ref):
                    c = QTextCursor(block)
                    self.setTextCursor(c)
                    self.ensureCursorVisible()
                    break
                block = block.next()
            return None
        wikilink = cfmt.property(CHAR_WIKILINK)
        if wikilink:
            return str(wikilink)
        href = cfmt.anchorHref()
        if href:
            if href.startswith("qnotebook:"):
                return href[len("qnotebook:"):]
            return href
        return None

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        link = self._link_at(e.pos())
        if link:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            modifiers = e.modifiers()
            # Ctrl+click OR plain click on wikilink fires linkActivated.
            link = self._link_at(e.pos())
            if link and (modifiers & Qt.KeyboardModifier.ControlModifier or self._is_wikilink_at(e.pos())):
                self.linkActivated.emit(link)
                return
            # Toggle task-list checkbox when clicking at column 0 of a task block
            if self._maybe_toggle_task(e.pos()):
                return
        super().mousePressEvent(e)

    def _is_wikilink_at(self, pos) -> bool:
        cur = self.cursorForPosition(pos)
        return bool(cur.charFormat().property(CHAR_WIKILINK))

    def _maybe_toggle_task(self, pos) -> bool:
        cur = self.cursorForPosition(pos)
        block = cur.block()
        bfmt = block.blockFormat()
        if (bfmt.property(BLOCK_KIND) or "") != "task":
            return False
        # Toggle state
        state = int(bfmt.property(BLOCK_TASK_STATE) or 0)
        bfmt.setProperty(BLOCK_TASK_STATE, 0 if state else 1)
        c = QTextCursor(block)
        c.setBlockFormat(bfmt)
        return True

    # ---- images ----

    def insert_image(self, rel_path: str, alt: str, abs_path: str | None = None) -> None:
        """Insert an image fragment at the cursor. `rel_path` is stored so
        serialize can emit `![alt](rel_path)`. `abs_path` (when supplied)
        registers the actual pixels as a document resource so it renders."""
        register_image_resource(self.document(), rel_path, abs_path)
        img_fmt = QTextImageFormat()
        img_fmt.setName(rel_path)
        img_fmt.setProperty(CHAR_IMAGE_ALT, alt)
        # Width cap with aspect ratio preservation
        if abs_path:
            img = QImage(abs_path)
            if not img.isNull():
                w = img.width()
                h = img.height()
                if w > IMAGE_MAX_WIDTH:
                    ratio = IMAGE_MAX_WIDTH / float(w)
                    img_fmt.setWidth(IMAGE_MAX_WIDTH)
                    img_fmt.setHeight(h * ratio)
                else:
                    img_fmt.setWidth(w)
                    img_fmt.setHeight(h)
        cur = self.textCursor()
        cur.insertImage(img_fmt)

    def dragEnterEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            e.acceptProposedAction()
            return
        super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            e.acceptProposedAction()
            return
        super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasUrls():
            handled = False
            for url in md.urls():
                if not url.isLocalFile():
                    continue
                if self._is_image_url(url):
                    self.imageDropped.emit(url.toLocalFile())
                else:
                    self.fileDropped.emit(url.toLocalFile())
                handled = True
            if handled:
                e.acceptProposedAction()
                return
        super().dropEvent(e)

    def _is_image_url(self, url: QUrl) -> bool:
        if not url.isLocalFile():
            return False
        p = url.toLocalFile()
        from pathlib import Path as _P
        return _P(p).suffix.lower() in self.IMAGE_EXTS

    # Qt's HTML clipboard drops UserProperty values, so a copied inline-code
    # span pasted back lost CHAR_CODE: it kept a monospace look but saved as
    # plain text, not `backticks`. A copy records the selection's code spans
    # (offsets into its plain text) in this private format; a paste restores
    # them only when the inserted text is exactly the recorded text.
    CODE_SPANS_MIME = "application/x-qnotebook-code-spans"

    def createMimeDataFromSelection(self) -> QMimeData:  # noqa: N802 (Qt override)
        base = super().createMimeDataFromSelection()
        cur = self.textCursor()
        if not cur.hasSelection():
            return base
        start, end = cur.selectionStart(), cur.selectionEnd()
        spans: list[list[int]] = []
        block = self.document().findBlock(start)
        while block.isValid() and block.position() < end:
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.charFormat().property(CHAR_CODE):
                    s = max(frag.position(), start)
                    e = min(frag.position() + frag.length(), end)
                    if s < e:
                        spans.append([s - start, e - start])
                it += 1
            block = block.next()
        if not spans:
            return base
        # Qt returns its internal QTextEditMimeData, whose formats() is a
        # fixed list: setData() on it is invisible to hasFormat(). Copy every
        # format it offers into a plain QMimeData, then add the spans.
        data = QMimeData()
        for fmt in base.formats():
            data.setData(fmt, base.data(fmt))
        text = cur.selection().toPlainText()
        payload = json.dumps({"text": text, "spans": spans})
        data.setData(self.CODE_SPANS_MIME, QByteArray(payload.encode("utf-8")))
        return data

    @staticmethod
    def _utf16_len(text: str) -> int:
        # Document positions count UTF-16 code units, not Python characters.
        return len(text.encode("utf-16-le")) // 2

    @classmethod
    def _parse_code_spans(cls, raw: bytes) -> tuple[str, list[tuple[int, int]]] | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        if not isinstance(payload, dict):
            return None
        text = payload.get("text")
        spans = payload.get("spans")
        if not isinstance(text, str) or not text or not isinstance(spans, list):
            return None
        try:
            length = cls._utf16_len(text)
        except UnicodeError:
            return None  # e.g. an escaped lone surrogate; not a real fragment
        out: list[tuple[int, int]] = []
        for span in spans:
            if (not isinstance(span, list) or len(span) != 2
                    or not all(type(v) is int for v in span)):
                return None
            s, e = span
            if not 0 <= s < e <= length:
                return None
            out.append((s, e))
        return text, out

    def _restore_code_spans(self, source: QMimeData, insert_pos: int, insert_end: int) -> None:
        if not source.hasFormat(self.CODE_SPANS_MIME):
            return
        parsed = self._parse_code_spans(bytes(source.data(self.CODE_SPANS_MIME)))
        if parsed is None:
            return
        text, spans = parsed
        # Only the range this paste actually inserted, and only if it is the
        # recorded fragment: never existing text that happens to match.
        if insert_end - insert_pos != self._utf16_len(text):
            return
        check = QTextCursor(self.document())
        check.setPosition(insert_pos)
        check.setPosition(insert_end, QTextCursor.MoveMode.KeepAnchor)
        if check.selection().toPlainText() != text:
            return
        fmt = self._code_char_format()
        cur = QTextCursor(self.document())
        cur.joinPreviousEditBlock()  # one undo step with the paste itself
        for s, e in spans:
            cur.setPosition(insert_pos + s)
            cur.setPosition(insert_pos + e, QTextCursor.MoveMode.KeepAnchor)
            cur.mergeCharFormat(fmt)
        cur.endEditBlock()

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802 (Qt override)
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                self.imagePasted.emit(img)
                return
        if source.hasUrls():
            handled = False
            for url in source.urls():
                if self._is_image_url(url):
                    self.imageDropped.emit(url.toLocalFile())
                    handled = True
            if handled:
                return
        insert_pos = self.textCursor().selectionStart()
        super().insertFromMimeData(source)
        insert_end = self.textCursor().position()
        if insert_end > insert_pos:
            self._restore_code_spans(source, insert_pos, insert_end)

    # ---- insertions ----

    def insert_text_at_cursor(self, text: str) -> None:
        self.textCursor().insertText(text)

    def insert_horizontal_rule(self) -> None:
        cur = self.textCursor()
        cur.insertText("\n---\n")

    # ---- smoke helpers for tests ----

    # ---- context menu / spell ----

    def contextMenuEvent(self, e):  # noqa: N802
        menu = self.createStandardContextMenu()
        sh = getattr(self, "_spell_highlighter", None)
        if sh is not None and sh.is_active():
            cur = self.cursorForPosition(e.pos())
            cur.select(QTextCursor.SelectionType.WordUnderCursor)
            word = cur.selectedText()
            if word and not self._is_word_correct(word, sh):
                from PyQt6.QtGui import QAction
                menu.addSeparator()
                sugs = sh.suggestions(word, n=5)
                for sug in sugs:
                    act = QAction(sug, menu)
                    act.triggered.connect(
                        lambda _checked=False, c=cur, s=sug: self._replace_word(c, s)
                    )
                    menu.addAction(act)
                menu.addSeparator()
                add = QAction("Add to dictionary", menu)
                add.triggered.connect(lambda: sh.add_to_dictionary(word))
                menu.addAction(add)
                ig_once = QAction("Ignore once", menu)
                ig_once.triggered.connect(lambda: None)  # session no-op
                menu.addAction(ig_once)
                ig_nb = QAction("Ignore in this notebook", menu)
                ig_nb.triggered.connect(lambda: sh.ignore_word(word))
                menu.addAction(ig_nb)
        menu.exec(e.globalPos())

    def _is_word_correct(self, word: str, sh) -> bool:
        try:
            return sh._dict.check(word) if sh._dict else True
        except Exception:
            return True

    def _replace_word(self, cursor, replacement: str) -> None:
        cursor.insertText(replacement)
        self.setTextCursor(cursor)

    def attach_spell_highlighter(self, sh) -> None:
        """Lets MainWindow tell the editor about the spell highlighter so the
        context menu can offer suggestions."""
        self._spell_highlighter = sh
        if sh is not None:
            if self._content_highlighter is not None:
                self._content_highlighter.setDocument(None)
            set_style = getattr(sh, "set_content_style", None)
            if callable(set_style):
                set_style(self._content_style)
        elif self._content_highlighter is not None:
            self._content_highlighter.setDocument(self.document())
            self._content_highlighter.set_style(self._content_style)

    def heading_level_at_cursor(self) -> int:
        return int(self.textCursor().blockFormat().property(BLOCK_LEVEL) or 0)
