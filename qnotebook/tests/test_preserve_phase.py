"""Preserve-phase tests — plugin metadata survives save when the serializer
would otherwise canonicalize it, as long as the user didn't edit that region.
"""

from __future__ import annotations

from pathlib import Path

from qnotebook.safe_save import LoadResult, SafeWriter, sha256_bytes


def _load(original: bytes, baseline: bytes) -> LoadResult:
    return LoadResult(original=original, baseline=baseline,
                      hash_original=sha256_bytes(original))


def test_unchanged_line_restored_from_original(tmp_path: Path) -> None:
    """User edits line 2; line 1 had plugin metadata lost by serializer.
    After save, line 1 is restored from original bytes."""
    p = tmp_path / "n.md"
    original = b"rating:: 9\nMy notes line\n"
    baseline = b"rating: 9\nMy notes line\n"   # simulated canonicalization
    p.write_bytes(original)
    lr = _load(original, baseline)
    # Editor produced baseline-style for line 1, modified line 2.
    E = b"rating: 9\nMy EDITED notes line\n"
    r = SafeWriter.save(p, E, lr, root=tmp_path)
    assert r.ok
    final = p.read_bytes()
    assert b"rating:: 9" in final  # original form restored
    assert b"EDITED" in final      # user edit preserved


def test_edited_line_keeps_editor_version(tmp_path: Path) -> None:
    """User explicitly edited the plugin-bearing line — their edit wins."""
    p = tmp_path / "n.md"
    original = b"rating:: 9\nbody\n"
    baseline = b"rating: 9\nbody\n"
    p.write_bytes(original)
    lr = _load(original, baseline)
    # User changed rating line to something different
    E = b"rating: 10\nbody\n"
    r = SafeWriter.save(p, E, lr, root=tmp_path)
    assert r.ok
    final = p.read_bytes()
    assert b"10" in final


def test_preserve_phase_noop_when_baseline_equals_original(tmp_path: Path) -> None:
    p = tmp_path / "n.md"
    data = b"hello\n"
    p.write_bytes(data)
    lr = _load(data, data)
    r = SafeWriter.save(p, b"hello world\n", lr, root=tmp_path)
    assert r.ok
    assert p.read_bytes() == b"hello world\n"
