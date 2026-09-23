"""Archive plugin for QFileMan.

Adds *Extract Here*, *Extract To...*, and *Create Archive...* entries
to the context menu. Each operation shells out to whichever external
tool fits the format (``tar``, ``unzip``, ``7z``); none of them are
implemented in-process, mirroring how Krusader, Dolphin, and Midnight
Commander handle archives.

Format detection is by extension, which is good enough for the common
case and trivially testable. ``detect_format`` and the ``*_argv``
builders are pure functions so they can be exercised without spawning
a process.
"""

from __future__ import annotations

import logging
import os
import stat
import tarfile
import zipfile
from collections.abc import Sequence

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


EXTRACT_EXTS = (
    ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
    ".tar.zst", ".zip", ".7z", ".rar",
)


def detect_format(path: str) -> str | None:
    """Return a short format key for ``path`` or ``None`` if unrecognized.

    Returned keys: ``"tar"``, ``"tar.gz"``, ``"tar.bz2"``, ``"tar.xz"``,
    ``"tar.zst"``, ``"zip"``, ``"7z"``, ``"rar"``.
    """
    lower = path.lower()
    # Check the longest suffixes first so ``foo.tar.gz`` isn't read as ``.gz``.
    pairs = (
        (".tar.gz", "tar.gz"), (".tgz", "tar.gz"),
        (".tar.bz2", "tar.bz2"), (".tbz2", "tar.bz2"),
        (".tar.xz", "tar.xz"), (".txz", "tar.xz"),
        (".tar.zst", "tar.zst"),
        (".tar", "tar"),
        (".zip", "zip"),
        (".7z", "7z"),
        (".rar", "rar"),
    )
    for suffix, key in pairs:
        if lower.endswith(suffix):
            return key
    return None


def extract_argv(path: str, dest_dir: str) -> list[str] | None:
    """Build an argv to extract ``path`` into ``dest_dir``. None if unknown."""
    fmt = detect_format(path)
    if fmt is None:
        return None
    if fmt.startswith("tar"):
        # ``tar`` auto-detects the compression with -a/--auto-compress,
        # but ``-xf`` alone has handled all the listed variants for years.
        # Keep existing files instead of clobbering them. Archive entry
        # validation still belongs in a future in-process/staging extractor.
        return ["tar", "--keep-old-files", "-xf", path, "-C", dest_dir]
    if fmt == "zip":
        return ["unzip", "-n", path, "-d", dest_dir]
    if fmt == "7z":
        return ["7z", "x", f"-o{dest_dir}", "-aos", path]
    if fmt == "rar":
        # ``unrar`` is the canonical extractor; ``7z`` can also read rar.
        return ["unrar", "x", "-o-", path, dest_dir + os.sep]
    return None


# --------------------------------------------------------------------------
# Pre-extraction path containment (defense-in-depth)
# --------------------------------------------------------------------------
#
# The extractors above (``tar``, ``unzip``, ``7z``, ``unrar``) each apply
# their own traversal protections, and on a GNU-tar host ``tar`` already
# strips a leading ``/`` and refuses members containing ``..``. We do not
# want to *rely* on that: a busybox/other ``tar``, or a historically weaker
# ``unzip``, may not give the same guarantees, and a member symlink that
# points outside the destination (later written through) escapes a plain
# pathname check entirely.
#
# So for the formats the standard library can enumerate without extracting
# (tar — except zstd-compressed — and zip), we run our own in-process check
# first and refuse the whole operation if any member would land — or, via a
# link, redirect a write — outside ``dest_dir``. ``7z``, ``rar`` and
# ``.tar.zst`` have no usable stdlib reader (``tarfile`` gained zstd only in
# 3.14), so they remain extractor-dependent (unchanged behavior); this is an
# intentional, documented gap rather than a fail-closed refusal — we must not
# block extraction of a perfectly valid archive just because stdlib cannot
# decompress it.


# Formats whose members the standard library can enumerate without
# extracting. ``tar.zst`` is deliberately excluded: ``tarfile`` cannot
# decompress zstd before Python 3.14, and we will not fail-closed on a
# format the external extractor handles fine.
_INTROSPECTABLE = ("tar", "tar.gz", "tar.bz2", "tar.xz", "zip")

# Upper bound on a zip symlink target we will read during the preflight.
# Real targets are short paths; this caps memory against a hostile member
# marked as a symlink but carrying a large payload (PATH_MAX is ~4 KiB).
_MAX_SYMLINK_BYTES = 64 * 1024


