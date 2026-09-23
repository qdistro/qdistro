"""Tests for the background-worker plumbing and the three call sites that
used to block the Qt main thread (search, folder-size, checksum).

These assert two things the GUI-thread fix has to get right:

* the *pure* helpers honour a cooperative-cancel token, so a worker thread
  can abandon a deep walk / huge hash promptly; and
* the :class:`~qfileman.worker.Worker` lifecycle delivers result/error/
  cancel correctly and tears its thread down without leaking or aborting.
"""

from __future__ import annotations

import hashlib
import time

import pytest
from qfileman.plugins.builtin import checksum as checksum_mod
from qfileman.plugins.builtin import folder_size as fs_mod
from qfileman.search import FileSearch
from qfileman.worker import (
    Cancellation,
    Cancelled,
    Worker,
    run_in_thread,
)

# --------------------------------------------------------------------------
# Cancellation token
# --------------------------------------------------------------------------

def test_cancellation_starts_unset():
    c = Cancellation()
    assert c.is_set() is False
    c.raise_if_cancelled()  # no-op


def test_cancellation_sets_and_raises():
    c = Cancellation()
    c.cancel()
    assert c.is_set() is True
    with pytest.raises(Cancelled):
        c.raise_if_cancelled()


# --------------------------------------------------------------------------
# Pure helpers honour is_cancelled
# --------------------------------------------------------------------------

