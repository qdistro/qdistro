"""Render markdown source into a QTextDocument with semantic formats.

Each block stores a `UserProperty` with its block kind so the serializer
can reconstruct markdown without guessing. Inline character formats use
standard Qt attributes (bold, italic, etc.) plus anchor href for links.
Wikilinks use href `qnotebook:<Target>`; regular links use their URL as-is.

Parser: mistune v3, with built-in `table`/`strikethrough`/`task_lists`/`math`
plugins plus locally-defined inline plugins for `[[wikilinks]]` and `#tags`.
Block-level project markers (`[[!TOC]]`, `{{transclusion}}`) are sentinel-
preprocessed before parsing and re-tagged on the QTextDocument afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import mistune
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFrameFormat,
    QTextImageFormat,
    QTextListFormat,
    QTextTableFormat,
)

# Qt User properties on blocks — all values are strings/ints for portability.
BLOCK_KIND = QTextCharFormat.Property.UserProperty + 1  # "p" | "h" | "code" | "bq" | "hr" | "li" | "task" | "th" | "td"
BLOCK_LEVEL = QTextCharFormat.Property.UserProperty + 2  # heading level, list depth
BLOCK_LIST_KIND = QTextCharFormat.Property.UserProperty + 3  # "ul" | "ol"
BLOCK_TASK_STATE = QTextCharFormat.Property.UserProperty + 4  # 0 | 1
BLOCK_CODE_LANG = QTextCharFormat.Property.UserProperty + 5
BLOCK_ORDERED_START = QTextCharFormat.Property.UserProperty + 6

# Char properties
CHAR_WIKILINK = QTextCharFormat.Property.UserProperty + 10  # str target
CHAR_CODE = QTextCharFormat.Property.UserProperty + 11  # bool
CHAR_IMAGE_ALT = QTextCharFormat.Property.UserProperty + 12  # str alt text for images
CHAR_TAG = QTextCharFormat.Property.UserProperty + 13  # str tag name (e.g. "todo" for `#todo`)
BLOCK_TOC_MARKER = QTextCharFormat.Property.UserProperty + 14
BLOCK_TRANSCLUSION = QTextCharFormat.Property.UserProperty + 22
BLOCK_TRANSCLUDED_CHILD = QTextCharFormat.Property.UserProperty + 23
BLOCK_FOOTNOTE_DEF = QTextCharFormat.Property.UserProperty + 24
CHAR_FOOTNOTE_REF = QTextCharFormat.Property.UserProperty + 25

IMAGE_MAX_WIDTH = 600

# Kept for backwards compatibility with code that imported it from md_to_qdoc.
# The mistune `task_lists` plugin is always available since mistune is a hard
# dep, but the constant lets older callers keep their isinstance checks.
HAS_MDIT_TASKLISTS = True


WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
TAG_INLINE_RE = re.compile(r"(?:(?<=^)|(?<=[\s(\[]))(#[A-Za-z][\w-]*)")


def register_image_resource(doc: QTextDocument, rel_path: str, abs_path: str | None) -> None:
    """Register an image pixel source under `rel_path` so the document
    renders the image inline. Tolerates missing files (renders as a placeholder)."""
    if not abs_path:
        return
    img = QImage(abs_path)
    if img.isNull():
        return
    doc.addResource(
        QTextDocument.ResourceType.ImageResource.value,
        QUrl(rel_path),
        img,
    )


# --------------------------------------------------------------------
# Block & char formats
# --------------------------------------------------------------------


def _heading_format(level: int) -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    fmt.setHeadingLevel(level)
    fmt.setTopMargin(12)
    fmt.setBottomMargin(6)
    fmt.setProperty(BLOCK_KIND, "h")
    fmt.setProperty(BLOCK_LEVEL, level)
    return fmt


def _heading_char_format(level: int) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setFontWeight(QFont.Weight.Bold)
    sizes = {1: 20, 2: 17, 3: 15, 4: 13, 5: 12, 6: 11}
    f.setFontPointSize(sizes.get(level, 11))
    return f


def _paragraph_format() -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    fmt.setProperty(BLOCK_KIND, "p")
    fmt.setTopMargin(4)
    fmt.setBottomMargin(4)
    return fmt


def _code_format() -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    fmt.setProperty(BLOCK_KIND, "code")
    fmt.setBackground(QColor("#f4f4f4"))
    fmt.setLeftMargin(12)
    fmt.setRightMargin(12)
    fmt.setTopMargin(2)
    fmt.setBottomMargin(2)
    return fmt


def _code_char_format() -> QTextCharFormat:
    f = QTextCharFormat()
    f.setFontFamilies(["monospace"])
    return f


def _quote_format() -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    fmt.setProperty(BLOCK_KIND, "bq")
    fmt.setLeftMargin(16)
    fmt.setTopMargin(2)
    fmt.setBottomMargin(2)
    return fmt


def _hr_format() -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    fmt.setProperty(BLOCK_KIND, "hr")
    fmt.setTopMargin(8)
    fmt.setBottomMargin(8)
    return fmt


def _list_item_block_format(depth: int, is_task: bool, task_state: int) -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    fmt.setProperty(BLOCK_KIND, "task" if is_task else "li")
    fmt.setProperty(BLOCK_LEVEL, depth)
    if is_task:
        fmt.setProperty(BLOCK_TASK_STATE, task_state)
    fmt.setTopMargin(2)
    fmt.setBottomMargin(2)
    return fmt


@dataclass
class _InlineStyle:
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    link: str | None = None
    wikilink: str | None = None
    tag: str | None = None
    image_src: str | None = None
    image_alt: str = ""
    equation: str | None = None
    equation_display: bool = False

    def char_format(self) -> QTextCharFormat:
        f = QTextCharFormat()
        if self.bold:
            f.setFontWeight(QFont.Weight.Bold)
        if self.italic:
            f.setFontItalic(True)
        if self.strike:
            f.setFontStrikeOut(True)
        if self.code:
            f.setFontFamilies(["monospace"])
            f.setBackground(QColor("#f4f4f4"))
            f.setProperty(CHAR_CODE, True)
        if self.wikilink is not None:
            f.setAnchor(True)
            f.setAnchorHref(f"qnotebook:{self.wikilink}")
            f.setForeground(QColor("#1a5fb4"))
            f.setFontUnderline(True)
            f.setProperty(CHAR_WIKILINK, self.wikilink)
        elif self.link is not None:
            f.setAnchor(True)
            f.setAnchorHref(self.link)
            f.setForeground(QColor("#1a5fb4"))
            f.setFontUnderline(True)
        if self.tag is not None:
            f.setForeground(QColor("#1c71d8"))
            f.setFontWeight(QFont.Weight.DemiBold)
            f.setProperty(CHAR_TAG, self.tag)
        if self.equation is not None:
            from .equations import EQ_DISPLAY, EQ_LATEX
            f.setFontFamilies(["monospace"])
            f.setBackground(QColor("#fff8dc"))
            f.setProperty(EQ_LATEX, self.equation)
            f.setProperty(EQ_DISPLAY, bool(self.equation_display))
        return f


def _style_with(style: _InlineStyle, **changes) -> _InlineStyle:
    new = _InlineStyle(**style.__dict__)
    for k, v in changes.items():
        setattr(new, k, v)
    return new


# --------------------------------------------------------------------
# Renderer (cursor wrapper)
# --------------------------------------------------------------------


class _Renderer:
    def __init__(self, doc: QTextDocument, base_path: Path | None = None) -> None:
        self.doc = doc
        self.cursor = QTextCursor(doc)
        self._first_block = True
        self.base_path = base_path

    def new_block(self, block_fmt: QTextBlockFormat, char_fmt: QTextCharFormat | None = None) -> None:
        if self._first_block:
            self.cursor.setBlockFormat(block_fmt)
            if char_fmt is not None:
                self.cursor.setBlockCharFormat(char_fmt)
            self._first_block = False
        else:
            self.cursor.insertBlock(block_fmt, char_fmt or QTextCharFormat())

    def insert_text(self, text: str, fmt: QTextCharFormat | None = None) -> None:
        if fmt is None:
            fmt = QTextCharFormat()
        self.cursor.insertText(text, fmt)

    def insert_run(self, text: str, style: _InlineStyle, base_fmt: QTextCharFormat | None = None) -> None:
        if style.image_src is not None:
            self._insert_image_run(style.image_src, style.image_alt)
            return
        f = style.char_format()
        if base_fmt is not None:
            merged = QTextCharFormat(base_fmt)
            merged.merge(f)
            f = merged
        self.cursor.insertText(text, f)

    def _insert_image_run(self, src: str, alt: str) -> None:
        img_fmt = QTextImageFormat()
        img_fmt.setName(src)
        img_fmt.setProperty(CHAR_IMAGE_ALT, alt)
        abs_path: str | None = None
        if self.base_path is not None:
            try:
                abs_path = str((self.base_path / src).resolve())
            except (OSError, ValueError):
                abs_path = None
        if abs_path:
            img = QImage(abs_path)
            if not img.isNull():
                self.doc.addResource(
                    QTextDocument.ResourceType.ImageResource.value,
                    QUrl(src),
                    img,
                )
                w = img.width()
                h = img.height()
                if w > IMAGE_MAX_WIDTH:
                    ratio = IMAGE_MAX_WIDTH / float(w)
                    img_fmt.setWidth(IMAGE_MAX_WIDTH)
                    img_fmt.setHeight(h * ratio)
                else:
                    img_fmt.setWidth(w)
                    img_fmt.setHeight(h)
        self.cursor.insertImage(img_fmt)


# --------------------------------------------------------------------
# Inline plugins (wikilinks, tags)
# --------------------------------------------------------------------

# Mistune compiles every inline rule's regex into one giant alternation,
# so every named group must be unique across all plugins. Prefix ours with
# the rule name to stay safe.
_WIKILINK_PATTERN = (
    r"\[\[(?P<wikilink_target>[^\]\|]+?)"
    r"(?:\|(?P<wikilink_alias>[^\]]+?))?\]\]"
)
# Match `#tag` only at start-of-string or after whitespace / `(` / `[`.
_TAG_PATTERN = r"(?:(?<=^)|(?<=[\s(\[]))(?P<qntag_full>#[A-Za-z][\w-]*)"


def _append_wikilink_token(m, state, pipe_sentinel: str | None = None) -> int:
    target = m.group("wikilink_target")
    alias = m.group("wikilink_alias")
    if pipe_sentinel and pipe_sentinel in target:
        # The block-level table plugin runs before inline parsing. Aliased
        # wikilinks on adjacent lines otherwise look like a two-column table
        # and are irreversibly split before this plugin sees them.
        target, alias = target.split(pipe_sentinel, 1)
        alias = alias.replace(pipe_sentinel, "|")
    target = target.strip()
    display = alias.strip() if alias else target
    state.append_token({
        "type": "wikilink",
        "raw": display,
        "attrs": {"target": target},
    })
    return m.end()


def _parse_wikilink(inline, m, state):
    return _append_wikilink_token(m, state)


def _parse_tag(inline, m, state):
    full = m.group("qntag_full")  # includes leading "#"
    state.append_token({
        "type": "tag",
        "raw": full,
        "attrs": {"name": full[1:]},
    })
    return m.end()


def _wikilink_plugin(md: mistune.Markdown) -> None:
    md.inline.register("wikilink", _WIKILINK_PATTERN, _parse_wikilink, before="link")


def _wikilink_plugin_with_pipe_sentinel(pipe_sentinel: str):
    def plugin(md: mistune.Markdown) -> None:
        def parse(inline, m, state):
            return _append_wikilink_token(m, state, pipe_sentinel)

        md.inline.register("wikilink", _WIKILINK_PATTERN, parse, before="link")

    return plugin


def _tag_plugin(md: mistune.Markdown) -> None:
    md.inline.register("tag", _TAG_PATTERN, _parse_tag)


# --------------------------------------------------------------------
# Inline walk
# --------------------------------------------------------------------


def _walk_inline(children: list[dict], style: _InlineStyle) -> list[tuple[str, _InlineStyle]]:
    out: list[tuple[str, _InlineStyle]] = []
    for node in children or []:
        ntype = node.get("type")
        if ntype == "text":
            out.append((node.get("raw", ""), style))
        elif ntype == "strong":
            out.extend(_walk_inline(node.get("children", []), _style_with(style, bold=True)))
        elif ntype == "emphasis":
            out.extend(_walk_inline(node.get("children", []), _style_with(style, italic=True)))
        elif ntype == "strikethrough":
            out.extend(_walk_inline(node.get("children", []), _style_with(style, strike=True)))
        elif ntype == "codespan":
            out.append((node.get("raw", ""), _style_with(style, code=True)))
        elif ntype == "link":
            href = (node.get("attrs") or {}).get("url", "")
            out.extend(_walk_inline(node.get("children", []), _style_with(style, link=href)))
        elif ntype == "image":
            url = (node.get("attrs") or {}).get("url", "")
            alt_parts: list[str] = []
            for c in node.get("children", []) or []:
                if c.get("type") == "text":
                    alt_parts.append(c.get("raw", ""))
            alt = "".join(alt_parts) or node.get("raw", "")
            out.append(("", _style_with(style, image_src=url, image_alt=alt)))
        elif ntype == "wikilink":
            display = node.get("raw", "")
            target = (node.get("attrs") or {}).get("target", display)
            out.append((display, _style_with(style, wikilink=target)))
        elif ntype == "tag":
            full = node.get("raw", "")
            name = (node.get("attrs") or {}).get("name", full[1:] if full.startswith("#") else full)
            out.append((full, _style_with(style, tag=name)))
        elif ntype in ("inline_math", "block_math"):
            latex = node.get("raw", "")
            display = ntype == "block_math"
            delim = "$$" if display else "$"
            out.append(
                (
                    f"{delim}{latex}{delim}",
                    _style_with(
                        style,
                        equation=latex,
                        equation_display=display,
                    ),
                )
            )
        elif ntype in ("softbreak", "linebreak"):
            out.append(("\n", style))
        elif ntype in ("inline_html", "block_html"):
            out.append((node.get("raw", ""), style))
        elif ntype == "blank_line":
            pass
        else:
            # Unknown inline — fallback to text content if any.
            children_inner = node.get("children")
            if children_inner:
                out.extend(_walk_inline(children_inner, style))
            elif "raw" in node:
                out.append((node.get("raw", ""), style))
    return out


# --------------------------------------------------------------------
# Block walk
# --------------------------------------------------------------------


class _BlockState:
    """Mutable state threaded through the recursive block walker."""

    def __init__(self) -> None:
        self.list_stack: list[tuple[str, int]] = []  # (kind, start)
        self.qlist_stack: list = []  # parallel QTextList stack


def _walk_block(node: dict, r: _Renderer, st: _BlockState) -> None:
    ntype = node.get("type")

    if ntype == "heading":
        level = int((node.get("attrs") or {}).get("level", 1))
        r.new_block(_heading_format(level), _heading_char_format(level))
        runs = _walk_inline(node.get("children", []), _InlineStyle())
        base = _heading_char_format(level)
        for text, style in runs:
            r.insert_run(text, style, base_fmt=base)
        return

    if ntype == "paragraph":
        r.new_block(_paragraph_format())
        runs = _walk_inline(node.get("children", []), _InlineStyle())
        for text, style in runs:
            r.insert_run(text, style)
        return

    if ntype == "thematic_break":
        r.new_block(_hr_format())
        r.insert_text("—" * 20)
        return

    if ntype == "block_code":
        info = (node.get("attrs") or {}).get("info") or ""
        lang = info.strip().split()[0] if info.strip() else ""
        content = node.get("raw", "")
        if content.endswith("\n"):
            content = content[:-1]
        highlighted = _pygments_highlight(content, lang) if lang else None
        if highlighted is not None:
            for idx, line_tokens in enumerate(highlighted):
                fmt = _code_format()
                fmt.setProperty(BLOCK_CODE_LANG, lang)
                if idx == 0 and len(highlighted) > 1:
                    fmt.setProperty(BLOCK_LEVEL, 1)
                r.new_block(fmt, _code_char_format())
                for tok_text, tok_color in line_tokens:
                    f = _code_char_format()
                    if tok_color:
                        f.setForeground(QColor(tok_color))
                    r.insert_text(tok_text, f)
        else:
            lines = content.split("\n") if content else [""]
            for idx, line in enumerate(lines):
                fmt = _code_format()
                fmt.setProperty(BLOCK_CODE_LANG, lang)
                if idx == 0 and len(lines) > 1:
                    fmt.setProperty(BLOCK_LEVEL, 1)
                r.new_block(fmt, _code_char_format())
                r.insert_text(line, _code_char_format())
        return

    if ntype == "block_math":
        # Display equation: a paragraph containing one display equation run.
        latex = node.get("raw", "")
        r.new_block(_paragraph_format())
        style = _InlineStyle()
        style.equation = latex
        style.equation_display = True
        r.insert_run(f"$${latex}$$", style)
        return

    if ntype == "block_quote":
        for child in node.get("children", []) or []:
            if child.get("type") == "paragraph":
                r.new_block(_quote_format())
                runs = _walk_inline(child.get("children", []), _InlineStyle())
                for text, style in runs:
                    r.insert_run(text, style)
            else:
                _walk_block(child, r, st)
        return

    if ntype == "list":
        kind = "ol" if (node.get("attrs") or {}).get("ordered") else "ul"
        # mistune doesn't expose the start integer; default to 1.
        start = int((node.get("attrs") or {}).get("start") or 1)
        st.list_stack.append((kind, start))
        st.qlist_stack.append(None)
        try:
            for item in node.get("children", []) or []:
                _walk_list_item(item, r, st)
        finally:
            st.list_stack.pop()
            st.qlist_stack.pop()
        return

    if ntype == "table":
        _render_table(r, node)
        return

    if ntype == "footnotes":
        # Render each footnote_item as a paragraph block: `[^key]: body`.
        for item in node.get("children", []) or []:
            key = (item.get("attrs") or {}).get("key", "")
            body_parts: list[str] = []
            for child in item.get("children", []) or []:
                if child.get("type") == "paragraph":
                    runs = _walk_inline(child.get("children", []), _InlineStyle())
                    body_parts.append("".join(t for t, _ in runs))
            r.new_block(_paragraph_format())
            r.insert_text(f"[^{key}]: " + " ".join(body_parts))
        return

    if ntype == "blank_line":
        return

    if ntype == "block_html":
        # Pass HTML through as a paragraph of literal text.
        raw = node.get("raw", "")
        if raw.strip():
            r.new_block(_paragraph_format())
            r.insert_text(raw)
        return

    # Unknown block — best-effort: recurse into children if any.
    for child in node.get("children", []) or []:
        _walk_block(child, r, st)


def _walk_list_item(item: dict, r: _Renderer, st: _BlockState) -> None:
    is_task = item.get("type") == "task_list_item"
    task_state = 1 if is_task and (item.get("attrs") or {}).get("checked") else 0

    children = item.get("children", []) or []
    # First inline-bearing child becomes the item's primary block; nested
    # `list` children become nested lists; extra paragraphs become continuation
    # blocks at the same depth.
    first_inline_done = False
    for child in children:
        ctype = child.get("type")
        if ctype in ("block_text", "paragraph"):
            depth = len(st.list_stack)
            if not first_inline_done:
                fmt = _list_item_block_format(depth, is_task, task_state)
                r.new_block(fmt)
                current_list = st.qlist_stack[-1] if st.qlist_stack else None
                new_list = _apply_list_format(r.cursor, st.list_stack, current_list)
                st.qlist_stack[-1] = new_list
                first_inline_done = True
            else:
                # Continuation paragraph in a loose list — render as a li-kind
                # block (no second list bullet).
                fmt = _list_item_block_format(depth, False, 0)
                r.new_block(fmt)
            runs = _walk_inline(child.get("children", []), _InlineStyle())
            for text, style in runs:
                r.insert_run(text, style)
        elif ctype == "list":
            _walk_block(child, r, st)
        elif ctype == "block_code":
            _walk_block(child, r, st)
        else:
            _walk_block(child, r, st)


def _apply_list_format(cursor: QTextCursor, list_stack: list[tuple[str, int]], existing):
    kind, start = list_stack[-1]
    block_fmt = cursor.blockFormat()
    block_fmt.setProperty(BLOCK_LIST_KIND, kind)
    if kind == "ol":
        block_fmt.setProperty(BLOCK_ORDERED_START, start)
    cursor.setBlockFormat(block_fmt)
    if existing is not None:
        existing.add(cursor.block())
        return existing
    lf = QTextListFormat()
    depth = len(list_stack)
    lf.setIndent(depth)
    if kind == "ul":
        styles = [
            QTextListFormat.Style.ListDisc,
            QTextListFormat.Style.ListCircle,
            QTextListFormat.Style.ListSquare,
        ]
        lf.setStyle(styles[(depth - 1) % 3])
    else:
        lf.setStyle(QTextListFormat.Style.ListDecimal)
        set_start = getattr(lf, "setStart", None)
        if set_start is not None:
            set_start(start)
    return cursor.createList(lf)


# --------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------


def _render_table(r: _Renderer, node: dict) -> None:
    head_cells: list[list[dict]] = []
    align: list[str] = []
    body_rows: list[list[list[dict]]] = []
    for section in node.get("children", []) or []:
        stype = section.get("type")
        if stype == "table_head":
            for cell in section.get("children", []) or []:
                head_cells.append(cell.get("children", []) or [])
                a = (cell.get("attrs") or {}).get("align") or ""
                align.append(a)
        elif stype == "table_body":
            for row in section.get("children", []) or []:
                row_cells: list[list[dict]] = []
                for cell in row.get("children", []) or []:
                    row_cells.append(cell.get("children", []) or [])
                body_rows.append(row_cells)

    n_cols = max(len(head_cells), max((len(row) for row in body_rows), default=0)) if (head_cells or body_rows) else 0
    if n_cols == 0:
        return
    n_rows = (1 if head_cells else 0) + len(body_rows)
    if n_rows == 0:
        return

    tfmt = QTextTableFormat()
    tfmt.setBorder(1)
    tfmt.setCellPadding(4)
    tfmt.setCellSpacing(0)
    r.new_block(_paragraph_format())
    table = r.cursor.insertTable(n_rows, n_cols, tfmt)

    def fill_cell(row: int, col: int, children: list[dict], is_header: bool) -> None:
        cell = table.cellAt(row, col)
        cur = cell.firstCursorPosition()
        bfmt = QTextBlockFormat()
        bfmt.setProperty(BLOCK_KIND, "th" if is_header else "td")
        cur.setBlockFormat(bfmt)
        runs = _walk_inline(children, _InlineStyle())
        for text, style in runs:
            f = style.char_format()
            if is_header:
                f.setFontWeight(QFont.Weight.Bold)
            cur.insertText(text, f)

    row_offset = 0
    if head_cells:
        for c in range(n_cols):
            fill_cell(0, c, head_cells[c] if c < len(head_cells) else [], True)
        row_offset = 1
    for ri, row in enumerate(body_rows):
        for c in range(n_cols):
            fill_cell(row_offset + ri, c, row[c] if c < len(row) else [], False)
    r.cursor.movePosition(QTextCursor.MoveOperation.End)


# --------------------------------------------------------------------
# Sentinel preprocessing for project-specific block markers
# --------------------------------------------------------------------

_TOC_SENTINEL = "QNOTEBOOKTOCMARKERLINE"
_TRANSCLUDE_SENTINEL_PREFIX = "QNOTEBOOKTRANSCLUDELINE"
_WIKILINK_PIPE_SENTINEL_BASE = "QNOTEBOOKWIKILINKPIPE"


def _preprocess_transclusions(md_text: str) -> tuple[str, list[str]]:
    out_lines: list[str] = []
    targets: list[str] = []
    in_fence = False
    pat = re.compile(r"^\{\{([^{}\n]+)\}\}$")
    for line in (md_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        m = pat.match(stripped) if not in_fence else None
        if m:
            idx = len(targets)
            targets.append(m.group(1).strip())
            out_lines.append(f"{_TRANSCLUDE_SENTINEL_PREFIX}{idx}")
        else:
            out_lines.append(line)
    out = "\n".join(out_lines)
    if md_text.endswith("\n"):
        out += "\n"
    return out, targets


def _preprocess_toc_markers(md_text: str) -> str:
    out_lines: list[str] = []
    in_fence = False
    for line in (md_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if not in_fence and stripped == "[[!TOC]]":
            out_lines.append(_TOC_SENTINEL)
        else:
            out_lines.append(line)
    out = "\n".join(out_lines)
    if md_text.endswith("\n"):
        out += "\n"
    return out


def _preprocess_wikilink_pipes(md_text: str) -> tuple[str, str | None]:
    """Hide wikilink alias pipes from Mistune's block-level table parser.

    The table plugin sees raw blocks before the wikilink inline plugin. Two
    adjacent ``[[Target|alias]]`` lines therefore satisfy its permissive
    header/separator grammar and become a table. Replace pipes only inside
    real, unescaped wikilinks outside code spans/fences with a document-unique
    token. The sentinel-aware inline plugin restores their meaning later.
    """
    if "[[" not in md_text or "|" not in md_text:
        return md_text, None

    sentinel = _WIKILINK_PIPE_SENTINEL_BASE
    suffix = 0
    while sentinel in md_text:
        suffix += 1
        sentinel = f"{_WIKILINK_PIPE_SENTINEL_BASE}{suffix}"

    changed = False
    in_fence = False
    out_lines: list[str] = []
    for line in md_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue

        out: list[str] = []
        pos = 0
        code_ticks = 0
        while pos < len(line):
            if line[pos] == "`":
                end = pos + 1
                while end < len(line) and line[end] == "`":
                    end += 1
                run = end - pos
                if code_ticks == 0:
                    code_ticks = run
                elif run == code_ticks:
                    code_ticks = 0
                out.append(line[pos:end])
                pos = end
                continue

            if code_ticks == 0 and line.startswith("[[", pos):
                # An odd backslash run escapes the opening brackets.
                backslashes = 0
                check = pos - 1
                while check >= 0 and line[check] == "\\":
                    backslashes += 1
                    check -= 1
                end = line.find("]]", pos + 2)
                if backslashes % 2 == 0 and end >= 0:
                    raw = line[pos:end + 2]
                    if "|" in raw:
                        raw = raw.replace("|", sentinel)
                        changed = True
                    out.append(raw)
                    pos = end + 2
                    continue

            out.append(line[pos])
            pos += 1
        out_lines.append("".join(out))

    if not changed:
        return md_text, None
    return "".join(out_lines), sentinel


# --------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------


def _build_parser(wikilink_pipe_sentinel: str | None = None) -> mistune.Markdown:
    wikilink = (
        _wikilink_plugin_with_pipe_sentinel(wikilink_pipe_sentinel)
        if wikilink_pipe_sentinel else _wikilink_plugin
    )
    plugins = ["table", "strikethrough", "task_lists", "math",
               wikilink, _tag_plugin]
    return mistune.create_markdown(renderer=None, plugins=plugins)


def markdown_to_qdoc(
    md_text: str,
    doc: QTextDocument,
    base_path: Path | None = None,
    transclusion_resolver=None,
) -> None:
    """Parse markdown and populate `doc` with styled blocks."""
    doc.clear()
    doc.setDefaultStyleSheet("")
    root_fmt = QTextFrameFormat()
    root_fmt.setMargin(0)
    doc.rootFrame().setFrameFormat(root_fmt)

    # Frontmatter: strip and stash on the doc.
    try:
        from . import frontmatter as _fm_mod
        _fm_data, md_text = _fm_mod.split(md_text or "")
    except Exception:
        _fm_data = {}
    try:
        doc._zimqt_frontmatter = _fm_data  # type: ignore[attr-defined]
    except Exception:
        pass

    md_text = _preprocess_toc_markers(md_text or "")
    md_text, transclusion_targets = _preprocess_transclusions(md_text)
    md_text, wikilink_pipe_sentinel = _preprocess_wikilink_pipes(md_text)

    parser = _build_parser(wikilink_pipe_sentinel)
    ast = parser(md_text or "")

    r = _Renderer(doc, base_path=base_path)
    st = _BlockState()
    for node in ast or []:
        _walk_block(node, r, st)

    _post_process_toc_markers(doc)
    _post_process_transclusions(doc, transclusion_targets, transclusion_resolver)
    _post_process_footnotes(doc)


# --------------------------------------------------------------------
# Post-processing on the rendered QTextDocument
# --------------------------------------------------------------------


def _post_process_toc_markers(doc: QTextDocument) -> None:
    headings: list[tuple[int, str]] = []
    block = doc.firstBlock()
    while block.isValid():
        bf = block.blockFormat()
        if (bf.property(BLOCK_KIND) or "") == "h":
            level = int(bf.property(BLOCK_LEVEL) or 1)
            headings.append((level, block.text().strip()))
        block = block.next()

    hits: list[int] = []
    block = doc.firstBlock()
    while block.isValid():
        if block.text().strip() == _TOC_SENTINEL:
            hits.append(block.position())
        block = block.next()
    for pos in reversed(hits):
        cur = QTextCursor(doc)
        cur.setPosition(pos)
        blk = cur.block()
        cur.setPosition(blk.position())
        cur.setPosition(blk.position() + blk.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        bf = blk.blockFormat()
        bf.setProperty(BLOCK_TOC_MARKER, True)
        cur.setBlockFormat(bf)
        cf = QTextCharFormat()
        cf.setForeground(QColor("#1a5fb4"))
        cf.setFontItalic(True)
        cur.insertText("[[!TOC]]", cf)
        for level, htext in headings:
            if not htext:
                continue
            cbf = QTextBlockFormat()
            cbf.setProperty(BLOCK_KIND, "p")
            cbf.setProperty(BLOCK_TRANSCLUDED_CHILD, True)
            cbf.setLeftMargin(12 * level)
            cur.insertBlock(cbf)
            acf = QTextCharFormat()
            acf.setForeground(QColor("#1a5fb4"))
            acf.setAnchor(True)
            acf.setAnchorHref(f"qnotebook:#{htext}")
            acf.setProperty(CHAR_WIKILINK, f"#{htext}")
            acf.setFontUnderline(True)
            cur.insertText(htext, acf)


def _post_process_transclusions(doc: QTextDocument, targets: list[str], resolver) -> None:
    pat = re.compile(rf"^{_TRANSCLUDE_SENTINEL_PREFIX}(\d+)$")
    hits: list[tuple[int, int]] = []
    block = doc.firstBlock()
    while block.isValid():
        m = pat.match(block.text().strip())
        if m:
            hits.append((block.position(), int(m.group(1))))
        block = block.next()
    for pos, tidx in reversed(hits):
        if tidx >= len(targets):
            continue
        target = targets[tidx]
        cur = QTextCursor(doc)
        cur.setPosition(pos)
        blk = cur.block()
        cur.setPosition(blk.position())
        cur.setPosition(blk.position() + blk.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        bf = blk.blockFormat()
        bf.setProperty(BLOCK_TRANSCLUSION, target)
        cur.setBlockFormat(bf)
        cf = QTextCharFormat()
        cf.setForeground(QColor("#7f7f7f"))
        cf.setFontItalic(True)
        cur.insertText(f"{{{{{target}}}}}", cf)
        if resolver is not None:
            try:
                included = resolver(target)
            except Exception:
                included = None
            if included:
                cbf = QTextBlockFormat()
                cbf.setProperty(BLOCK_KIND, "p")
                cbf.setProperty(BLOCK_TRANSCLUDED_CHILD, True)
                cbf.setLeftMargin(12)
                cur.insertBlock(cbf)
                ccf = QTextCharFormat()
                ccf.setForeground(QColor("#4a4a4a"))
                cur.insertText(included.strip(), ccf)


_FN_REF_RE = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")
_FN_DEF_RE = re.compile(r"^\[\^([A-Za-z0-9_-]+)\]:\s")


def _post_process_footnotes(doc: QTextDocument) -> None:
    block = doc.firstBlock()
    while block.isValid():
        text = block.text()
        m_def = _FN_DEF_RE.match(text)
        if m_def:
            bf = block.blockFormat()
            bf.setProperty(BLOCK_FOOTNOTE_DEF, m_def.group(1))
            cur = QTextCursor(block)
            cur.setBlockFormat(bf)
        for m in _FN_REF_RE.finditer(text):
            if m.start() == 0 and m_def and m.group(1) == m_def.group(1):
                continue
            cur = QTextCursor(doc)
            cur.setPosition(block.position() + m.start())
            cur.setPosition(block.position() + m.end(), QTextCursor.MoveMode.KeepAnchor)
            cf = QTextCharFormat()
            cf.setForeground(QColor("#1a5fb4"))
            cf.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignSuperScript)
            cf.setProperty(CHAR_FOOTNOTE_REF, m.group(1))
            cur.mergeCharFormat(cf)
        block = block.next()


# --------------------------------------------------------------------
# Pygments highlighting (unchanged)
# --------------------------------------------------------------------


def _pygments_highlight(source: str, lang: str) -> list[list[tuple[str, str | None]]] | None:
    try:
        from pygments import lex
        from pygments.lexers import get_lexer_by_name
        from pygments.token import Token
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    try:
        lexer = get_lexer_by_name(lang, stripall=False)
    except ClassNotFound:
        return None

    palette = {
        Token.Keyword: "#8f3f71",
        Token.Keyword.Namespace: "#8f3f71",
        Token.Keyword.Constant: "#8f3f71",
        Token.Name.Function: "#1d4e89",
        Token.Name.Class: "#1d4e89",
        Token.Name.Builtin: "#1d4e89",
        Token.Name.Decorator: "#b16286",
        Token.String: "#af5f00",
        Token.String.Doc: "#7f7f7f",
        Token.Number: "#d65d0e",
        Token.Comment: "#7f7f7f",
        Token.Operator: "#5f5f5f",
        Token.Punctuation: "#5f5f5f",
    }

    def color_for(tok_type) -> str | None:
        t = tok_type
        while t is not None:
            if t in palette:
                return palette[t]
            t = t.parent
        return None

    lines: list[list[tuple[str, str | None]]] = [[]]
    for tok_type, tok_text in lex(source, lexer):
        color = color_for(tok_type)
        parts = tok_text.split("\n")
        for k, part in enumerate(parts):
            if part:
                lines[-1].append((part, color))
            if k < len(parts) - 1:
                lines.append([])
    if len(lines) > 1 and not lines[-1]:
        lines.pop()
    if not lines:
        lines = [[]]
    return lines
