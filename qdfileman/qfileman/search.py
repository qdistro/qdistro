"""File search across a directory tree.

Ported from the dual qfileman implementation under qdistro-org2; same
public surface (``by_name`` / ``by_content``) with logging added for
unreadable files so journal-line assertions can see them.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path

log = logging.getLogger(__name__)

# How often (in lines) a content search re-checks the cancel flag while
# streaming a single file. Frequent enough to abandon a huge file promptly,
# sparse enough not to add measurable per-line overhead.
_CANCEL_EVERY = 256


class FileSearch:
    """Search a directory tree by filename pattern and/or file content."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def by_name(
        self,
        pattern: str = "*",
        hidden: bool = False,
        max_depth: int | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[Path]:
        """Yield paths whose basename matches ``pattern`` (glob syntax).

        ``is_cancelled``, when supplied, is polled once per directory and at
        every match; the walk stops as soon as it returns ``True`` so a worker
        thread can abandon a deep tree promptly.
        """
        for dirpath, dirnames, filenames in self._walk(self.root, max_depth):
            if is_cancelled is not None and is_cancelled():
                return
            if not hidden:
                # Drop hidden subdirs from traversal in-place so os.walk skips them.
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not hidden and name.startswith("."):
                    continue
                if fnmatch.fnmatch(name, pattern):
                    if is_cancelled is not None and is_cancelled():
                        return
                    yield dirpath / name

    def by_content(
        self,
        query: str,
        pattern: str = "*",
        hidden: bool = False,
        case_sensitive: bool = False,
        max_depth: int | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[tuple[Path, int, str]]:
        """Yield ``(path, line_no, line_text)`` for files containing ``query``.

        ``is_cancelled`` is honoured as in :meth:`by_name`: polled per
        directory and per matching file so a cancel doesn't have to wait for
        the whole tree to finish.
        """
        needle = query if case_sensitive else query.lower()
        for dirpath, dirnames, filenames in self._walk(self.root, max_depth):
            if is_cancelled is not None and is_cancelled():
                return
            if not hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not hidden and name.startswith("."):
                    continue
                if not fnmatch.fnmatch(name, pattern):
                    continue
                if is_cancelled is not None and is_cancelled():
                    return
                fp = dirpath / name
                try:
                    # Stream line-by-line rather than slurping the whole file:
                    # a multi-gigabyte file neither spikes memory nor blocks
                    # cancellation until the read completes. The cancel flag is
                    # polled every _CANCEL_EVERY lines so a huge single file
                    # can still be abandoned promptly.
                    with fp.open(
                        encoding="utf-8", errors="replace"
                    ) as handle:
                        for i, line in enumerate(handle, 1):
                            if (
                                is_cancelled is not None
                                and i % _CANCEL_EVERY == 0
                                and is_cancelled()
                            ):
                                return
                            line = line.rstrip("\n").rstrip("\r")
                            haystack = line if case_sensitive else line.lower()
                            if needle in haystack:
                                yield (fp, i, line)
                except OSError as e:
                    log.warning("search: could not read %s: %s", fp, e)
                    continue

    @staticmethod
    def _walk(
        root: Path, max_depth: int | None
    ) -> Iterator[tuple[Path, list[str], list[str]]]:
        """``os.walk`` with optional depth cap measured from ``root``."""
        root = root.resolve()
        for dirpath, dirnames, filenames in os.walk(root):
            dp = Path(dirpath)
            if max_depth is not None:
                try:
                    depth = len(dp.relative_to(root).parts)
                except ValueError:
                    depth = 0
                if depth > max_depth:
                    dirnames.clear()
                    continue
            yield (dp, dirnames, filenames)
