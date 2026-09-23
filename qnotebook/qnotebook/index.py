"""SQLite-backed index of pages and wikilinks."""

from __future__ import annotations

import re
import sqlite3

from . import frontmatter as _fm
from .notebook import DOTDIR, Notebook

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def split_target_heading(target: str) -> tuple[str, str]:
    """Split `Page#Heading` -> (`Page`, `Heading`). `#Heading` -> (``, `Heading`)."""
    if "#" in target:
        page, _, heading = target.partition("#")
        return page.strip(), heading.strip()
    return target.strip(), ""

# Tags: `#tag` at a word boundary, with letters/digits/underscore/dash.
# We explicitly require a non-word char (or start of line) before the `#`
# so that URL fragments like `http://x#y` don't match.
TAG_RE = re.compile(r"(?:(?<=^)|(?<=[\s(\[]))#([A-Za-z][\w-]*)")


def extract_tags(md_text: str) -> list[str]:
    """Return `#tag` names in `md_text`, skipping fenced code / inline code
    / markdown link targets / wikilink internals."""
    # Strip YAML frontmatter so `tags: [...]` lines aren't scanned as `#`-tags.
    try:
        _, md_text = _fm.split(md_text)
    except Exception:
        pass
    out: list[str] = []
    in_fence = False
    for line in md_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Strip inline code spans.
        cleaned = re.sub(r"`[^`]*`", "", line)
        # Strip markdown link URL parts `[text](url)` and bare urls/wikilinks
        # where `#` could be a fragment.
        cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cleaned)
        cleaned = re.sub(r"\[\[([^\]]*)\]\]", "", cleaned)
        for m in TAG_RE.finditer(cleaned):
            out.append(m.group(1))
    # De-dup preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t not in seen:
            uniq.append(t)
            seen.add(t)
    return uniq


def extract_wikilinks(md_text: str) -> list[str]:
    """Return wikilink targets from markdown source.

    Skips links inside fenced code blocks and inline code spans.
    """
    try:
        _, md_text = _fm.split(md_text)
    except Exception:
        pass
    out: list[str] = []
    in_fence = False
    for line in md_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Strip inline code spans
        cleaned = re.sub(r"`[^`]*`", "", line)
        for m in WIKILINK_RE.finditer(cleaned):
            raw = m.group(1).strip()
            page_part, _heading = split_target_heading(raw)
            # Same-page anchor [[#Heading]] has no forward-link target.
            if not page_part:
                continue
            target = _normalize_target(page_part)
            if target:
                out.append(target)
    return out


def _normalize_target(target: str) -> str:
    """Normalize `Foo/Bar` or `Foo:Bar` to colon form."""
    t = target.strip().replace("\\", "/")
    if "/" in t:
        t = t.replace("/", ":")
    # Strip leading/trailing colons
    return t.strip(":")


REWRITE_RE = re.compile(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]")


def rewrite_wikilinks(md_text: str, old: str, new: str) -> str:
    """Rewrite `[[old]]`/`[[old|alias]]` → `[[new]]`/`[[new|alias]]`.

    Matches by normalized target (slashes and colons equivalent); leaves
    aliases and unrelated links untouched. Skips fenced code blocks and
    inline code spans."""
    old_norm = _normalize_target(old)
    out_lines: list[str] = []
    in_fence = False
    for line in md_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        out_lines.append(_rewrite_line(line, old_norm, new))
    return "".join(out_lines)


def _rewrite_line(line: str, old_norm: str, new: str) -> str:
    # Protect inline code spans: only rewrite outside them.
    parts: list[str] = []
    pos = 0
    for m in re.finditer(r"`[^`]*`", line):
        parts.append(_rewrite_outside_code(line[pos : m.start()], old_norm, new))
        parts.append(m.group(0))
        pos = m.end()
    parts.append(_rewrite_outside_code(line[pos:], old_norm, new))
    return "".join(parts)


def _rewrite_outside_code(segment: str, old_norm: str, new: str) -> str:
    def repl(m: re.Match) -> str:
        target = m.group(1).strip()
        alias = m.group(2) or ""
        page_part, heading = split_target_heading(target)
        if not page_part:
            return m.group(0)  # [[#Heading]] same-page anchor — unaffected
        if _normalize_target(page_part) != old_norm:
            return m.group(0)
        suffix = f"#{heading}" if heading else ""
        return f"[[{new}{suffix}{alias}]]"
    return REWRITE_RE.sub(repl, segment)


