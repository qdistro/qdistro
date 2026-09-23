"""Full-text search across a notebook."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Hit:
    page_path: str
    line_no: int  # 1-based
    line_text: str
    match_span: tuple[int, int]  # (start, end) within line_text


def _has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


class Search:
    """Plain-text + regex search over all .md pages.

    When the Index is provided and FTS5 is available, `query()` prefilters
    candidate pages via the FTS table before scanning. Without FTS5 we
    fall back to a full scan of every .md file in the notebook.
    """

    def __init__(self, notebook, index=None) -> None:
        self.notebook = notebook
        self.index = index

    def query(
        self,
        text: str,
        case: bool = False,
        whole_word: bool = False,
        regex: bool = False,
    ) -> list[Hit]:
        if not text:
            return []
        if regex:
            flags = 0 if case else re.IGNORECASE
            try:
                pat = re.compile(text, flags)
            except re.error:
                return []
        else:
            pattern = re.escape(text)
            if whole_word:
                pattern = r"\b" + pattern + r"\b"
            flags = 0 if case else re.IGNORECASE
            pat = re.compile(pattern, flags)

        candidates: Iterable[str]
        if self.index is not None and self.index.has_fts() and not regex:
            candidates = self.index.fts_candidates(text)
        else:
            candidates = (p.path for p in self.notebook.pages())

        hits: list[Hit] = []
        for page_path in candidates:
            body = self.notebook.get_page(page_path)
            for line_no, line in enumerate(body.splitlines(), start=1):
                for m in pat.finditer(line):
                    hits.append(
                        Hit(page_path, line_no, line, (m.start(), m.end()))
                    )
        return hits
