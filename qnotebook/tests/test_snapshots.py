"""Snapshot store tests."""

from __future__ import annotations

from pathlib import Path

from qnotebook import snapshots
from qnotebook.notebook import Notebook


def test_save_creates_snapshot(tmp_path: Path) -> None:
    nb = Notebook(tmp_path)
    nb.save_page("Foo", "first\n")
    # Second save should archive the first version.
    nb.save_page("Foo", "second\n")
    snaps = nb.snapshots("Foo")
    assert len(snaps) == 1
    assert snaps[0].read_bytes() == b"first\n"


def test_fifo_rotation_keeps_last_10(tmp_path: Path) -> None:
    nb = Notebook(tmp_path)
    nb.save_page("Foo", "v0\n")
    for i in range(15):
        # Each save snapshots the pre-existing version.
        nb.save_page("Foo", f"v{i+1}\n")
    snaps = nb.snapshots("Foo")
    assert len(snaps) == snapshots.KEEP


def test_restore_writes_snapshot_back(tmp_path: Path) -> None:
    nb = Notebook(tmp_path)
    nb.save_page("Foo", "A\n")
    nb.save_page("Foo", "B\n")
    snaps = nb.snapshots("Foo")
    assert snaps
    nb.restore_snapshot(snaps[0])
    assert nb.get_page("Foo") == "A\n"
    # The restore itself took a pre-restore snapshot.
    snaps_after = nb.snapshots("Foo")
    assert len(snaps_after) >= len(snaps)


def test_no_snapshot_for_brand_new_page(tmp_path: Path) -> None:
    nb = Notebook(tmp_path)
    nb.save_page("NewPage", "hello\n")
    # First save: nothing existed on disk, so no snapshot taken.
    assert nb.snapshots("NewPage") == []


def test_snapshot_iso_timestamp_format(tmp_path: Path) -> None:
    nb = Notebook(tmp_path)
    nb.save_page("F", "x\n")
    nb.save_page("F", "y\n")
    snaps = nb.snapshots("F")
    assert len(snaps[0].iso) == 19
    assert snaps[0].iso[4] == "-"
