"""CLI surface for qnotebook — headless commands never construct QApplication.

Commands:
  qnotebook [<notebook>] [<page>]             GUI launch
  qnotebook --list-notebooks
  qnotebook --list-pages <notebook>
  qnotebook --search <notebook> <query> [--format=grep|json]
  qnotebook --export <notebook> <page> --format {html,pdf,md} --output <path>
  qnotebook --export-all <notebook> --format html --output <dir>
  qnotebook --index-rebuild <notebook>
  qnotebook --new-page <notebook> <page> [--template NAME] [--content STR|--stdin]
  qnotebook --open-quicknote [<notebook>]
  qnotebook --append-today <notebook> <text>
  qnotebook --append <notebook> <page> <text>
     [--bullet] [--timestamp] [--heading NAME] [--stdin] [--link TARGET]
  qnotebook --version / --help
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# ----- Notebook registry (QSettings-backed) -----


def _recent_notebooks() -> list[str]:
    try:
        from PyQt6.QtCore import QSettings
    except Exception:
        return []
    s = QSettings("qnotebook", "qnotebook")
    val = s.value("recent_notebooks", [], type=list)
    return [str(v) for v in val if v]


def _notebook(path: str):
    from .notebook import Notebook
    return Notebook(Path(path))


# ----- Headless commands -----


def cmd_list_notebooks() -> int:
    for nb in _recent_notebooks():
        print(nb)
    return 0


def cmd_list_pages(notebook: str) -> int:
    nb = _notebook(notebook)
    for ref in nb.pages():
        print(ref.path)
    return 0


def cmd_search(notebook: str, query: str, fmt: str = "grep") -> int:
    nb = _notebook(notebook)
    results: list[tuple[str, int, str]] = []
    for ref in nb.pages():
        try:
            text = nb.get_page(ref.path)
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if query.lower() in line.lower():
                path = str(nb.file_for(ref.path))
                results.append((path, i, line))
    if fmt == "json":
        print(json.dumps([
            {"path": p, "line": line_no, "text": t} for (p, line_no, t) in results
        ]))
    else:
        for p, line_no, t in results:
            print(f"{p}:{line_no}:{t}")
    return 0


def cmd_export(notebook: str, page: str, fmt: str, output: str) -> int:
    from . import export
    nb = _notebook(notebook)
    out = Path(output)
    if fmt == "html":
        export.export_page_html(nb, page, out)
    elif fmt == "pdf":
        # PDF export needs a QApplication — launch a minimal one.
        from PyQt6.QtWidgets import QApplication
        _app = QApplication.instance() or QApplication([])
        export.export_page_pdf(nb, page, out)
    elif fmt == "md":
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(nb.get_page(page), encoding="utf-8")
    else:
        print(f"Unknown format: {fmt}", file=sys.stderr)
        return 2
    print(f"Wrote {out}")
    return 0


def cmd_export_all(notebook: str, fmt: str, output: str) -> int:
    from . import export
    nb = _notebook(notebook)
    out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)
    if fmt == "html":
        export.export_notebook_html(nb, out_dir)
    else:
        # Plain markdown dump
        for ref in nb.pages():
            dest = out_dir / (ref.path.replace(":", "/") + ".md")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(nb.get_page(ref.path), encoding="utf-8")
    print(f"Exported to {out_dir}")
    return 0


def cmd_index_rebuild(notebook: str) -> int:
    from .index import Index
    nb = _notebook(notebook)
    idx = Index(nb)
    idx.rebuild()
    print(f"Index rebuilt for {notebook}")
    return 0


def cmd_new_page(notebook: str, page: str, *,
                 template: str | None = None,
                 content: str | None = None,
                 stdin: bool = False) -> int:
    nb = _notebook(notebook)
    if nb.exists(page):
        print(f"Page already exists: {page}", file=sys.stderr)
        return 2
    body = ""
    if content is not None:
        body = content
    elif stdin:
        body = sys.stdin.read()
    elif template is not None:
        from .templates import load_template, render_template
        tpl = load_template(nb, template)
        if tpl is None:
            print(f"Template not found: {template}", file=sys.stderr)
            return 2
        body = render_template(tpl, page)
    nb.save_page(page, body)
    print(str(nb.file_for(page)))
    return 0


def cmd_append(notebook: str, page: str, text: str, *,
               bullet: bool = False, timestamp: bool = False,
               heading: str | None = None, stdin: bool = False,
               link: str | None = None) -> int:
    nb = _notebook(notebook)
    if stdin:
        text = sys.stdin.read().rstrip("\n")
    prefix = ""
    if timestamp:
        prefix += time.strftime("%H:%M") + " "
    if bullet:
        prefix = "- " + prefix
    line = prefix + text
    if link:
        line += f" [[{link}]]"

    # Load-then-modify-then-save through the merge ladder so a concurrent
    # GUI save (or another external writer) that lands between our read and
    # our write is detected as drift and merged rather than clobbered.
    if nb.exists(page):
        lr = nb.load_for_save(page)
        existing = lr.original.decode("utf-8", errors="replace")
    else:
        lr = None
        existing = ""
    new_body = _append_to_body(existing, line, heading=heading)
    result = nb.save_page(page, new_body, load_result=lr)
    if result.conflict:
        # Surface an unmerged conflict as a `.md.conflict` sibling and
        # report exit code 3 so scripts can detect it. The original disk
        # bytes are preserved.
        conflict_path = nb.file_for(page).with_suffix(".md.conflict")
        from . import safe_save as _ss
        _ss.atomic_write(conflict_path, result.ours)
        print(f"conflict: wrote {conflict_path}", file=sys.stderr)
        return 3
    _forward_reload(nb.root, page)
    print(str(nb.file_for(page)))
    return 0


def _forward_reload(root: Path, page: str) -> None:
    try:
        from . import single_instance
        single_instance.try_forward(root, {"cmd": "reload", "page": page})
    except Exception:
        pass


def _append_to_body(body: str, line: str, *, heading: str | None = None) -> str:
    """Append ``line`` to ``body``. If ``heading`` given, append under that
    heading; create the heading if missing."""
    if heading is None:
        if not body.endswith("\n") and body:
            body += "\n"
        return body + line + "\n"

    # Find heading (any level matching exact title)
    lines = body.splitlines()
    target_idx = -1
    target_level = 0
    for i, ln in enumerate(lines):
        stripped = ln.lstrip("#").lstrip()
        if stripped == heading and ln.startswith("#"):
            target_idx = i
            target_level = len(ln) - len(ln.lstrip("#"))
            break
    if target_idx < 0:
        # Append new heading at end
        if body and not body.endswith("\n"):
            body += "\n"
        return body + f"\n## {heading}\n{line}\n"
    # Insert right before next heading of same or higher level, else end
    insert_at = len(lines)
    for j in range(target_idx + 1, len(lines)):
        if lines[j].startswith("#"):
            lvl = len(lines[j]) - len(lines[j].lstrip("#"))
            if lvl <= target_level:
                insert_at = j
                break
    lines.insert(insert_at, line)
    return "\n".join(lines) + "\n"


def cmd_append_today(notebook: str, text: str, **kwargs) -> int:
    today = time.strftime("Journal:%Y:%m:%d")
    nb = _notebook(notebook)
    if not nb.exists(today):
        try:
            from .templates import load_template, render_template
            tpl = load_template(nb, "Daily Journal")
        except Exception:
            tpl = None
        if tpl is None:
            nb.create_page(today, f"# {today}\n\n")
        else:
            body = render_template(tpl, today)
            nb.create_page(today, body)
    return cmd_append(notebook, today, text, **kwargs)


# ----- Argparse -----


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qnotebook", description="qnotebook CLI")
    p.add_argument("--version", action="store_true")
    p.add_argument("--list-notebooks", action="store_true")
    p.add_argument("--list-pages", metavar="NOTEBOOK")
    p.add_argument("--search", nargs=2, metavar=("NOTEBOOK", "QUERY"))
    p.add_argument("--format", default="grep")
    p.add_argument("--export", nargs=2, metavar=("NOTEBOOK", "PAGE"))
    p.add_argument("--export-all", metavar="NOTEBOOK")
    p.add_argument("--output", metavar="PATH")
    p.add_argument("--index-rebuild", metavar="NOTEBOOK")
    p.add_argument("--new-page", nargs=2, metavar=("NOTEBOOK", "PAGE"))
    p.add_argument("--template", metavar="NAME")
    p.add_argument("--content", metavar="STR")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--open-quicknote", metavar="NOTEBOOK", nargs="?", const="")
    p.add_argument("--append-today", nargs=2, metavar=("NOTEBOOK", "TEXT"))
    p.add_argument("--append", nargs=3, metavar=("NOTEBOOK", "PAGE", "TEXT"))
    p.add_argument("--bullet", action="store_true")
    p.add_argument("--timestamp", action="store_true")
    p.add_argument("--heading", metavar="NAME")
    p.add_argument("--link", metavar="TARGET")
    p.add_argument("positional", nargs="*")
    return p


def _append_kwargs(args) -> dict:
    return dict(
        bullet=args.bullet,
        timestamp=args.timestamp,
        heading=args.heading,
        stdin=args.stdin,
        link=args.link,
    )


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        from . import __version__
        print(__version__)
        return 0
    if args.list_notebooks:
        return cmd_list_notebooks()
    if args.list_pages:
        return cmd_list_pages(args.list_pages)
    if args.search:
        return cmd_search(args.search[0], args.search[1], fmt=args.format)
    if args.export:
        if not args.output:
            print("--output required", file=sys.stderr)
            return 2
        return cmd_export(args.export[0], args.export[1],
                          args.format, args.output)
    if args.export_all:
        return cmd_export_all(args.export_all, args.format, args.output or ".")
    if args.index_rebuild:
        return cmd_index_rebuild(args.index_rebuild)
    if args.new_page:
        return cmd_new_page(
            args.new_page[0], args.new_page[1],
            template=args.template, content=args.content, stdin=args.stdin,
        )
    if args.append_today:
        return cmd_append_today(args.append_today[0], args.append_today[1],
                                **_append_kwargs(args))
    if args.append:
        return cmd_append(args.append[0], args.append[1], args.append[2],
                          **_append_kwargs(args))
    # Fallthrough: GUI — return sentinel -1 so __main__ launches GUI instead.
    return -1
