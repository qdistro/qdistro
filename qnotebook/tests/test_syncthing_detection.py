"""Syncthing conflict file detection."""

from __future__ import annotations

from pathlib import Path

from qnotebook.sync_conflict import (
    ConflictWatcher,
    parse_conflict_name,
    scan,
)


def test_parse_conflict_name() -> None:
    p = Path("/nb/note.sync-conflict-20260415-100000-ABCDEF1.md")
    cf = parse_conflict_name(p)
    assert cf is not None
    assert cf.date == "20260415"
    assert cf.time == "100000"
    assert cf.device == "ABCDEF1"
    assert cf.original.name == "note.md"
    assert cf.iso.startswith("2026-04-15 10:00:00")


def test_parse_conflict_name_rejects_plain(tmp_path: Path) -> None:
    assert parse_conflict_name(Path("note.md")) is None
    assert parse_conflict_name(Path("x.sync-conflict-garbage.md")) is None


def test_scan_finds_conflict(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "a.sync-conflict-20260415-100000-AAA1111.md").write_text("y")
    (tmp_path / ".qnotebook").mkdir()
    (tmp_path / ".qnotebook" / "b.sync-conflict-20260415-100000-BBB2222.md").write_text("z")
    hits = scan(tmp_path)
    assert len(hits) == 1
    assert hits[0].path.name.startswith("a.sync-conflict-")


def test_watcher_emits_signal(tmp_path: Path, qtbot) -> None:
    (tmp_path / "note.md").write_text("x")
    (tmp_path / "note.sync-conflict-20260415-100000-XYZ0001.md").write_text("y")
    w = ConflictWatcher()
    seen: list = []
    w.conflictFileFound.connect(seen.append)
    w.set_root(tmp_path)
    assert len(seen) == 1
    # Rescan dedupes
    w.rescan()
    assert len(seen) == 1
