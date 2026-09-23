"""File system model for QFileMan."""

import ctypes
import ctypes.util
import errno
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# RENAME_NOREPLACE = 1 (linux/fs.h). renameat2(2) with this flag fails with
# EEXIST instead of silently clobbering, closing the TOCTOU window that a
# stat()-then-rename() check leaves open. Only on Linux; absent elsewhere.
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100


def _renameat2_noreplace(old: Path, new: Path) -> bool:
    """Atomically rename ``old`` -> ``new`` only if ``new`` doesn't exist.

    Returns True on success. Raises ``FileExistsError`` if ``new`` already
    exists (the whole point — fail closed, no clobber). Returns False when the
    syscall is unavailable (non-Linux, old kernel, or fs without support), so
    the caller can fall back to a best-effort stat-guarded rename.
    """
    if sys.platform != "linux":
        return False
    libc_name = ctypes.util.find_library("c")
    try:
        libc = ctypes.CDLL(libc_name or "libc.so.6", use_errno=True)
        syscall = libc.syscall
    except (OSError, AttributeError):
        return False
    # renameat2 syscall number: x86_64=316, aarch64=276, others via SYS_*.
    nr = {"x86_64": 316, "aarch64": 276, "armv7l": 382,
          "i686": 353, "ppc64le": 357, "s390x": 347}.get(os.uname().machine)
    if nr is None:
        return False
    syscall.restype = ctypes.c_long
    res = syscall(
        ctypes.c_long(nr),
        ctypes.c_int(_AT_FDCWD), ctypes.c_char_p(os.fsencode(old)),
        ctypes.c_int(_AT_FDCWD), ctypes.c_char_p(os.fsencode(new)),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if res == 0:
        return True
    eno = ctypes.get_errno()
    if eno == errno.EEXIST:
        raise FileExistsError(eno, os.strerror(eno), str(new))
    if eno in (errno.ENOSYS, errno.EINVAL):
        # Kernel or filesystem lacks renameat2/RENAME_NOREPLACE.
        return False
    raise OSError(eno, os.strerror(eno), str(old), None, str(new))


def is_safe_rename_name(name) -> bool:
    """Return True when rename input is a single basename."""
    if not isinstance(name, str) or not name:
        return False
    p = Path(name)
    return (
        "\x00" not in name
        and not p.is_absolute()
        and p.name == name
        and name not in (".", "..")
    )


def _same_path(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` denote the same existing inode.

    Renaming a file onto itself (e.g. a case-only rename on a
    case-insensitive fs, or a redundant rename) must not be treated as a
    clobber. ``os.path.samefile`` compares inodes; fall back to a textual
    compare when either path is missing.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normpath(a) == os.path.normpath(b)


def _is_clobber(old: Path, new: Path) -> bool:
    """True when renaming ``old`` to ``new`` would destroy a distinct file."""
    if not _occupied(new):
        return False
    return not _same_path(old, new)


def would_clobber(old_path, new_path) -> bool:
    """Public predicate: would moving ``old_path`` onto ``new_path`` destroy
    a distinct existing file? Used by the GUI to decide when to prompt."""
    return _is_clobber(Path(old_path), Path(new_path))


def effective_copy_target(source, dest) -> Path:
    """The path that a copy/move of ``source`` into ``dest`` actually writes.

    ``shutil.move``/``copy2`` place a source *inside* ``dest`` when ``dest``
    is an existing directory, so the real clobber candidate is
    ``dest/basename(source)`` in that case — not ``dest`` itself.
    """
    return _effective_target(Path(source), Path(dest))


def copy_move_conflict(source, dest, *, rsync: bool = False):
    """Name of an existing destination entry a copy/move would clobber, or None.

    Models the two distinct placement semantics the GUI uses:

    * **shutil** (and rsync of a *file*): the source lands at the effective
      target (``dest`` itself, or ``dest/basename`` when ``dest`` is an
      existing directory). One candidate.
    * **rsync of a *directory*** (``rsync=True``): qfileman passes ``src/`` with
      a trailing slash, so rsync merges the *contents* of ``source`` into
      ``dest``; the real clobber candidates are ``dest/<child>`` for each
      top-level child of ``source``.

    Returns the basename of the first occupied, distinct candidate (a broken
    symlink counts as occupied), or ``None`` when nothing would be destroyed.
    """
    src = Path(source)
    dst = Path(dest)
    if rsync and src.is_dir():
        # Merge-into-dest semantics: check each top-level child for a clash.
        try:
            children = sorted(src.iterdir())
        except OSError:
            children = []
        for child in children:
            cand = dst / child.name
            if _occupied(cand) and not _same_path(child, cand):
                return cand.name
        return None
    target = _effective_target(src, dst)
    if _occupied(target) and not _same_path(src, target):
        return target.name
    return None


def _effective_target(src: Path, dst: Path) -> Path:
    # ``Path.is_dir()`` follows symlinks, matching shutil.copy2/move, which use
    # os.path.isdir(dst) and so treat a symlink-to-directory AS a directory:
    # they write to ``dst/basename(src)``. Mirror that — a symlink pointing at
    # a directory must land at ``dst/src.name``, not overwrite the link itself.
    # A symlink-to-file (is_dir() False) or broken/absent dst stays a leaf.
    if dst.is_dir():
        return dst / src.name
    return dst


def _occupied(path: Path) -> bool:
    """True if anything (incl. a broken symlink) lives at ``path``.

    ``Path.exists()`` follows symlinks and returns False for a dangling one,
    yet that pathname is taken — ``shutil.move`` would replace it and
    ``shutil.copy2`` would write *through* it. Use ``lexists`` semantics so
    the guard never mistakes an occupied name for a free one.
    """
    return path.exists() or path.is_symlink()


def rename_exclusive(old: Path, new: Path, *, overwrite: bool = False) -> None:
    """Rename ``old`` -> ``new``, raising ``FileExistsError`` rather than
    clobbering a distinct ``new`` (unless ``overwrite=True``).

    A rename onto itself (redundant, or case-only on a case-insensitive fs) is
    a permitted no-op. With ``overwrite`` the rename is unconditional. Without
    it, ``renameat2(RENAME_NOREPLACE)`` makes the check+rename atomic; if the
    syscall is unavailable, fall back to a stat guard plus ``os.rename``.
    """
    old = Path(old)
    new = Path(new)
    if overwrite or _same_path(old, new):
        os.rename(old, new)
        return
    try:
        if _renameat2_noreplace(old, new):
            return
    except FileExistsError:
        raise
    # Fallback path: no renameat2/RENAME_NOREPLACE here. Stat-guard, then
    # rename. The residual TOCTOU window is unavoidable without the syscall,
    # but this is strictly safer than the previous unconditional rename.
    if _occupied(new) and not _same_path(old, new):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(new))
    os.rename(old, new)


class FileItem:
    """Represents a file or directory in the file manager."""

    def __init__(self, path):
        self.path = Path(path)
        self._stat = None  # cached lazily on first stat-backed access

    def _cached_stat(self):
        """Return ``path.stat()``, caching on first call.

        Multiple property reads (``size`` plus ``modified``) on the same
        item are common during sort + status-line rendering; caching keeps
        directory listings O(n) stat calls instead of O(n*k).
        """
        if self._stat is None:
            self._stat = self.path.stat()
        return self._stat

    @property
    def name(self):
        return self.path.name

    @property
    def is_dir(self):
        return self.path.is_dir()

    @property
    def is_file(self):
        return self.path.is_file()

    @property
    def is_symlink(self):
        return self.path.is_symlink()

    @property
    def size(self):
        if self.is_dir:
            return 0
        try:
            return self._cached_stat().st_size
        except OSError:
            return 0

    @property
    def modified(self):
        try:
            return datetime.fromtimestamp(self._cached_stat().st_mtime)
        except OSError:
            return None

    @property
    def extension(self):
        return self.path.suffix.lower()

    def __repr__(self):
        return f"FileItem({self.path})"


class FileModel:
    """Model that provides file listing and operations."""

    def __init__(self, current_path=None):
        self._current_path = Path(current_path or os.path.expanduser("~"))
        self._files = None  # None means "not yet loaded"
        self._show_hidden = False
        self._sort_by = "name"
        self._sort_order = "asc"
        self._filters = []

    @property
    def current_path(self):
        return self._current_path

    @property
    def show_hidden(self) -> bool:
        """Whether dotfiles are included in :meth:`get_files`."""
        return self._show_hidden

    @property
    def sort_by(self) -> str:
        """The current sort key (``name`` / ``size`` / ``date`` / ``type``)."""
        return self._sort_by

    @property
    def sort_order(self) -> str:
        """The current sort direction (``asc`` / ``desc``)."""
        return self._sort_order

    def apply_state(
        self,
        *,
        show_hidden: bool | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> None:
        """Mutate several view fields at once and refresh once.

        Each of :meth:`set_show_hidden` and :meth:`set_sort` refreshes the
        listing on its own. When the caller wants to update more than one
        of these at the same time, use this method to avoid redundant
        refreshes.
        """
        if show_hidden is not None:
            self._show_hidden = bool(show_hidden)
        if sort_by is not None:
            self._sort_by = sort_by
        if sort_order is not None:
            self._sort_order = sort_order
        self.refresh()

    def set_path(self, path):
        """Change current directory."""
        p = Path(path)
        if p.is_dir():
            self._current_path = p
            self._files = None  # Invalidate cache
            return True
        return False

    def go_up(self):
        """Go to parent directory. Returns False at filesystem root."""
        parent = self._current_path.parent
        if parent != self._current_path:
            self._current_path = parent
            self._files = None
            return True
        return False

    def go_home(self):
        """Go to home directory."""
        self._current_path = Path(os.path.expanduser("~"))
        self._files = None

    def refresh(self):
        """Refresh file listing."""
        self._files = self._load_files()

    def _load_files(self):
        """Load files from current directory."""
        files = []
        try:
            entries = list(self._current_path.iterdir())
        except PermissionError as e:
            log.warning("permission denied listing %s: %s", self._current_path, e)
            return []
        except OSError as e:
            log.warning("error listing %s: %s", self._current_path, e)
            return []

        for entry in entries:
            if not self._show_hidden and entry.name.startswith("."):
                continue
            files.append(FileItem(entry))

        # Pipe paths through each registered FileFilter, then rebuild the
        # FileItem list once at the end. Each filter consumes and returns
        # path strings; intermediate FileItem reconstruction would be
        # wasted work since the items themselves carry no filter state.
        if self._filters:
            paths = [str(fi.path) for fi in files]
            for f in self._filters:
                paths = f.filter_files(paths)
            files = [FileItem(p) for p in paths]

        files = self._sort_files(files)
        return files

    def _sort_files(self, files):
        """Sort files by current sort settings."""
        reverse = self._sort_order == "desc"

        def sort_key(f):
            if self._sort_by == "name":
                return f.name.lower()
            elif self._sort_by == "size":
                return f.size
            elif self._sort_by == "date":
                return f.modified.timestamp() if f.modified else 0
            elif self._sort_by == "type":
                return (f.extension, f.name.lower())
            return f.name.lower()

        return sorted(files, key=sort_key, reverse=reverse)

    def get_files(self):
        """Get list of files in current directory."""
        if self._files is None:
            self.refresh()
        return self._files

    def set_show_hidden(self, show):
        """Set whether to show hidden files."""
        self._show_hidden = show
        self.refresh()

    def set_sort(self, sort_by, sort_order=None):
        """Set sort options."""
        self._sort_by = sort_by
        if sort_order:
            self._sort_order = sort_order
        self.refresh()

    def add_filter(self, file_filter):
        """Register a FileFilter-style object.

        ``file_filter`` must expose a ``filter_files(paths) -> paths`` method
        (the contract of :class:`qfileman.plugin.FileFilter`). Plain functions
        are not accepted.
        """
        if not hasattr(file_filter, "filter_files"):
            raise TypeError(
                "add_filter requires an object with a filter_files() method; "
                f"got {type(file_filter).__name__}"
            )
        self._filters.append(file_filter)
        self.refresh()

    def clear_filters(self):
        """Clear all filters."""
        self._filters = []
        self.refresh()

    # File operations
    def create_directory(self, name):
        """Create a directory in current path."""
        try:
            (self._current_path / name).mkdir()
            self.refresh()
            return True
        except OSError as e:
            log.warning("create_directory(%s) failed: %s", name, e)
            return False

    def delete(self, path):
        """Delete a file or directory."""
        p = Path(path)
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            self.refresh()
            return True
        except OSError as e:
            log.warning("delete(%s) failed: %s", path, e)
            return False

    def rename(self, old_path, new_name, *, overwrite=False):
        """Rename a file or directory.

        Refuses to clobber an existing sibling unless ``overwrite=True``; a
        bare ``Path.rename`` would silently destroy it. The non-overwrite path
        prefers ``renameat2(RENAME_NOREPLACE)`` so the no-clobber check and the
        rename are a single atomic syscall (TOCTOU-free); where that's
        unavailable it degrades to a stat-guarded ``os.rename`` (a small race
        window, but never worse than the unconditional rename it replaced).
        """
        if not is_safe_rename_name(new_name):
            log.warning("rename(%s -> %s) rejected: unsafe target name",
                        old_path, new_name)
            return False
        old = Path(old_path)
        new = old.parent / new_name
        try:
            rename_exclusive(old, new, overwrite=overwrite)
            self.refresh()
            return True
        except FileExistsError:
            log.warning("rename(%s -> %s) rejected: destination exists",
                        old_path, new_name)
            return False
        except OSError as e:
            log.warning("rename(%s -> %s) failed: %s", old_path, new_name, e)
            return False

    def copy(self, source, dest, *, overwrite=False):
        """Copy a file or directory.

        Resolves the effective target (``shutil`` drops a source *into* an
        existing destination directory) and refuses to overwrite it unless
        ``overwrite=True``.
        """
        src = Path(source)
        dst = Path(dest)
        target = _effective_target(src, dst)
        if not overwrite and _occupied(target):
            log.warning("copy(%s -> %s) rejected: destination exists",
                        source, dest)
            return False
        try:
            if src.is_dir():
                # copytree refuses an existing dir on its own; only reach
                # here with overwrite, so allow merging into it.
                shutil.copytree(src, dst, dirs_exist_ok=overwrite)
            else:
                shutil.copy2(src, dst)
            self.refresh()
            return True
        except OSError as e:
            log.warning("copy(%s -> %s) failed: %s", source, dest, e)
            return False

    def move(self, source, dest, *, overwrite=False):
        """Move a file or directory.

        ``shutil.move`` silently replaces an existing file destination; guard
        it the same way ``copy`` does unless ``overwrite=True``.
        """
        src = Path(source)
        dst = Path(dest)
        target = _effective_target(src, dst)
        if not overwrite and _occupied(target):
            log.warning("move(%s -> %s) rejected: destination exists",
                        source, dest)
            return False
        try:
            shutil.move(src, dst)
            self.refresh()
            return True
        except OSError as e:
            log.warning("move(%s -> %s) failed: %s", source, dest, e)
            return False