def test_search_by_name_stops_when_cancelled(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("x")
    c = Cancellation()
    c.cancel()  # cancel before we even start
    results = list(FileSearch(tmp_path).by_name("*.txt", is_cancelled=c.is_set))
    assert results == []


def test_search_by_content_stops_when_cancelled(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("needle\n")
    c = Cancellation()
    c.cancel()
    rows = list(
        FileSearch(tmp_path).by_content("needle", is_cancelled=c.is_set)
    )
    assert rows == []


def test_search_without_token_unchanged(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    names = sorted(p.name for p in FileSearch(tmp_path).by_name("*.txt"))
    assert names == ["a.txt", "b.txt"]


def test_by_content_streams_and_cancels_within_a_large_file(tmp_path):
    """Cancellation is honoured mid-file, not just between files."""
    # One big file with the needle on an early line and again much later.
    big = tmp_path / "big.txt"
    lines = ["needle\n"] + ["filler\n"] * 5000 + ["needle\n"]
    big.write_text("".join(lines), encoding="utf-8")

    # Cancel as soon as the first match is seen: the second (far later) match
    # must never be yielded because the per-line poll stops the stream.
    c = Cancellation()
    seen = []
    for hit in FileSearch(tmp_path).by_content("needle", is_cancelled=c.is_set):
        seen.append(hit)
        c.cancel()  # request cancel right after the first hit
    # At most the first match; the streaming poll aborts before line ~5002.
    assert len(seen) == 1
    assert seen[0][1] == 1  # line number of the first match


def test_by_content_line_numbers_and_text(tmp_path):
    """Streaming read preserves line numbers and strips the newline."""
    f = tmp_path / "f.txt"
    f.write_text("alpha\nbeta needle\ngamma\n", encoding="utf-8")
    hits = list(FileSearch(tmp_path).by_content("needle"))
    assert len(hits) == 1
    _fp, line_no, text = hits[0]
    assert line_no == 2
    assert text == "beta needle"  # no trailing newline


def test_directory_size_cancel_returns_partial(tmp_path):
    # Pre-cancel: the walk breaks before counting anything.
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 100)
    c = Cancellation()
    c.cancel()
    assert fs_mod.directory_size(str(tmp_path), is_cancelled=c.is_set) == 0
    # Without the token the full size is still reported.
    assert fs_mod.directory_size(str(tmp_path)) == 200


def test_child_sizes_reports_progress_and_cancels(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 10)
    (tmp_path / "b.bin").write_bytes(b"y" * 10)
    seen: list[str] = []
    rows = fs_mod.child_sizes(str(tmp_path), on_progress=seen.append)
    assert {n for n, _s, _d in rows} == {"a.bin", "b.bin"}
    assert set(seen) == {"a.bin", "b.bin"}

    # Cancel before the first child -> empty result.
    c = Cancellation()
    c.cancel()
    assert fs_mod.child_sizes(str(tmp_path), is_cancelled=c.is_set) == []


def test_hash_file_progress_and_cancel(tmp_path):
    payload = b"abc" * 500_000  # > 1 MiB so there are multiple chunks
    f = tmp_path / "data.bin"
    f.write_bytes(payload)

    progress: list[tuple[int, int]] = []
    digest = checksum_mod.hash_file(
        str(f), "sha256", on_progress=lambda d, t: progress.append((d, t))
    )
    assert digest == hashlib.sha256(payload).hexdigest()
    assert progress, "expected at least one progress callback"
    # Final progress reaches the full size.
    assert progress[-1][0] == len(payload)
    assert progress[-1][1] == len(payload)

    # Cancel mid-stream: raises Cancelled rather than returning a partial.
    c = Cancellation()
    c.cancel()
    with pytest.raises(Cancelled):
        checksum_mod.hash_file(str(f), "sha256", is_cancelled=c.is_set)


# --------------------------------------------------------------------------
# Worker lifecycle. We use qtbot.waitSignal — the canonical pytest-qt way to
# block on a cross-thread queued signal — and always wait for the backing
# QThread to actually finish before the test ends, so a local thread ref can
# never be collected mid-run (which would abort with "QThread: Destroyed
# while thread is still running").
# --------------------------------------------------------------------------

def _flag_on_finished(worker):
    """Connect a 'done' flag to ``worker.finished`` before the run starts.

    We watch the *worker* (not the thread): the thread self-deletes via
    ``deleteLater`` the instant it finishes, so touching the thread object
    afterwards is unsafe. ``worker.finished`` fires just before that and is a
    reliable, stable completion signal to wait on. The worker is likewise
    deleteLater'd, but only after the thread stops — after we've observed the
    flag — so the flag closure never touches a dead object.
    """
    done = {"v": False}
    worker.finished.connect(lambda: done.__setitem__("v", True))
    return done


def test_worker_delivers_result(qtbot):
    out = {}

    def fn(cancel, progress):
        progress(1, 2, "half")
        return 42

    w = Worker(fn)
    w.result.connect(lambda v: out.setdefault("result", v))
    done = _flag_on_finished(w)
    run_in_thread(w)
    qtbot.waitUntil(lambda: done["v"], timeout=5000)
    assert out["result"] == 42


def test_worker_reports_error(qtbot):
    errs = []

    def fn(cancel, progress):
        raise RuntimeError("boom")

    w = Worker(fn)
    w.error.connect(errs.append)
    done = _flag_on_finished(w)
    run_in_thread(w)
    qtbot.waitUntil(lambda: done["v"], timeout=5000)
    assert errs and "boom" in errs[0]


def test_worker_suppresses_result_after_cancel(qtbot):
    """A result produced after a late cancel must not be delivered."""
    got = []
    started = {"v": False}

    def fn(cancel, progress):
        started["v"] = True
        # Spin until cancelled, then return a value that must be dropped.
        while not cancel.is_set():
            time.sleep(0.005)
        return "stale"

    w = Worker(fn)
    w.result.connect(got.append)
    done = _flag_on_finished(w)
    run_in_thread(w)
    qtbot.waitUntil(lambda: started["v"], timeout=5000)
    w.request_cancel()
    qtbot.waitUntil(lambda: done["v"], timeout=5000)
    assert got == []  # result was suppressed


def test_worker_handles_cancelled_exception(qtbot):
    """A callable that raises Cancelled finishes cleanly (no error signal)."""
    errs = []

    def fn(cancel, progress):
        raise Cancelled()

    w = Worker(fn)
    w.error.connect(errs.append)
    done = _flag_on_finished(w)
    run_in_thread(w)
    qtbot.waitUntil(lambda: done["v"], timeout=5000)
    assert errs == []


# --------------------------------------------------------------------------
# ProgressRunner end-to-end: the two plugin call sites compute off-thread and
# deliver the result to a GUI-thread callback.
# --------------------------------------------------------------------------

def test_progress_runner_delivers_folder_size(qtbot):
    import pathlib
    import tempfile

    from qfileman.worker import ProgressRunner

    tmp_path = pathlib.Path(tempfile.mkdtemp())
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "b.bin").write_bytes(b"y" * 50)

    got = {}

    def work(cancel, progress):
        return fs_mod.child_sizes(
            str(tmp_path), is_cancelled=cancel.is_set, on_progress=lambda n: None
        )

    runner = ProgressRunner(
        work, title="t", label="l",
        on_result=lambda children: got.setdefault("children", children),
    )
    runner.start()
    qtbot.waitUntil(lambda: "children" in got, timeout=5000)
    sizes = {n: s for n, s, _d in got["children"]}
    assert sizes == {"a.bin": 100, "b.bin": 50}


def test_progress_runner_delivers_checksum(qtbot):
    import pathlib
    import tempfile

    from qfileman.worker import ProgressRunner

    tmp_path = pathlib.Path(tempfile.mkdtemp())
    payload = b"hello world" * 200_000  # multiple chunks
    f = tmp_path / "data.bin"
    f.write_bytes(payload)

    got = {}

    def work(cancel, progress):
        return checksum_mod.hash_file(
            str(f), "sha256",
            is_cancelled=cancel.is_set,
            on_progress=lambda d, t: progress(d, t, ""),
        )

    runner = ProgressRunner(
        work, title="t", label="l",
        on_result=lambda digest: got.setdefault("digest", digest),
    )
    runner.start()
    qtbot.waitUntil(lambda: "digest" in got, timeout=5000)
    assert got["digest"] == hashlib.sha256(payload).hexdigest()
