"""Optional per-save git commits in the notebook root."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )


def is_repo(root: Path) -> bool:
    return (root / ".git").exists()


def init_repo(root: Path) -> bool:
    """Initialize a git repo at `root`. Returns True if the repo now exists."""
    if is_repo(root):
        return True
    res = _run(["git", "init", "-q"], root)
    if res.returncode != 0:
        return False
    # Set a local identity so commits succeed in sandboxes without global config.
    _run(["git", "config", "user.email", "qnotebook@local"], root)
    _run(["git", "config", "user.name", "qnotebook"], root)
    return is_repo(root)


def _page_pathspecs(root: Path, page: str) -> list[str] | None:
    """Repo-relative pathspecs owned by ``page``: its ``.md`` file plus the
    sibling ``_resources/`` directory that holds its images/attachments.

    The ``_resources/`` spec is included when that directory exists on disk OR
    is already tracked in git (so a *deletion* of a previously-committed
    resource is still recorded). It is omitted only when the dir is both absent
    and untracked — git aborts the whole ``add``/``commit`` if a pathspec
    matches nothing, which would silently drop the save.

    Returns ``None`` on error so the caller can fall back to ``git add -A``.
    Imported lazily to avoid a circular import with ``notebook``."""
    try:
        from .notebook import page_to_relpath
        rel = page_to_relpath(page)
        specs = [rel.as_posix()]
        # Resources for a page live in `<page-dir>/_resources/` (see
        # MainWindow._resources_dir_for_current).
        resdir_rel = rel.parent / "_resources"
        resdir_posix = resdir_rel.as_posix()
        if (root / resdir_rel).is_dir():
            specs.append(resdir_posix)
        else:
            # Dir gone from disk — include it only if git still tracks content
            # under it, so the deletion gets committed.
            tracked = _run(
                ["git", "ls-files", "--", resdir_posix], root
            )
            if tracked.stdout.strip():
                specs.append(resdir_posix)
        return specs
    except Exception:
        return None


def commit_page(root: Path, page: str, rung: str | None = None) -> bool:
    """Commit the edited page (its ``.md`` plus its ``_resources/``).

    Initializes the repo if missing. Returns True iff a new commit was made.
    ``rung`` is the SafeWriter merge rung — the commit log doubles as a
    merge audit when included.

    Staging only the triggering page's own paths (rather than a blanket
    ``git add -A``) keeps each save its own correctly-attributed commit. This
    matters because commits are dispatched asynchronously (see
    :func:`commit_page_async`): a later worker must not sweep an unrelated
    page's pending changes into *this* page's commit message, nor leave a
    queued sibling save with a clean tree and no commit of its own. The page's
    co-located ``_resources/`` directory is included so an inserted image or
    attachment is versioned together with the markdown that references it."""
    if not is_repo(root):
        if not init_repo(root):
            return False
    specs = _page_pathspecs(root, page)
    if specs:
        # Stage this page's own paths (and deletions thereof, hence -A
        # semantics scoped to these pathspecs).
        _run(["git", "add", "-A", "--", *specs], root)
        status = _run(["git", "status", "--porcelain", "--", *specs], root)
    else:
        # Page name didn't map to a path — fall back to staging everything so
        # the save is never silently dropped from history.
        _run(["git", "add", "-A"], root)
        status = _run(["git", "status", "--porcelain"], root)
    if not status.stdout.strip():
        return False
    msg = f"edit: {page}"
    if rung:
        msg += f" [{rung}]"
    commit_cmd = ["git", "commit", "-q", "-m", msg]
    if specs:
        # Scope the commit to this page's pathspecs too, so any unrelated
        # content that happens to already sit in the index (e.g. left over from
        # an aborted prior commit) is NOT swept in under this page's message.
        commit_cmd += ["--", *specs]
    res = _run(commit_cmd, root)
    return res.returncode == 0


def page_history(root: Path, page_file_rel: str) -> list[tuple[str, str, str]]:
    """Return commit log for the file as list of (sha, iso_date, subject).

    Most recent first. Empty list when no repo or no commits."""
    if not is_repo(root):
        return []
    res = _run(
        [
            "git", "log", "--follow", "--pretty=format:%H%x1f%ad%x1f%s",
            "--date=iso", "--", page_file_rel,
        ],
        root,
    )
    out: list[tuple[str, str, str]] = []
    for line in res.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) >= 3:
            out.append((parts[0], parts[1], parts[2]))
    return out


def page_at_revision(root: Path, sha: str, page_file_rel: str) -> str | None:
    """Return the content of `page_file_rel` at commit `sha`, or None on error."""
    if not is_repo(root):
        return None
    res = _run(["git", "show", f"{sha}:{page_file_rel}"], root)
    if res.returncode != 0:
        return None
    return res.stdout


def commit_count(root: Path) -> int:
    if not is_repo(root):
        return 0
    res = _run(["git", "rev-list", "--count", "HEAD"], root)
    try:
        return int(res.stdout.strip())
    except ValueError:
        return 0


# ------------------------------------------------------------------
# Asynchronous commit dispatch
# ------------------------------------------------------------------
#
# ``commit_page`` shells out to ``git add``/``status``/``commit`` and can take
# seconds on a large notebook repo. Running it inline on the GUI thread (on
# every save, including the unattended autosave timer) freezes the editor
# mid-typing. ``commit_page_async`` hands the work to a single background
# worker thread so the save path returns immediately.
#
# A single-thread pool serializes commits: git refuses concurrent writers on
# the same repo (``index.lock``), and serializing also preserves commit order.


def _commit_pool():
    """Lazily create a serialized (max 1 thread) QThreadPool for commits.

    Returns ``None`` when PyQt6 cannot be imported (e.g. a pure-CLI/headless
    context without Qt) so callers can fall back to running the commit inline.
    QThreadPool itself does not require a running event loop to execute tasks.
    """
    try:
        from PyQt6.QtCore import QThreadPool
    except Exception:
        return None
    pool = getattr(_commit_pool, "_pool", None)
    if pool is None:
        pool = QThreadPool()
        pool.setMaxThreadCount(1)
        # Don't let a slow commit silently expire while queued.
        pool.setExpiryTimeout(-1)
        _commit_pool._pool = pool
    return pool


def commit_page_async(
    root: Path, page: str, rung: str | None = None, *, done=None
) -> bool:
    """Run :func:`commit_page` off the GUI thread.

    The commit is a fire-and-forget side effect — the editor never consumes
    its return value — so dispatching it asynchronously keeps the save path
    responsive. When the optional ``done`` callback is supplied it is invoked
    with the boolean result *from the worker thread*; callers that touch Qt
    widgets must marshal back to the GUI thread themselves.

    Returns ``True`` if the commit was dispatched to a worker thread, ``False``
    if it had to run inline (no Qt event loop available) — in the inline case
    the commit has already completed by the time this returns.
    """
    pool = _commit_pool()
    if pool is None:
        result = commit_page(root, page, rung=rung)
        if done is not None:
            done(result)
        return False

    from PyQt6.QtCore import QRunnable

    class _CommitTask(QRunnable):
        def __init__(self) -> None:
            super().__init__()
            self.setAutoDelete(True)

        def run(self) -> None:
            try:
                result = commit_page(root, page, rung=rung)
            except Exception:
                result = False
            if done is not None:
                done(result)

    pool.start(_CommitTask())
    return True


def wait_for_pending_commits(timeout_ms: int = -1) -> bool:
    """Block until queued async commits finish. Mainly for tests / shutdown.

    Returns ``True`` if the pool drained (or there is no pool), ``False`` on
    timeout.
    """
    pool = getattr(_commit_pool, "_pool", None)
    if pool is None:
        return True
    return pool.waitForDone(timeout_ms)
