"""Tests for FileSearch."""

from __future__ import annotations

from pathlib import Path

from qfileman.search import FileSearch


def test_by_name_default_glob_yields_visible_files(tmp_tree: Path) -> None:
    """``*`` finds every visible file across the tree."""
    names = {p.name for p in FileSearch(tmp_tree).by_name("*")}
    assert "file1.txt" in names
    assert "file2.py" in names
    assert "nested.txt" in names


def test_by_name_pattern_filters_to_extension(tmp_tree: Path) -> None:
    results = list(FileSearch(tmp_tree).by_name("*.py"))
    assert [p.name for p in results] == ["file2.py"]


def test_by_name_hides_dotfiles_by_default(tmp_tree: Path) -> None:
    names = {p.name for p in FileSearch(tmp_tree).by_name("*")}
    assert ".hidden" not in names
    assert "inside.txt" not in names, (
        "files inside hidden directories must also be skipped when hidden=False"
    )


def test_by_name_includes_dotfiles_when_hidden_true(tmp_tree: Path) -> None:
    names = {p.name for p in FileSearch(tmp_tree).by_name("*", hidden=True)}
    assert ".hidden" in names
    assert "inside.txt" in names


def test_by_name_respects_max_depth(tmp_tree: Path) -> None:
    """max_depth=0 yields only files directly under the root."""
    names = {p.name for p in FileSearch(tmp_tree).by_name("*", max_depth=0)}
    assert "file1.txt" in names
    assert "nested.txt" not in names


def test_by_content_finds_substring(tmp_tree: Path) -> None:
    results = list(FileSearch(tmp_tree).by_content("hello"))
    assert len(results) == 1
    fp, line_no, line_text = results[0]
    assert fp.name == "file1.txt"
    assert line_no == 1
    assert "hello" in line_text


def test_by_content_no_match_yields_nothing(tmp_tree: Path) -> None:
    assert list(FileSearch(tmp_tree).by_content("zzzznotfound")) == []


def test_by_content_case_insensitive_default(tmp_tree: Path) -> None:
    assert len(list(FileSearch(tmp_tree).by_content("HELLO"))) == 1


def test_by_content_case_sensitive_misses_wrong_case(tmp_tree: Path) -> None:
    assert (
        list(FileSearch(tmp_tree).by_content("HELLO", case_sensitive=True)) == []
    )


def test_by_content_pattern_restricts_files_scanned(tmp_tree: Path) -> None:
    """``*.txt`` should skip file2.py even if it contains the query."""
    results = list(FileSearch(tmp_tree).by_content("hi", pattern="*.txt"))
    assert results == [], (
        "file2.py contains 'hi' but pattern excludes .py files"
    )


def test_by_content_descends_into_subdirs(tmp_tree: Path) -> None:
    results = list(FileSearch(tmp_tree).by_content("nested"))
    assert [r[0].name for r in results] == ["nested.txt"]


def test_by_content_logs_warning_on_unreadable_file(tmp_path, caplog):
    """Files we can't read should be skipped with a logged warning."""
    import os

    root = tmp_path / "tree"
    root.mkdir()
    locked = root / "locked.txt"
    locked.write_text("hello", encoding="utf-8")
    locked.chmod(0o000)
    try:
        with caplog.at_level("WARNING", logger="qfileman.search"):
            results = list(FileSearch(root).by_content("hello"))
        # Root can't be locked out, so skip the assertion when running as root.
        if os.geteuid() != 0:
            assert results == []
            assert any("could not read" in r.message for r in caplog.records)
    finally:
        locked.chmod(0o600)
