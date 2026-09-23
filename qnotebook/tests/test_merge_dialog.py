"""3-pane merge dialog tests — construct, set choices, verify bytes."""

from __future__ import annotations

from qnotebook.merge_dialog import (
    MergeDialog,
    apply_choices,
    compute_hunks,
    conflict_marker_bytes,
)


def test_compute_hunks_overlapping(qtbot) -> None:
    base = b"A\nB\nC\n"
    ours = b"A\nB_OURS\nC\n"
    theirs = b"A\nB_THEIRS\nC\n"
    hunks = compute_hunks(base, ours, theirs)
    assert len(hunks) == 1
    assert hunks[0].o_start == 1 and hunks[0].o_end == 2


def test_apply_choices_ours_wins(qtbot) -> None:
    base = b"A\nB\nC\n"
    ours = b"A\nB_OURS\nC\n"
    theirs = b"A\nB_THEIRS\nC\n"
    hunks = compute_hunks(base, ours, theirs)
    hunks[0].choice = "ours"
    merged = apply_choices(base, ours, theirs, hunks)
    assert merged == b"A\nB_OURS\nC\n"


def test_apply_choices_theirs_wins(qtbot) -> None:
    base = b"A\nB\nC\n"
    ours = b"A\nB_OURS\nC\n"
    theirs = b"A\nB_THEIRS\nC\n"
    hunks = compute_hunks(base, ours, theirs)
    hunks[0].choice = "theirs"
    merged = apply_choices(base, ours, theirs, hunks)
    assert merged == b"A\nB_THEIRS\nC\n"


def test_conflict_marker_bytes_roundtrip(qtbot) -> None:
    out = conflict_marker_bytes(b"B\n", b"O\n", b"T\n")
    assert b"<<<<<<< ours" in out
    assert b"||||||| base" in out
    assert b">>>>>>> theirs" in out


def test_dialog_accepts_merged(qtbot) -> None:
    base = b"A\nB\nC\n"
    ours = b"A\nB_OURS\nC\n"
    theirs = b"A\nB_THEIRS\nC\n"
    dlg = MergeDialog(base, ours, theirs, page_name="Test")
    qtbot.addWidget(dlg)
    dlg.set_hunk_choice(0, "theirs")
    dlg._accept_merged()
    assert dlg.outcome == MergeDialog.RESULT_MERGED
    assert dlg.result_bytes == b"A\nB_THEIRS\nC\n"


def test_dialog_save_as_conflict(qtbot) -> None:
    dlg = MergeDialog(b"B\n", b"O\n", b"T\n")
    qtbot.addWidget(dlg)
    dlg._save_conflict_file()
    assert dlg.outcome == MergeDialog.RESULT_CONFLICT_FILE
    assert b"<<<<<<<" in dlg.result_bytes
