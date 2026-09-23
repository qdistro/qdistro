"""Serialize a QTextDocument (populated by md_to_qdoc) back to markdown."""

from __future__ import annotations

from PyQt6.QtGui import QFont, QTextBlock, QTextCursor, QTextDocument, QTextTable

from .md_to_qdoc import (
    BLOCK_CODE_LANG,
    BLOCK_KIND,
    BLOCK_LEVEL,
    BLOCK_LIST_KIND,
    BLOCK_TASK_STATE,
    BLOCK_TOC_MARKER,
    BLOCK_TRANSCLUDED_CHILD,
    BLOCK_TRANSCLUSION,
    CHAR_CODE,
    CHAR_IMAGE_ALT,
    CHAR_WIKILINK,
)


def qdoc_to_markdown(doc: QTextDocument) -> str:
    out: list[str] = []
    blocks = _iter_blocks(doc)

    i = 0
    last_kind: str | None = None
    while i < len(blocks):
        block = blocks[i]
        # Detect table: Qt exposes tables via QTextCursor; if the block is inside a frame that is a QTextTable, emit the table once then skip its blocks.
        table = _table_of_block(doc, block)
        if table is not None:
            _ensure_blank(out, last_kind)
            out.extend(_emit_table(table).split("\n"))
            # Skip all blocks belonging to this table
            last_block_of_table = _last_block_in_table(doc, table)
            while i < len(blocks) and blocks[i].blockNumber() <= last_block_of_table.blockNumber():
                i += 1
            last_kind = "table"
            continue

        fmt = block.blockFormat()
        kind = fmt.property(BLOCK_KIND) or "p"

        # Transcluded-content children are rendered on load only; skip on save.
        if fmt.property(BLOCK_TRANSCLUDED_CHILD):
            i += 1
            continue

        # Transclusion placeholder: emit `{{Target}}` literally.
        tr_target = fmt.property(BLOCK_TRANSCLUSION)
        if tr_target:
            _ensure_blank(out, last_kind)
            out.append("{{" + str(tr_target) + "}}")
            last_kind = "p"
            i += 1
            continue

        # TOC marker block
        if fmt.property(BLOCK_TOC_MARKER):
            _ensure_blank(out, last_kind)
            out.append("[[!TOC]]")
            last_kind = "p"
            i += 1
            continue

        # Handle code-block runs: group consecutive "code" blocks into one fence
        if kind == "code":
            # Collect run
            run_lines: list[str] = []
            lang = fmt.property(BLOCK_CODE_LANG) or ""
            while i < len(blocks):
                b = blocks[i]
                bfmt = b.blockFormat()
                if (bfmt.property(BLOCK_KIND) or "") != "code":
                    break
                # Table nested in code? No.
                run_lines.append(b.text())
                i += 1
            _ensure_blank(out, last_kind)
            fence = "```"
            out.append(fence + (lang or ""))
            out.extend(run_lines)
            out.append(fence)
            last_kind = "code"
            continue

        if kind == "h":
            level = int(fmt.property(BLOCK_LEVEL) or 1)
            _ensure_blank(out, last_kind)
            out.append("#" * max(1, min(6, level)) + " " + _emit_inline(block))
            last_kind = "h"
            i += 1
            continue

        if kind == "hr":
            _ensure_blank(out, last_kind)
            out.append("---")
            last_kind = "hr"
            i += 1
            continue

        if kind == "bq":
            # Group consecutive bq blocks
            _ensure_blank(out, last_kind)
            while i < len(blocks):
                b = blocks[i]
                if (b.blockFormat().property(BLOCK_KIND) or "") != "bq":
                    break
                text = _emit_inline(b)
                out.append("> " + text if text else ">")
                i += 1
            last_kind = "bq"
            continue

        if kind in ("li", "task"):
            # Emit list items; group contiguous list items of same list stack.
            _ensure_blank(out, last_kind)
            while i < len(blocks):
                b = blocks[i]
                bkind = b.blockFormat().property(BLOCK_KIND) or ""
                if bkind not in ("li", "task"):
                    break
                depth = int(b.blockFormat().property(BLOCK_LEVEL) or 1)
                list_kind = b.blockFormat().property(BLOCK_LIST_KIND) or "ul"
                indent = "  " * (depth - 1)
                if list_kind == "ol":
                    # Determine number: use QTextList's item index
                    tl = b.textList()
                    num = tl.itemNumber(b) + 1 if tl is not None else 1
                    marker = f"{num}."
                else:
                    marker = "-"
                text = _emit_inline(b)
                if bkind == "task":
                    state = int(b.blockFormat().property(BLOCK_TASK_STATE) or 0)
                    checkbox = "[x]" if state else "[ ]"
                    out.append(f"{indent}{marker} {checkbox} {text}".rstrip())
                else:
                    out.append(f"{indent}{marker} {text}".rstrip())
                i += 1
            last_kind = "li"
            continue

        # Default: paragraph
        text = _emit_inline(block)
        if not text:
            # Empty paragraph — skip; block separation is handled by _ensure_blank.
            i += 1
            continue
        _ensure_blank(out, last_kind)
        out.append(text)
        last_kind = "p"
        i += 1

    # Strip trailing blanks
    while out and out[-1] == "":
        out.pop()
    body = "\n".join(out) + ("\n" if out else "")
    # Prepend frontmatter if one was stashed on the doc by markdown_to_qdoc.
    fm_data = getattr(doc, "_zimqt_frontmatter", None)
    if fm_data:
        try:
            from . import frontmatter as _fm_mod
            return _fm_mod.join(fm_data, body)
        except Exception:
            pass
    return body


