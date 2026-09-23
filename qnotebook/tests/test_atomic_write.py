"""Atomic write tests — SafeWriter.atomic_write + Notebook.save_page."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from qnotebook.notebook import Notebook
from qnotebook.safe_save import atomic_write, detect_sync_conflict_siblings


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    atomic_write(p, b"hello\n")
    assert p.read_bytes() == b"hello\n"


def test_atomic_write_replaces_existing(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_bytes(b"old\n")
    atomic_write(p, b"new\n")
    assert p.read_bytes() == b"new\n"


def test_tempfile_cleaned_up_on_success(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    atomic_write(p, b"hi\n")
    leftovers = list(tmp_path.glob(".qnotebook-tmp-*"))
    assert leftovers == []


def test_atomic_write_same_dir_tempfile_stays_local(tmp_path: Path) -> None:
    # Replacing across filesystems is not atomic; SafeWriter must use a
    # sibling tempfile. We verify by monkey-patching tempfile to assert dir.
    import tempfile as _tf

    calls: list[str] = []
    real_mkstemp = _tf.mkstemp

    def capture(**kw):
        calls.append(kw.get("dir", ""))
        return real_mkstemp(**kw)

    _tf.mkstemp = capture
    try:
        p = tmp_path / "sub" / "x.md"
        atomic_write(p, b"y")
    finally:
        _tf.mkstemp = real_mkstemp
    assert calls and Path(calls[0]) == (tmp_path / "sub")


@pytest.mark.cheat_aware(
    protects="a reader (e.g. Syncthing, another editor) never observes a "
    "partially-written / truncated note while we save",
    severity="critical",
    cheats=[
        "drop the `data not in (content_a, content_b)` torn-read check",
        "shrink the loop count or content size so a torn window never opens",
        "swallow the torn read into a skip instead of appending to errors",
    ],
    consequence="a concurrent reader copies a half-written note; the user's "
    "content is silently corrupted or lost via sync",
)
def test_reader_never_sees_truncated_content(tmp_path: Path) -> None:
    """Continuously read while writing — every full read must be valid."""
    p = tmp_path / "note.md"
    # Make content big enough to matter but still fast.
    content_a = b"# heading a\n\n" + b"line-a\n" * 500
    content_b = b"# heading b\n\n" + b"line-b\n" * 500
    p.write_bytes(content_a)

    stop = threading.Event()
    errors: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                data = p.read_bytes()
            except FileNotFoundError:
                continue
            # Must be one or the other — never a truncation
            if data and data not in (content_a, content_b):
                errors.append(f"torn read, len={len(data)}")
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(100):
            atomic_write(p, content_a if i % 2 == 0 else content_b)
    finally:
        time.sleep(0.01)
        stop.set()
        t.join(timeout=2)
    assert errors == []


def test_notebook_save_page_routes_through_safe_save(tmp_path: Path) -> None:
    nb = Notebook(tmp_path)
    nb.save_page("Foo", "hello\n")
    assert (tmp_path / "Foo.md").read_bytes() == b"hello\n"


def test_detect_sync_conflict_siblings(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_bytes(b"hi")
    # Simulate Syncthing conflict file
    (tmp_path / "note.sync-conflict-20260415-100000-ABCDEF.md").write_bytes(b"other")
    hits = detect_sync_conflict_siblings(p)
    assert len(hits) == 1
    assert "sync-conflict" in hits[0].name
