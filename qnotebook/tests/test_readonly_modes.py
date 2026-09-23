"""Readonly / warn classification for special files."""

from __future__ import annotations

from pathlib import Path

from qnotebook.special_files import LARGE_FILE_BYTES, classify


def test_excalidraw_filename_readonly(tmp_path: Path) -> None:
    p = tmp_path / "drawing.excalidraw.md"
    p.write_text("# drawing")
    mode = classify(p)
    assert mode.readonly
    assert "Excalidraw" in mode.reason


def test_excalidraw_frontmatter_readonly(tmp_path: Path) -> None:
    p = tmp_path / "thing.md"
    p.write_text("---\nexcalidraw-plugin: parsed\n---\n\n# body\n")
    mode = classify(p)
    assert mode.readonly
    assert "Excalidraw" in mode.reason


def test_kanban_frontmatter_editable_but_warns(tmp_path: Path) -> None:
    p = tmp_path / "board.md"
    p.write_text("---\nkanban-plugin: basic\n---\n\n# board\n")
    mode = classify(p)
    assert not mode.readonly
    assert mode.warn
    assert "Kanban" in mode.reason


def test_large_file_readonly_warn(tmp_path: Path) -> None:
    p = tmp_path / "big.md"
    p.write_bytes(b"x" * (LARGE_FILE_BYTES + 1))
    mode = classify(p)
    assert mode.readonly
    assert mode.warn


def test_normal_file_editable(tmp_path: Path) -> None:
    p = tmp_path / "normal.md"
    p.write_text("# hi\n")
    mode = classify(p)
    assert not mode.readonly
    assert not mode.warn
