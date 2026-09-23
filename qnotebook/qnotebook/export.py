"""HTML export: single page and whole notebook."""

from __future__ import annotations

import html
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import mistune

from .notebook import Notebook, page_to_relpath

DEFAULT_CSS = """\
body {
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 16px;
    line-height: 1.55;
    color: #222;
    background: #fafafa;
    margin: 0;
}
.layout { display: flex; min-height: 100vh; }
.sidebar {
    width: 220px;
    background: #f0f0ec;
    border-right: 1px solid #ddd;
    padding: 16px 12px;
    box-sizing: border-box;
    font-size: 14px;
}
.sidebar h2 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #555;
    margin: 0 0 8px 0;
}
.sidebar ul { list-style: none; padding-left: 10px; margin: 0; }
.sidebar li { margin: 2px 0; }
.sidebar a { color: #1a5fb4; text-decoration: none; }
.sidebar a:hover { text-decoration: underline; }
.content {
    flex: 1;
    padding: 32px 48px;
    max-width: 720px;
    box-sizing: border-box;
}
h1, h2, h3, h4, h5, h6 {
    font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif;
    line-height: 1.25;
}
a { color: #1a5fb4; }
code {
    font-family: 'Menlo', 'Monaco', 'Consolas', monospace;
    font-size: 0.92em;
    background: #f0efe9;
    padding: 1px 4px;
    border-radius: 3px;
}
pre {
    background: #f4f4f0;
    padding: 10px 14px;
    border-radius: 4px;
    overflow-x: auto;
    border: 1px solid #e4e3dc;
}
pre code { background: transparent; padding: 0; }
blockquote {
    border-left: 4px solid #c8c8c0;
    margin-left: 0;
    padding-left: 14px;
    color: #555;
}
table { border-collapse: collapse; }
table th, table td { border: 1px solid #c4c4bc; padding: 4px 10px; }
table th { background: #ececdc; }
img { max-width: 100%; height: auto; }
a.wikilink { color: #1a5fb4; }
a.wikilink.broken { color: #a33; text-decoration: line-through; }
a.tag {
    color: #1c71d8;
    font-weight: 600;
    text-decoration: none;
    background: #e8f1fd;
    padding: 0 4px;
    border-radius: 3px;
    font-size: 0.9em;
}
hr { border: none; border-top: 1px solid #c4c4bc; margin: 16px 0; }
.task-list-item { list-style: none; }
.task-list-item input { margin-right: 6px; }
"""


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
<div class="layout">
{sidebar}
<main class="content">
{body}
</main>
</div>
</body>
</html>
"""


WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
TAG_INLINE_RE = re.compile(r"(?:(?<=^)|(?<=[\s(\[]))#([A-Za-z][\w-]*)")


def _page_to_href(target: str, from_page: str | None) -> str:
    """Return a relative href for a wikilink target from the page doing the linking."""
    target = target.replace("\\", "/").strip().strip(":").strip("/")
    target_colon = target.replace("/", ":")
    target_parts = target_colon.split(":")
    if from_page:
        depth = from_page.count(":")
        prefix = "../" * depth
    else:
        prefix = ""
    return prefix + "/".join(target_parts) + ".html"


def _preprocess_wikilinks_and_tags(md_text: str, from_page: str | None, known_pages: set[str] | None) -> str:
    """Replace `[[Foo]]` and `#tag` outside code with HTML so the markdown
    renderer leaves them intact (html=True enables raw HTML passthrough)."""
    out_lines: list[str] = []
    in_fence = False
    for line in md_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        # Split off inline code spans so we don't rewrite inside them.
        segments: list[tuple[str, bool]] = []  # (text, is_code)
        pos = 0
        for m in re.finditer(r"`[^`]*`", line):
            if m.start() > pos:
                segments.append((line[pos:m.start()], False))
            segments.append((m.group(0), True))
            pos = m.end()
        if pos < len(line):
            segments.append((line[pos:], False))
        if not segments:
            segments.append((line, False))
        rebuilt: list[str] = []
        for seg, is_code in segments:
            if is_code:
                rebuilt.append(seg)
                continue
            seg = _rewrite_wikilinks_segment(seg, from_page, known_pages)
            seg = _rewrite_tags_segment(seg)
            rebuilt.append(seg)
        out_lines.append("".join(rebuilt))
    return "\n".join(out_lines)


def _rewrite_wikilinks_segment(seg: str, from_page: str | None, known_pages: set[str] | None) -> str:
    def repl(m: re.Match) -> str:
        target = m.group(1).strip()
        alias = m.group(2)
        display = alias.strip() if alias else target
        norm = target.replace("\\", "/").strip().strip(":").strip("/").replace("/", ":")
        href = _page_to_href(target, from_page)
        broken = known_pages is not None and norm not in known_pages
        cls = "wikilink broken" if broken else "wikilink"
        return f'<a class="{cls}" href="{html.escape(href, quote=True)}">{html.escape(display)}</a>'
    return WIKILINK_RE.sub(repl, seg)


def _rewrite_tags_segment(seg: str) -> str:
    def repl(m: re.Match) -> str:
        tag = m.group(1)
        return f'<a class="tag" href="?tag={html.escape(tag, quote=True)}">#{html.escape(tag)}</a>'
    return TAG_INLINE_RE.sub(repl, seg)


def _render_body(md_text: str, from_page: str | None, known_pages: set[str] | None) -> str:
    preprocessed = _preprocess_wikilinks_and_tags(md_text, from_page, known_pages)
    md = mistune.create_markdown(
        renderer="html",
        plugins=["table", "strikethrough", "task_lists", "footnotes", "math"],
        escape=False,
    )
    return md(preprocessed)


def _sidebar(pages: list[str], current: str | None) -> str:
    if not pages:
        return ""
    # Build a nested tree from colon-separated paths.
    tree: dict = {}
    for p in pages:
        parts = p.split(":")
        node = tree
        for part in parts:
            node = node.setdefault(part, {})

    def render_node(node: dict, prefix: list[str], depth: int) -> str:
        if not node:
            return ""
        items: list[str] = []
        for key in sorted(node.keys(), key=str.lower):
            full = ":".join(prefix + [key])
            href = _page_to_href(full, current)
            children = render_node(node[key], prefix + [key], depth + 1)
            cls = ' class="current"' if full == current else ""
            items.append(
                f'<li><a href="{html.escape(href, quote=True)}"{cls}>{html.escape(key)}</a>{children}</li>'
            )
        return "<ul>" + "".join(items) + "</ul>"

    return f'<nav class="sidebar"><h2>Pages</h2>{render_node(tree, [], 0)}</nav>'


def _copy_resources(notebook: Notebook, page: str, out_dir: Path, out_page_dir: Path) -> None:
    """Copy `_resources/` images referenced by the page into the export tree.

    The relative paths written into the page stay `_resources/...` (the page's
    sibling), so we need a `_resources/` directory alongside the exported html."""
    src_dir = notebook.file_for(page).parent / "_resources"
    if not src_dir.is_dir():
        return
    dst_dir = out_page_dir / "_resources"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for src in src_dir.iterdir():
        if src.is_file():
            shutil.copy2(src, dst_dir / src.name)


def export_page_pdf(notebook: Notebook, page: str, out_path: Path | str) -> Path:
    """Render the page to a styled QTextDocument and write PDF via QPdfWriter."""
    from PyQt6.QtCore import QMarginsF, QSizeF
    from PyQt6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md_text = notebook.get_page(page)
    base_path = notebook.file_for(page).parent

    from .md_to_qdoc import markdown_to_qdoc
    doc = QTextDocument()
    markdown_to_qdoc(md_text, doc, base_path=base_path)

    writer = QPdfWriter(str(out_path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    layout = writer.pageLayout()
    layout.setUnits(QPageLayout.Unit.Millimeter)
    layout.setMargins(QMarginsF(18, 18, 18, 18))
    writer.setPageLayout(layout)
    writer.setResolution(150)

    # Cap the document width to the page's printable width.
    page_size_pt = writer.pageLayout().paintRectPixels(writer.resolution()).size()
    doc.setPageSize(QSizeF(page_size_pt.width(), page_size_pt.height()))
    doc.print(writer)
    return out_path


def load_notebook_css(notebook: Notebook) -> str:
    """Return per-notebook CSS from `.qnotebook/export.css` or DEFAULT_CSS."""
    p = notebook.root / ".qnotebook" / "export.css"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            pass
    return DEFAULT_CSS


def save_notebook_css(notebook: Notebook, css: str) -> Path:
    """Persist per-notebook CSS to `.qnotebook/export.css`. Returns the path."""
    p = notebook.root / ".qnotebook" / "export.css"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(css, encoding="utf-8")
    return p


def export_page_html(
    notebook: Notebook,
    page: str,
    out_path: Path | str,
    css: str | None = None,
    known_pages: set[str] | None = None,
    sidebar_pages: list[str] | None = None,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md_text = notebook.get_page(page)
    body = _render_body(md_text, page, known_pages)
    title = page.rsplit(":", 1)[-1]
    sidebar_html = ""
    if sidebar_pages is not None:
        sidebar_html = _sidebar(sidebar_pages, page)
    html_doc = HTML_TEMPLATE.format(
        title=html.escape(title),
        css=css or DEFAULT_CSS,
        sidebar=sidebar_html,
        body=body,
    )
    out_path.write_text(html_doc, encoding="utf-8")
    # Copy referenced _resources for the page
    _copy_resources(notebook, page, out_path.parent, out_path.parent)
    return out_path


def export_notebook_html(
    notebook: Notebook,
    out_dir: Path | str,
    css: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_pages = [p.path for p in notebook.pages()]
    known_pages = set(all_pages)
    written: list[Path] = []
    for page in all_pages:
        rel = page_to_relpath(page).with_suffix(".html")
        out_path = out_dir / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        md_text = notebook.get_page(page)
        body = _render_body(md_text, page, known_pages)
        title = page.rsplit(":", 1)[-1]
        sidebar_html = _sidebar(all_pages, page)
        html_doc = HTML_TEMPLATE.format(
            title=html.escape(title),
            css=css or DEFAULT_CSS,
            sidebar=sidebar_html,
            body=body,
        )
        out_path.write_text(html_doc, encoding="utf-8")
        _copy_resources(notebook, page, out_dir, out_path.parent)
        written.append(out_path)
        if progress is not None:
            progress(page)
    # Write an index.html redirecting to the first page (if any).
    if all_pages:
        first = all_pages[0]
        first_href = _page_to_href(first, None)
        (out_dir / "index.html").write_text(
            f'<!DOCTYPE html><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0; url={html.escape(first_href, quote=True)}">'
            f'<title>Index</title>',
            encoding="utf-8",
        )
    return written
