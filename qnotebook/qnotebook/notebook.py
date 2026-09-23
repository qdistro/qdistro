"""Markdown notebook: a directory tree of .md files.

Page paths use `:` as the separator in the API (e.g. `Foo:Bar`). On disk,
`Foo:Bar` lives at `<root>/Foo/Bar.md`. A page with children gets both a
`Foo/Bar.md` file and a `Foo/Bar/` sibling directory for its children.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import safe_save
from . import snapshots as _snapshots

DOTDIR = ".qnotebook"


def _page_parts(page: str) -> list[str]:
    if not page:
        raise ValueError("empty page path")
    parts = page.split(":")
    for p in parts:
        if (
            not p
            or p in (".", "..")
            or "/" in p
            or "\\" in p
            or any(ord(ch) < 32 for ch in p)
        ):
            raise ValueError(f"invalid page component: {p!r}")
    return parts


def page_to_relpath(page: str) -> Path:
    """`Foo:Bar` -> `Foo/Bar.md`."""
    parts = _page_parts(page)
    return Path(*parts[:-1], parts[-1] + ".md")


def relpath_to_page(rel: Path) -> str:
    """`Foo/Bar.md` -> `Foo:Bar`."""
    parts = list(rel.parts)
    if not parts or not parts[-1].endswith(".md"):
        raise ValueError(f"not a markdown path: {rel}")
    parts[-1] = parts[-1][:-3]
    return ":".join(parts)


def page_to_dirpath(page: str) -> Path:
    """Directory that holds children of `page`."""
    return Path(*_page_parts(page))


@dataclass(frozen=True)
class PageRef:
    path: str  # colon-separated

    @property
    def name(self) -> str:
        return self.path.rsplit(":", 1)[-1]

    @property
    def parent(self) -> PageRef | None:
        if ":" not in self.path:
            return None
        return PageRef(self.path.rsplit(":", 1)[0])


class Notebook:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / DOTDIR).mkdir(exist_ok=True)

    # ---- filesystem helpers ----

    def file_for(self, page: str) -> Path:
        return self.root / page_to_relpath(page)

    def dir_for(self, page: str) -> Path:
        return self.root / page_to_dirpath(page)

    def exists(self, page: str) -> bool:
        return self.file_for(page).is_file()

    # ---- iteration ----

    def pages(self) -> Iterator[PageRef]:
        """Yield all pages in depth-first, sorted order."""
        for rel in self._walk(self.root, Path()):
            yield PageRef(relpath_to_page(rel))

    def _walk(self, abs_dir: Path, rel_dir: Path) -> Iterator[Path]:
        try:
            entries = sorted(
                e for e in abs_dir.iterdir()
                if not e.name.startswith(".")
            )
        except (FileNotFoundError, PermissionError):
            return
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file() and e.suffix == ".md"]
        # Emit .md files at this level sorted by name
        for f in sorted(files, key=lambda p: p.name.lower()):
            yield rel_dir / f.name
        # Recurse into directories
        for d in sorted(dirs, key=lambda p: p.name.lower()):
            yield from self._walk(d, rel_dir / d.name)

    def children(self, page: str | None) -> list[PageRef]:
        """Direct children of `page` (or root if None)."""
        if page is None:
            abs_dir = self.root
            prefix = ""
        else:
            abs_dir = self.dir_for(page)
            prefix = page + ":"
        if not abs_dir.is_dir():
            return []
        out: list[PageRef] = []
        seen: set[str] = set()
        for e in sorted(abs_dir.iterdir(), key=lambda p: p.name.lower()):
            if e.name.startswith("."):
                continue
            if e.is_file() and e.suffix == ".md":
                name = e.stem
                seen.add(name)
                out.append(PageRef(prefix + name))
            elif e.is_dir():
                # Represent as a page even if no .md exists (placeholder parent).
                if e.name not in seen:
                    out.append(PageRef(prefix + e.name))
                    seen.add(e.name)
        # Deduplicate while preserving order
        uniq: list[PageRef] = []
        seen2: set[str] = set()
        for p in out:
            if p.path not in seen2:
                uniq.append(p)
                seen2.add(p.path)
        return sorted(uniq, key=lambda p: p.name.lower())

    def has_children(self, page: str) -> bool:
        return self.dir_for(page).is_dir() and any(
            not e.name.startswith(".")
            for e in self.dir_for(page).iterdir()
        )

    # ---- CRUD ----

    def get_page(self, page: str) -> str:
        f = self.file_for(page)
        if not f.is_file():
            return ""
        return f.read_text(encoding="utf-8")

    def save_page(self, page: str, md_text: str,
                  load_result: safe_save.LoadResult | None = None,
                  *,
                  allow_subprocess: bool = True,
                  ) -> safe_save.SaveResult:
        """Atomic save via SafeWriter (same-dir tempfile + fsync + merge ladder
        when ``load_result`` is supplied). Takes a pre-save snapshot of the
        current on-disk bytes so recent states are recoverable from
        File → Snapshots."""
        f = self.file_for(page)
        if f.is_file():
            try:
                _snapshots.take_snapshot(self.root, f)
            except Exception:
                pass
        text = md_text.rstrip("\n") + "\n" if md_text else ""
        data = text.encode("utf-8")
        if load_result is None:
            safe_save.atomic_write(f, data)
            return safe_save.SaveResult(status="ok", bytes=data, rung="atomic")
        from . import nb_settings
        strict = bool(nb_settings.get(self.root, "strict_preserve", True))
        return safe_save.SafeWriter.save(
            f, data, load_result, root=self.root, strict_preserve=strict,
            allow_subprocess=allow_subprocess,
        )

    def load_for_save(self, page: str) -> safe_save.LoadResult:
        return safe_save.SafeWriter.load(self.file_for(page))

    def snapshots(self, page: str) -> list[_snapshots.Snapshot]:
        return _snapshots.list_snapshots(self.root, self.file_for(page))

    def restore_snapshot(self, snap: _snapshots.Snapshot) -> None:
        _snapshots.restore(self.root, snap)

    def create_page(self, page: str, initial: str = "") -> PageRef:
        if self.exists(page):
            raise FileExistsError(page)
        self.save_page(page, initial)
        return PageRef(page)

    def delete_page(self, page: str) -> None:
        """Delete the .md file; if the sibling child directory exists and
        is empty (no descendants), remove it too. Also clean up empty
        ancestor directories."""
        f = self.file_for(page)
        if f.is_file():
            f.unlink()
        d = self.dir_for(page)
        self._rmdir_if_empty(d)
        # Walk up and clean empty ancestor directories (but not the root).
        parent = f.parent
        while parent != self.root and parent.is_dir():
            if not self._rmdir_if_empty(parent):
                break
            parent = parent.parent

    def _rmdir_if_empty(self, d: Path) -> bool:
        if not d.is_dir() or d == self.root:
            return False
        try:
            empty = not any(not e.name.startswith(".") for e in d.iterdir())
        except (FileNotFoundError, PermissionError):
            return False
        if not empty:
            return False
        try:
            d.rmdir()
            return True
        except OSError:
            return False

    def move_page(self, old: str, new: str) -> None:
        """Move a page to a different parent path. Same filesystem mechanics
        as rename_page (both are just `path -> new path`)."""
        self.rename_page(old, new)

    def copy_page(self, src: str, dst: str) -> None:
        """Duplicate src.md to dst.md verbatim. No link rewriting."""
        if not self.exists(src):
            raise FileNotFoundError(src)
        if self.exists(dst):
            raise FileExistsError(dst)
        body = self.get_page(src)
        self.save_page(dst, body)

    def rename_page(self, old: str, new: str) -> None:
        """Rename a page file. Also moves child directory if present.

        Link rewriting in other pages is handled by `Index.rename_page`
        when an index is attached; callers that want inbound-link rewrites
        should go through `MainWindow`/`Index.rename_page`."""
        if not self.exists(old):
            raise FileNotFoundError(old)
        if self.exists(new):
            raise FileExistsError(new)
        new_file = self.file_for(new)
        new_file.parent.mkdir(parents=True, exist_ok=True)
        self.file_for(old).rename(new_file)
        old_dir = self.dir_for(old)
        if old_dir.is_dir():
            new_dir = self.dir_for(new)
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            old_dir.rename(new_dir)
