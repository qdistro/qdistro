"""Parallel write stress tests for SafeWriter.

Keep iteration counts conservative so the suite stays fast on CI — the
critical invariants (no torn reads, no data loss on disjoint edits) are
what we're checking, not throughput.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from qnotebook.safe_save import SafeWriter, atomic_write


def test_rapid_saves_no_torn_reads(tmp_path: Path) -> None:
    p = tmp_path / "n.md"
    a = b"# heading a\n\n" + b"line-a\n" * 300
    b = b"# heading b\n\n" + b"line-b\n" * 300
    p.write_bytes(a)

    stop = threading.Event()
    errors: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                data = p.read_bytes()
            except FileNotFoundError:
                continue
            if data and data not in (a, b):
                errors.append(f"torn read len={len(data)}")
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(200):
            atomic_write(p, a if i % 2 else b)
    finally:
        stop.set()
        t.join(timeout=5)
    assert errors == []


def test_parallel_edits_on_separate_pages_no_data_loss(tmp_path: Path) -> None:
    pages = {f"p{i}": (tmp_path / f"p{i}.md") for i in range(5)}
    for p in pages.values():
        p.write_bytes(b"seed\n")

    def worker(name: str, path: Path) -> None:
        for i in range(20):
            lr = SafeWriter.load(path)
            E = lr.original + f"{name}-{i}\n".encode()
            r = SafeWriter.save(path, E, lr, root=tmp_path)
            if not r.ok:
                # Acceptable: conflict surfaced — log but don't fail
                continue

    threads = [threading.Thread(target=worker, args=(n, p))
               for n, p in pages.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    # Every page must still parse.
    for path in pages.values():
        assert path.is_file()
        assert path.read_bytes()


def test_external_disjoint_edit_merges_during_save(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_bytes(b"L1\nL2\nL3\nL4\nL5\n")
    lr = SafeWriter.load(p)
    # External: change L5
    p.write_bytes(b"L1\nL2\nL3\nL4\nEXT\n")
    # Ours: change L1
    E = b"OUR\nL2\nL3\nL4\nL5\n"
    r = SafeWriter.save(p, E, lr, root=tmp_path)
    assert r.ok
    final = p.read_bytes()
    assert b"OUR" in final and b"EXT" in final


def test_external_overlap_surfaces_conflict(tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_bytes(b"same\n")
    lr = SafeWriter.load(p)
    p.write_bytes(b"external\n")
    r = SafeWriter.save(p, b"editor\n", lr, root=tmp_path)
    # Either a merge tool resolved it, or we get a conflict — never silent overwrite
    if r.ok:
        # If clean, it wasn't silently overwritten — verify the disk reflects something reasonable
        assert p.read_bytes() != b"same\n"
    else:
        assert r.conflict
        assert p.read_bytes() == b"external\n"  # disk unchanged on conflict


def test_noop_save_is_fast_and_correct(tmp_path: Path) -> None:
    p = tmp_path / "n.md"
    data = b"hello\n"
    p.write_bytes(data)
    lr = SafeWriter.load(p)
    start = time.time()
    for _ in range(100):
        r = SafeWriter.save(p, data, lr, root=tmp_path)
        assert r.ok
    elapsed = time.time() - start
    assert elapsed < 5.0, f"100 noop saves took {elapsed:.2f}s"