def _resolves_within(dest_dir: str, *parts: str) -> bool:
    """True iff ``parts`` joined under ``dest_dir`` stays inside ``dest_dir``.

    ``parts`` are joined onto the (normalized, absolute) destination and the
    result is normalized; we then require it to equal ``dest_dir`` or sit
    beneath it. ``os.path.normpath`` collapses ``..`` lexically, which is the
    right semantics here — we are reasoning about archive member names, not
    touching the filesystem, so we must not let an on-disk symlink in the
    real ``dest_dir`` change the verdict.
    """
    base = os.path.normpath(os.path.abspath(dest_dir))
    target = os.path.normpath(os.path.join(base, *parts))
    return target == base or target.startswith(base + os.sep)


def archive_unsafe_members(path: str, dest_dir: str) -> list[str]:
    """Return member names in ``path`` that would extract outside ``dest_dir``.

    Covers tar and zip (the formats :mod:`tarfile` / :mod:`zipfile` can
    enumerate without extraction). An empty list means "no unsafe members
    found — safe to hand to the external extractor". A member is unsafe if:

    * its name is absolute or escapes ``dest_dir`` via ``..``; or
    * it is a symlink whose target is absolute, or whose relative target —
      resolved from the link's own parent directory — escapes ``dest_dir``
      (tar symlinks and Info-ZIP unix-mode symlinks); or
    * (tar only) it is a hardlink whose target is absolute, or whose
      relative target — resolved from the extraction *root* — escapes
      ``dest_dir``.

    Formats with no usable stdlib reader (7z, rar, tar.zst) return ``[]``
    (not validated here). A malformed/unreadable tar or zip *that we should
    have been able to read* returns a single synthetic ``"<unreadable
    archive>"`` entry so callers **fail closed** rather than extracting an
    archive we could not inspect.
    """
    fmt = detect_format(path)
    if fmt not in _INTROSPECTABLE:
        # None, 7z, rar, tar.zst: no in-process enumeration; left to the
        # external extractor (unchanged behavior).
        return []

    try:
        if fmt == "zip":
            return _unsafe_zip_members(path, dest_dir)
        return _unsafe_tar_members(path, dest_dir)
    except (tarfile.TarError, zipfile.BadZipFile, OSError, EOFError,
            ValueError) as e:
        # Fail closed: an archive we cannot parse is one we will not extract.
        log.warning("Could not inspect archive %s for traversal: %s", path, e)
        return ["<unreadable archive>"]


def _unsafe_tar_members(path: str, dest_dir: str) -> list[str]:
    unsafe: list[str] = []
    with tarfile.open(path, mode="r:*") as tf:
        for member in tf.getmembers():
            name = member.name
            if not _resolves_within(dest_dir, name):
                unsafe.append(name)
                continue
            if member.issym():
                # A symlink's target is interpreted relative to the link's
                # OWN directory (where the link will be created).
                if _link_escapes(dest_dir, os.path.dirname(name),
                                 member.linkname):
                    unsafe.append(f"{name} -> {member.linkname}")
            elif member.islnk():
                # A hardlink's target (POSIX/tar semantics) is a path
                # relative to the extraction ROOT (dest_dir), NOT to the
                # link member's directory.
                if _link_escapes(dest_dir, "", member.linkname):
                    unsafe.append(f"{name} -> {member.linkname}")
    return unsafe


def _link_escapes(dest_dir: str, base_rel: str, link: str) -> bool:
    """True iff ``link`` resolved from ``dest_dir/base_rel`` escapes ``dest_dir``.

    Absolute link targets are always treated as escaping: they are
    surprising and extractor-dependent, so we reject them outright.
    """
    if os.path.isabs(link):
        return True
    return not _resolves_within(dest_dir, base_rel, link)


def _unsafe_zip_members(path: str, dest_dir: str) -> list[str]:
    unsafe: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            name = info.filename
            # Zip stores POSIX-style names; a backslash can smuggle a
            # separator past a naive check on some platforms, so normalize.
            candidate = name.replace("\\", "/")
            if not _resolves_within(dest_dir, candidate):
                unsafe.append(name)
                continue
            # Info-ZIP ``unzip`` restores symlinks recorded via the Unix
            # mode bits in the high half of ``external_attr``; the link
            # target is the entry's content. Validate it the same way as a
            # tar symlink (resolved from the link's own directory).
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                # The link target is the entry content. A real symlink target
                # is a short path; a member marked S_IFLNK but carrying a huge
                # payload is malformed/hostile — refuse it without reading
                # (and don't let it balloon memory during the preflight).
                if info.file_size > _MAX_SYMLINK_BYTES:
                    unsafe.append(f"{name} -> <oversize link>")
                    continue
                try:
                    link = zf.read(name).decode("utf-8", "surrogateescape")
                except (OSError, zipfile.BadZipFile, EOFError, ValueError):
                    unsafe.append(f"{name} -> <unreadable link>")
                    continue
                link = link.replace("\\", "/")
                if _link_escapes(dest_dir, os.path.dirname(candidate), link):
                    unsafe.append(f"{name} -> {link}")
    return unsafe


