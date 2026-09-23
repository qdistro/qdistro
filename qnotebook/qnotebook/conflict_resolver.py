"""Syncthing conflict resolver dialog — core logic, GUI-free where possible."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import safe_save
from .sync_conflict import ConflictFile


class ResolverActions:
    """Pure logic for resolving a single conflict pair. GUI (dialog) wraps
    this; tests exercise it directly."""

    @staticmethod
    def keep_mine(cf: ConflictFile, root: Path) -> None:
        """Delete the conflict file; keep the original as-is."""
        try:
            cf.path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def keep_theirs(cf: ConflictFile, root: Path) -> None:
        """Replace original with conflict-file contents, then delete conflict."""
        data = cf.path.read_bytes()
        safe_save.atomic_write(cf.original, data)
        try:
            cf.path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def save_both(cf: ConflictFile, root: Path) -> None:
        """Keep both: rename conflict to <stem>-conflict-<date>.<ext>."""
        new_name = (f"{cf.original.stem}-conflict-{cf.date}-{cf.time}"
                    f"-{cf.device}{cf.original.suffix}")
        target = cf.path.with_name(new_name)
        try:
            cf.path.rename(target)
        except OSError:
            pass

    @staticmethod
    def merge(cf: ConflictFile, root: Path,
              resolve: Callable[[bytes, bytes, bytes], bytes] | None = None
              ) -> safe_save.SaveResult | None:
        """Attempt a 3-way merge.

        Base = original bytes (best guess — we don't have the true common
        ancestor). Ours = original on disk. Theirs = conflict file.
        """
        if not cf.original.is_file():
            return None
        ours = cf.original.read_bytes()
        theirs = cf.path.read_bytes()
        # No true base — use ours as base so disjoint lines in theirs merge in.
        base = ours
        # Delegate to git merge-file via SafeWriter internal helper
        from .safe_save import _git_merge_file
        clean, out = _git_merge_file(base, ours, theirs)
        if clean:
            safe_save.atomic_write(cf.original, out)
            try:
                cf.path.unlink()
            except FileNotFoundError:
                pass
            return safe_save.SaveResult(status="ok", bytes=out, rung="git-merge-file")
        # Conflict — caller should pop the 3-pane dialog
        return safe_save.SaveResult(
            status="conflict", base=base, ours=ours, theirs=theirs,
            rung="conflict",
        )

    @staticmethod
    def skip(cf: ConflictFile, root: Path) -> None:
        """No-op — leave both files for later."""
        return None