def _iter_blocks(doc: QTextDocument) -> list[QTextBlock]:
    out: list[QTextBlock] = []
    b = doc.firstBlock()
    while b.isValid():
        out.append(b)
        b = b.next()
    return out


def _ensure_blank(out: list[str], last_kind: str | None) -> None:
    if not out:
        return
    if out[-1] == "":
        return
    out.append("")


def _emit_inline(block: QTextBlock, in_table_header: bool = False) -> str:
    parts: list[str] = []
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid():
            text = frag.text()
            if text:
                parts.append(_emit_fragment(text, frag.charFormat(), in_table_header))
        it += 1
    return "".join(parts)


def _emit_fragment(text: str, fmt, in_table_header: bool = False) -> str:
    # Equation fragment: round-trips back to `$..$` / `$$..$$`.
    from .equations import serialize_equation_fragment
    eq = serialize_equation_fragment(fmt)
    if eq is not None:
        return eq
    # Image fragment: QTextImageFormat has a non-empty name().
    if fmt.isImageFormat():
        name = fmt.toImageFormat().name()
        alt = fmt.property(CHAR_IMAGE_ALT)
        alt_str = "" if alt is None else str(alt)
        return f"![{alt_str}]({name})"
    # Wikilink: single anchor run
    wikilink = fmt.property(CHAR_WIKILINK)
    if wikilink:
        target = str(wikilink)
        if text == target:
            return f"[[{target}]]"
        return f"[[{target}|{text}]]"
    # Inline code: wrap in backticks, no other formatting
    if fmt.property(CHAR_CODE) or (fmt.fontFamilies() and "monospace" in [s.lower() for s in fmt.fontFamilies()] and not fmt.isAnchor()):
        # Escape backticks by using a longer fence
        if "`" in text:
            return f"`` {text} ``"
        return f"`{text}`"
    bold = fmt.fontWeight() >= QFont.Weight.Bold
    italic = fmt.fontItalic()
    strike = fmt.fontStrikeOut()
    is_link = fmt.isAnchor() and fmt.anchorHref() and not fmt.anchorHref().startswith("qnotebook:")

    # Don't treat heading char bold as markdown bold: heading's block kind already marks it.
    # We pass a flag via... just check: the block format BLOCK_KIND handles headings.
    # So if bold comes from heading font, we should strip it. We can't tell here easily.
    # Heuristic: if fontPointSize > 11, assume heading — skip bold/italic wrappers.
    font_size = fmt.fontPointSize()
    if font_size and font_size > 11.5:
        bold = False  # heading
        italic = False
    if in_table_header:
        bold = False  # table header bolding is a rendering choice, not content

    s = text
    if is_link:
        href = fmt.anchorHref()
        s = f"[{s}]({href})"
    if strike:
        s = f"~~{s}~~"
    if bold and italic:
        s = f"**_{s}_**"
    elif bold:
        s = f"**{s}**"
    elif italic:
        s = f"_{s}_"
    return s


def _table_of_block(doc: QTextDocument, block: QTextBlock) -> QTextTable | None:
    cur = QTextCursor(block)
    return cur.currentTable()


def _last_block_in_table(doc: QTextDocument, table: QTextTable) -> QTextBlock:
    # Find the block containing the table's last cell's last position.
    last_cell = table.cellAt(table.rows() - 1, table.columns() - 1)
    cur = last_cell.lastCursorPosition()
    return cur.block()


def _emit_table(table: QTextTable) -> str:
    rows = table.rows()
    cols = table.columns()
    if rows == 0 or cols == 0:
        return ""
    grid: list[list[str]] = []
    for r in range(rows):
        row: list[str] = []
        for c in range(cols):
            cell = table.cellAt(r, c)
            cur_start = cell.firstCursorPosition()
            cur_end = cell.lastCursorPosition()
            # Collect inline from all blocks in this cell (usually one)
            block = cur_start.block()
            texts: list[str] = []
            is_header = r == 0
            while block.isValid() and block.position() <= cur_end.position():
                texts.append(_emit_inline(block, in_table_header=is_header))
                if block == cur_end.block():
                    break
                block = block.next()
            row.append(" ".join(t for t in texts if t))
        grid.append(row)
    # Pad column widths
    widths = [0] * cols
    for row in grid:
        for c, cell in enumerate(row):
            widths[c] = max(widths[c], len(cell))
    widths = [max(w, 3) for w in widths]
    def fmt_row(cells: list[str]) -> str:
        return "| " + " | ".join(cells[c].ljust(widths[c]) for c in range(cols)) + " |"
    header = grid[0]
    body = grid[1:]
    out_lines = [fmt_row(header)]
    sep_cells = ["-" * widths[c] for c in range(cols)]
    out_lines.append("| " + " | ".join(sep_cells) + " |")
    for row in body:
        out_lines.append(fmt_row(row))
    return "\n".join(out_lines)