class Index:
    def __init__(self, notebook: Notebook) -> None:
        self.notebook = notebook
        self.db_path = notebook.root / DOTDIR / "index.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
                path TEXT PRIMARY KEY,
                mtime REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS links (
                src TEXT NOT NULL,
                dst TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_links_dst ON links(dst);
            CREATE INDEX IF NOT EXISTS idx_links_src ON links(src);
            CREATE TABLE IF NOT EXISTS tags (
                tag TEXT NOT NULL,
                page TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
            CREATE INDEX IF NOT EXISTS idx_tags_page ON tags(page);
            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT NOT NULL,
                page TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
            CREATE INDEX IF NOT EXISTS idx_aliases_page ON aliases(page);
            """
        )
        self._fts = False
        try:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts "
                "USING fts5(path UNINDEXED, body)"
            )
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
        c.commit()

    def has_fts(self) -> bool:
        return self._fts

    def fts_candidates(self, text: str) -> list[str]:
        if not self._fts or not text.strip():
            return []
        # Use FTS5 MATCH with a quoted phrase so punctuation doesn't error.
        safe = text.replace('"', '""')
        try:
            rows = self._conn.execute(
                'SELECT path FROM pages_fts WHERE body MATCH ?',
                (f'"{safe}"',),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r["path"] for r in rows]

    # ---- mutation ----

    def rebuild(self) -> None:
        c = self._conn
        c.execute("DELETE FROM pages")
        c.execute("DELETE FROM links")
        c.execute("DELETE FROM tags")
        c.execute("DELETE FROM aliases")
        if self._fts:
            c.execute("DELETE FROM pages_fts")
        for page in self.notebook.pages():
            self._update_page_locked(page.path)
        c.commit()

    def update_page(self, page_path: str, md_text: str | None = None) -> None:
        self._update_page_locked(page_path, md_text)
        self._conn.commit()

    def _update_page_locked(self, page_path: str, md_text: str | None = None) -> None:
        f = self.notebook.file_for(page_path)
        if not f.is_file():
            self._conn.execute("DELETE FROM pages WHERE path = ?", (page_path,))
            self._conn.execute("DELETE FROM links WHERE src = ?", (page_path,))
            self._conn.execute("DELETE FROM tags WHERE page = ?", (page_path,))
            self._conn.execute("DELETE FROM aliases WHERE page = ?", (page_path,))
            if self._fts:
                self._conn.execute("DELETE FROM pages_fts WHERE path = ?", (page_path,))
            return
        if md_text is None:
            md_text = f.read_text(encoding="utf-8")
        mtime = f.stat().st_mtime
        self._conn.execute(
            "INSERT OR REPLACE INTO pages(path, mtime) VALUES (?, ?)",
            (page_path, mtime),
        )
        self._conn.execute("DELETE FROM links WHERE src = ?", (page_path,))
        self._conn.execute("DELETE FROM tags WHERE page = ?", (page_path,))
        self._conn.execute("DELETE FROM aliases WHERE page = ?", (page_path,))
        # Parse frontmatter; index aliases + merge frontmatter tags.
        fm, _body = _fm.split(md_text)
        aliases = fm.get("aliases") if isinstance(fm, dict) else None
        if isinstance(aliases, list):
            alias_rows = [(str(a), page_path) for a in aliases if a]
            if alias_rows:
                self._conn.executemany(
                    "INSERT INTO aliases(alias, page) VALUES (?, ?)",
                    alias_rows,
                )
        targets = extract_wikilinks(md_text)
        if targets:
            self._conn.executemany(
                "INSERT INTO links(src, dst) VALUES (?, ?)",
                [(page_path, t) for t in targets],
            )
        tags = list(extract_tags(md_text))
        fm_tags = fm.get("tags") if isinstance(fm, dict) else None
        if isinstance(fm_tags, list):
            for t in fm_tags:
                ts = str(t).lstrip("#").strip()
                if ts and ts not in tags:
                    tags.append(ts)
        if tags:
            self._conn.executemany(
                "INSERT INTO tags(tag, page) VALUES (?, ?)",
                [(t, page_path) for t in tags],
            )
        if self._fts:
            self._conn.execute("DELETE FROM pages_fts WHERE path = ?", (page_path,))
            self._conn.execute(
                "INSERT INTO pages_fts(path, body) VALUES (?, ?)",
                (page_path, md_text),
            )

    def remove_page(self, page_path: str) -> None:
        self._conn.execute("DELETE FROM pages WHERE path = ?", (page_path,))
        self._conn.execute("DELETE FROM links WHERE src = ?", (page_path,))
        self._conn.execute("DELETE FROM tags WHERE page = ?", (page_path,))
        self._conn.execute("DELETE FROM aliases WHERE page = ?", (page_path,))
        if self._fts:
            self._conn.execute("DELETE FROM pages_fts WHERE path = ?", (page_path,))
        self._conn.commit()

    def rename_page(self, old: str, new: str) -> None:
        """Move entries from old to new in pages and links tables.

        Does NOT rewrite wikilink markdown text inside other pages; that's
        a higher-level operation (see Notebook.rename_page callers)."""
        c = self._conn
        c.execute("UPDATE pages SET path = ? WHERE path = ?", (new, old))
        c.execute("UPDATE links SET src = ? WHERE src = ?", (new, old))
        c.execute("UPDATE links SET dst = ? WHERE dst = ?", (new, old))
        c.execute("UPDATE tags SET page = ? WHERE page = ?", (new, old))
        c.execute("UPDATE aliases SET page = ? WHERE page = ?", (new, old))
        if self._fts:
            c.execute("UPDATE pages_fts SET path = ? WHERE path = ?", (new, old))
        c.commit()

    def delete_page_and_cleanup(self, page: str) -> int:
        """Delete the page file, remove from index. Returns inbound-link count
        (for display/confirmation before delete; caller should check this)."""
        inbound_count = len(self.backlinks(page))
        self.notebook.delete_page(page)
        self.remove_page(page)
        return inbound_count

    def move_page_and_rewrite(self, old: str, new: str) -> list[str]:
        """Alias for rename_page_and_rewrite; semantically a move to a
        different parent (same mechanics)."""
        return self.rename_page_and_rewrite(old, new)

    def copy_page(self, src: str, dst: str) -> None:
        """Duplicate page on disk and index the new page."""
        self.notebook.copy_page(src, dst)
        self.update_page(dst)

    def rename_page_and_rewrite(self, old: str, new: str) -> list[str]:
        """Rename a page on disk, relabel in the index, and rewrite all
        `[[old]]` references in other pages. Returns the list of page paths
        whose source was modified."""
        # Find inbound pages BEFORE we relabel (backlinks table still keys by old).
        inbound = self.backlinks(old)
        # Also rename child page paths that start with `old:` in index tables.
        self.notebook.rename_page(old, new)
        self.rename_page(old, new)
        # Cascade-relabel descendants (index rows only; filesystem cascade
        # was handled by Notebook's directory rename).
        old_prefix = old + ":"
        new_prefix = new + ":"
        c = self._conn
        descendants = [
            r["path"] for r in c.execute(
                "SELECT path FROM pages WHERE path LIKE ?",
                (old_prefix + "%",),
            ).fetchall()
        ]
        for dpath in descendants:
            ndpath = new_prefix + dpath[len(old_prefix):]
            c.execute("UPDATE pages SET path = ? WHERE path = ?", (ndpath, dpath))
            c.execute("UPDATE links SET src = ? WHERE src = ?", (ndpath, dpath))
        c.commit()

        modified: list[str] = []
        old_prefix_colon = old + ":"
        for src_page in inbound:
            if src_page == old:
                src_page_now = new
            elif src_page.startswith(old_prefix_colon):
                # A descendant of the renamed page also moved.
                src_page_now = new + ":" + src_page[len(old_prefix_colon):]
            else:
                src_page_now = src_page
            if not self.notebook.exists(src_page_now):
                continue
            text = self.notebook.get_page(src_page_now)
            rewritten = rewrite_wikilinks(text, old, new)
            if rewritten != text:
                self.notebook.save_page(src_page_now, rewritten)
                self.update_page(src_page_now, rewritten)
                modified.append(src_page_now)
        return modified

    # ---- queries ----

    def backlinks(self, page_path: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT src FROM links WHERE dst = ? ORDER BY src",
            (page_path,),
        ).fetchall()
        return [r["src"] for r in rows]

    def forward_links(self, page_path: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT dst FROM links WHERE src = ? ORDER BY dst",
            (page_path,),
        ).fetchall()
        return [r["dst"] for r in rows]

    def tags(self) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT tag, COUNT(DISTINCT page) AS n FROM tags "
            "GROUP BY tag ORDER BY n DESC, tag"
        ).fetchall()
        return [(r["tag"], r["n"]) for r in rows]

    def pages_with_tag(self, tag: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT page FROM tags WHERE tag = ? ORDER BY page",
            (tag,),
        ).fetchall()
        return [r["page"] for r in rows]

    def resolve_alias(self, alias: str) -> str | None:
        """Return the page path whose frontmatter aliases include `alias`, or None."""
        row = self._conn.execute(
            "SELECT page FROM aliases WHERE alias = ? LIMIT 1",
            (alias,),
        ).fetchone()
        return row["page"] if row else None

    def aliases_for(self, page_path: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT alias FROM aliases WHERE page = ? ORDER BY alias",
            (page_path,),
        ).fetchall()
        return [r["alias"] for r in rows]

    def all_pages(self) -> list[str]:
        rows = self._conn.execute("SELECT path FROM pages ORDER BY path").fetchall()
        return [r["path"] for r in rows]
