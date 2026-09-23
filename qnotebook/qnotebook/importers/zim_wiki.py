"""Convert a Zim wiki notebook (.txt files) to a qnotebook notebook (.md files).

Supported mappings:

    ====== H1 ======      -> # H1
    ===== H2 =====        -> ## H2
    ==== H3 ====          -> ### H3
    === H4 ===            -> #### H4
    == H5 ==              -> ##### H5
    **bold**              -> **bold**
    //italic//            -> _italic_
    __underline__         -> **...**   (CommonMark has no underline; use bold)
    [[link]]              -> [[link]]
    {{./image.png}}       -> ![](./image.png)
    [ ] / [*]             -> - [ ] / - [x]  (in bullet-list items)

Other Zim constructs (indented code, `~~strike~~`) are passed through as-is
where they also happen to be valid CommonMark.
"""

from __future__ import annotations

import re
from pathlib import Path

_HEADING_RE = re.compile(r"^(=+)\s+(.+?)\s+=+$")
_ITALIC_RE = re.compile(r"//([^/\n]+?)//")
_UNDERLINE_RE = re.compile(r"__([^_\n]+?)__")
_IMAGE_RE = re.compile(r"\{\{([^{}\n]+?)\}\}")
_CHECKBOX_LIST_RE = re.compile(r"^(\s*)\*\s+\[([ *x])\]\s+(.*)$")
_ZIM_BULLET_RE = re.compile(r"^(\s*)\*\s+(?!\[)(.*)$")


def convert_line(line: str) -> str:
    m = _HEADING_RE.match(line)
    if m:
        equals = len(m.group(1))
        # Zim uses 6 equals for H1, 5 for H2, etc. Clamp to 1..6.
        level = max(1, min(6, 7 - equals))
        return "#" * level + " " + m.group(2).strip()
    # Tasklist-style bullet: `* [ ] foo` or `* [*] foo`
    m = _CHECKBOX_LIST_RE.match(line)
    if m:
        indent, state, body = m.groups()
        box = "[ ]" if state == " " else "[x]"
        return f"{indent}- {box} {_convert_inline(body)}"
    # Plain Zim bullet `* item`
    m = _ZIM_BULLET_RE.match(line)
    if m:
        indent, body = m.groups()
        return f"{indent}- {_convert_inline(body)}"
    return _convert_inline(line)


def _convert_inline(text: str) -> str:
    # Image before italic (both use // inside paths)
    text = _IMAGE_RE.sub(lambda m: f"![]({m.group(1).strip()})", text)
    # Underline -> bold (CommonMark has no underline)
    text = _UNDERLINE_RE.sub(lambda m: f"**{m.group(1)}**", text)
    # Italic //x// -> _x_
    text = _ITALIC_RE.sub(lambda m: f"_{m.group(1)}_", text)
    return text


_ZIM_HEADER_RE = re.compile(r"^Content-Type:\s*text/x-zim-wiki", re.IGNORECASE)


def convert_text(src: str) -> str:
    """Convert the contents of a Zim `.txt` page to markdown."""
    lines = src.splitlines()
    # Strip Zim metadata header: a block of `Key: Value` lines up to a blank
    # line at the very top (if present).
    start = 0
    if lines and _ZIM_HEADER_RE.match(lines[0]):
        while start < len(lines) and lines[start].strip():
            start += 1
        # skip the blank separator line too
        while start < len(lines) and not lines[start].strip():
            start += 1
    out_lines = [convert_line(ln) for ln in lines[start:]]
    out = "\n".join(out_lines).rstrip("\n") + "\n"
    return out


def import_notebook(src_root: Path, dst_root: Path) -> list[Path]:
    """Walk `src_root` for `.txt` files and write `.md` equivalents under
    `dst_root` (same relative paths). Returns the list of written files."""
    src_root = Path(src_root)
    dst_root = Path(dst_root)
    dst_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for p in src_root.rglob("*.txt"):
        if any(part.startswith(".") for part in p.relative_to(src_root).parts):
            continue
        rel = p.relative_to(src_root)
        out = (dst_root / rel).with_suffix(".md")
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            src_text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            src_text = p.read_text(encoding="latin-1")
        out.write_text(convert_text(src_text), encoding="utf-8")
        written.append(out)
    return written
