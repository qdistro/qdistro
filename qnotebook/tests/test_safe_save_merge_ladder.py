"""Merge-ladder tests for SafeWriter."""

from __future__ import annotations

from pathlib import Path

import pytest
from qnotebook import safe_save
from qnotebook.safe_save import (
    HAS_GIT_MERGE_FILE,
    HAS_MERGIRAF,
    HAS_WIGGLE,
    LoadResult,
    SafeWriter,
    _apply_three_way_disjoint,
    sha256_bytes,
)


def _lr(original: bytes) -> LoadResult:
    return LoadResult(original=original, baseline=original,
                      hash_original=sha256_bytes(original))


def test_trivial_rung_disk_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    original = b"hello\nworld\n"
    p.write_bytes(original)
    lr = SafeWriter.load(p)
    E = b"hello\nworld!\n"
    result = SafeWriter.save(p, E, lr, root=tmp_path)
    assert result.ok
    assert result.rung == "trivial"
    assert p.read_bytes() == E


def test_fast_save_defers_strict_preserve_subprocess(
    tmp_path: Path, monkeypatch,
) -> None:
    """The GUI fast path must stop before strict-preserve's git fallback.

    This is the common autosave exposure: baseline != original can require a
    preserve merge even when the disk has not changed since load.
    """
    p = tmp_path / "note.md"
    original = b"alpha\nbeta\n"
    baseline = b"ALPHA\nBETA\n"
    p.write_bytes(original)
    lr = LoadResult(
        original=original,
        baseline=baseline,
        hash_original=sha256_bytes(original),
    )

    monkeypatch.setattr(safe_save, "HAS_GIT_MERGE_FILE", True)

    def boom(*_args, **_kwargs):
        raise AssertionError("fast save entered the subprocess merge rung")

    monkeypatch.setattr(safe_save, "_git_merge_file", boom)

    result = SafeWriter.save(
        p, b"editor\nreplacement\n", lr, root=tmp_path,
        allow_subprocess=False,
    )
    assert result.needs_merge
    assert result.reason == "strict-preserve"
    assert result.deferred is not None
    assert p.read_bytes() == original


@pytest.mark.cheat_aware(
    protects="when an external process and the editor change disjoint lines, "
    "the 3-way merge keeps BOTH edits — neither side's content is dropped",
    severity="critical",
    cheats=[
        "loosen the final assert to only `result.ok` and drop the "
        "`A_ext`/`E_ours` content checks",
        "widen `result.rung in (...)` to accept a rung that overwrites "
        "instead of merging",
        "make ours == external so there is nothing real to merge",
    ],
    consequence="a concurrent save (another app, Syncthing) silently "
    "overwrites the user's other edit — irreversible note data loss",
)
def test_disjoint_hunks_fast_path(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    original = b"A\nB\nC\nD\nE\n"
    p.write_bytes(original)
    lr = SafeWriter.load(p)
    # External: change line 1 (A -> A_ext)
    p.write_bytes(b"A_ext\nB\nC\nD\nE\n")
    # Ours: change line 5 (E -> E_ours)
    E = b"A\nB\nC\nD\nE_ours\n"
    result = SafeWriter.save(p, E, lr, root=tmp_path)
    assert result.ok
    assert result.rung in ("disjoint-hunks", "git-merge-file")
    merged = p.read_bytes()
    assert b"A_ext" in merged and b"E_ours" in merged


def test_overlap_escalates_beyond_disjoint(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    original = b"A\nB\nC\n"
    p.write_bytes(original)
    lr = SafeWriter.load(p)
    # Both sides edit line B
    p.write_bytes(b"A\nB_ext\nC\n")
    E = b"A\nB_ours\nC\n"
    # Pure-python disjoint should return None
    assert _apply_three_way_disjoint(original, E, b"A\nB_ext\nC\n") is None
    result = SafeWriter.save(p, E, lr, root=tmp_path)
    # Either it's a conflict, or an external tool resolved it
    assert result.status in ("conflict", "ok")


@pytest.mark.skipif(not HAS_GIT_MERGE_FILE, reason="git not available")
def test_git_merge_file_clean_on_disjoint_lines(tmp_path: Path) -> None:
    original = b"1\n2\n3\n4\n5\n"
    E = b"ours-1\n2\n3\n4\n5\n"
    D = b"1\n2\n3\n4\ntheirs-5\n"
    clean, out = safe_save._git_merge_file(original, E, D)
    assert clean
    assert b"ours-1" in out and b"theirs-5" in out


@pytest.mark.skipif(not HAS_GIT_MERGE_FILE, reason="git not available")
def test_git_merge_file_conflict_escalates(tmp_path: Path) -> None:
    original = b"X\n"
    E = b"ours\n"
    D = b"theirs\n"
    clean, out = safe_save._git_merge_file(original, E, D)
    assert not clean
    assert b"<<<<<<<" in out


def test_whitespace_only_prefers_ours(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    original = b"foo\nbar\n"
    p.write_bytes(original)
    lr = SafeWriter.load(p)
    # External rewrites with only whitespace variations (CRLF).
    p.write_bytes(b"foo\r\nbar\r\n")
    E = b"foo\nbar\nbaz\n"
    result = SafeWriter.save(p, E, lr, root=tmp_path)
    assert result.ok
    # Anything non-conflict is acceptable — disjoint, whitespace-ours, or git.


def test_roundtrip_guard_accepts_markdown(tmp_path: Path) -> None:
    assert safe_save._roundtrip_parses(b"# hello\n\n- a\n- b\n")


def test_fallthrough_surfaces_conflict_bytes(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    original = b"same line\n"
    p.write_bytes(original)
    lr = SafeWriter.load(p)
    p.write_bytes(b"external change\n")
    E = b"editor change\n"
    result = SafeWriter.save(p, E, lr, root=tmp_path)
    if result.conflict:
        assert result.base == original
        assert result.ours == E
        assert result.theirs == b"external change\n"


def test_merge_log_records_rung(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_bytes(b"hi\n")
    lr = SafeWriter.load(p)
    SafeWriter.save(p, b"hi\nbye\n", lr, root=tmp_path)
    log = (tmp_path / ".qnotebook" / "merge.log").read_text()
    assert "trivial" in log


@pytest.mark.skipif(not HAS_WIGGLE, reason="wiggle not available")
def test_wiggle_runs(tmp_path: Path) -> None:
    # wiggle smoke test; may or may not resolve cleanly depending on content.
    original = b"a\nb\nc\n"
    E = b"a\nB\nc\n"
    D = b"a\nb\nc_ext\n"
    clean, out = safe_save._wiggle(original, E, D)
    # Just assert it returns something — behavior depends on wiggle version.
    assert isinstance(out, (bytes, bytearray))


@pytest.mark.skipif(not HAS_MERGIRAF, reason="mergiraf not installed")
def test_mergiraf_structural_merge(tmp_path: Path) -> None:
    original = b"- item one\n- item two\n"
    E = b"- item ONE\n- item two\n"
    D = b"- item one\n- item TWO\n"
    clean, out = safe_save._mergiraf(original, E, D, ext=".md")
    assert isinstance(out, (bytes, bytearray))


def test_noop_save_when_disk_matches_editor(tmp_path: Path) -> None:
    p = tmp_path / "n.md"
    original = b"hello\n"
    p.write_bytes(original)
    lr = SafeWriter.load(p)
    # External re-writes the file with identical contents to what we'd save.
    same = b"hello\n"
    p.write_bytes(same)
    # editor also wants "hello\n" — should be ok
    r = SafeWriter.save(p, same, lr, root=tmp_path)
    assert r.ok