def create_argv(archive_path: str, sources: Sequence[str]) -> list[str] | None:
    """Build an argv that packs ``sources`` into ``archive_path``. None if unknown."""
    fmt = detect_format(archive_path)
    if fmt is None:
        return None
    sources = list(sources)
    if fmt == "tar":
        return ["tar", "-cf", archive_path, *sources]
    if fmt == "tar.gz":
        return ["tar", "-czf", archive_path, *sources]
    if fmt == "tar.bz2":
        return ["tar", "-cjf", archive_path, *sources]
    if fmt == "tar.xz":
        return ["tar", "-cJf", archive_path, *sources]
    if fmt == "tar.zst":
        return ["tar", "--zstd", "-cf", archive_path, *sources]
    if fmt == "zip":
        return ["zip", "-r", archive_path, *sources]
    if fmt == "7z":
        return ["7z", "a", archive_path, *sources]
    # rar creation requires a non-free binary; intentionally not offered.
    return None


def is_archive(path: str) -> bool:
    return detect_format(path) is not None


class ArchivePlugin(MenuProvider):
    name = "archive"
    description = "Extract and create archives (tar, zip, 7z, rar)"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path:
            return []
        items: list = []
        if os.path.isfile(path) and is_archive(path):
            items.append(("Extract Here", self._extract_here))
            items.append(("Extract To...", self._extract_to))
        # Creating an archive is offered for any path — single file, directory,
        # or even a path that doesn't exist (lets you cancel out of the dialog).
        items.append(("Create Archive...", self._create_archive))
        return items

    # ----------------------------------------------------------------- actions
    def _extract_here(self, path: str) -> None:
        dest = os.path.dirname(path) or "."
        self._extract_into(path, dest)

    def _extract_to(self, path: str) -> None:
        from PyQt6.QtWidgets import QFileDialog

        dest = QFileDialog.getExistingDirectory(
            None, "Extract To", os.path.dirname(path) or os.getcwd()
        )
        if not dest:
            return
        self._extract_into(path, dest)

    def _extract_into(self, path: str, dest: str) -> None:
        argv = extract_argv(path, dest)
        if argv is None:
            self._warn(f"Don't know how to extract: {path}")
            return
        unsafe = archive_unsafe_members(path, dest)
        if unsafe:
            preview = ", ".join(unsafe[:5])
            if len(unsafe) > 5:
                preview += f", … (+{len(unsafe) - 5} more)"
            self._warn(
                "Refusing to extract: archive contains entries that would "
                f"write outside the destination:\n{preview}"
            )
            return
        self._run(f"Extract {os.path.basename(path)}", argv, cwd=dest)

    def _create_archive(self, path: str) -> None:
        from PyQt6.QtWidgets import QFileDialog, QInputDialog

        base_dir = os.path.dirname(path) or os.getcwd()
        default_name = os.path.basename(path.rstrip(os.sep)) or "archive"
        formats = [
            "tar.gz", "tar.xz", "tar.bz2", "tar.zst", "tar", "zip", "7z",
        ]
        fmt, ok = QInputDialog.getItem(
            None, "Create Archive", "Format:", formats, 0, False
        )
        if not ok:
            return
        suggested = os.path.join(base_dir, f"{default_name}.{fmt}")
        archive_path, _ = QFileDialog.getSaveFileName(
            None, "Save Archive As", suggested
        )
        if not archive_path:
            return
        source = os.path.basename(path.rstrip(os.sep))
        argv = create_argv(archive_path, [source])
        if argv is None:
            self._warn(f"Unsupported archive format: {archive_path}")
            return
        self._run(f"Create {os.path.basename(archive_path)}", argv, cwd=base_dir)

    # ------------------------------------------------------------- plumbing
    def _run(self, title: str, argv: list[str], cwd: str) -> None:
        from qfileman.plugins.builtin._runner import (
            missing_tools,
            run_command_dialog,
        )

        missing = missing_tools([argv[0]])
        if missing:
            self._warn(f"Required tool not found on PATH: {missing[0]}")
            return
        run_command_dialog(title, argv, cwd=cwd)

    @staticmethod
    def _warn(message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        log.warning(message)
        QMessageBox.warning(None, "Archive", message)
