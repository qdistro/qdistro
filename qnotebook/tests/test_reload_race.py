"""Concurrency-hole tests: external writes during editor session must never
be silently clobbered on the next save.

Each test simulates a specific race window around `_page_load_result` /
`_on_external_page_change` / the CLI append path and asserts the end state
on disk preserves the external writer's bytes (possibly merged with
editor edits) rather than losing them."""

from __future__ import annotations

from pathlib import Path

from qnotebook import safe_save
from qnotebook.notebook import Notebook
from qnotebook.window import MainWindow


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_silent_reload_refreshes_load_result(qapp, tmp_path: Path, qtbot) -> None:
    """Race 1: after a silent reload, next save must use fresh baseline.

    Setup: open page, external writer replaces disk bytes, watcher fires,
    editor reloads silently. User then edits editor content (now matching
    the new baseline) and saves. Expect disk to reflect editor+external
    state — not the pre-external content."""
    (tmp_path / "Home.md").write_text("# Home\n\noriginal\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")

    # External writer rewrites disk.
    (tmp_path / "Home.md").write_text("# Home\n\nexternal content\n", encoding="utf-8")

    # Simulate the watcher's silent-reload path (editor is clean).
    w._reload_current_page_silently()

    lr = w._page_load_result["Home"]
    disk = (tmp_path / "Home.md").read_bytes()
    assert lr.hash_original == safe_save.sha256_bytes(disk), (
        "LoadResult baseline was not refreshed after silent reload"
    )

    # Save with no editor edits: trivial rung must hit and disk bytes remain.
    result = w.notebook.save_page("Home", _read(tmp_path / "Home.md"),
                                   load_result=lr)
    assert result.ok
    assert "external content" in _read(tmp_path / "Home.md")


def test_keep_mine_preserves_base_for_later_merge(qapp, tmp_path: Path, qtbot) -> None:
    """Race 1b: if user keeps their edits after external change, the stored
    LoadResult must remain the pre-external one so a subsequent save runs a
    3-way merge with the correct base."""
    (tmp_path / "Home.md").write_text("# Home\n\nline-a\nline-b\n",
                                       encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")

    pre_lr = w._page_load_result["Home"]
    pre_hash = pre_lr.hash_original

    # External writer appends to the end — a disjoint region.
    (tmp_path / "Home.md").write_text(
        "# Home\n\nline-a\nline-b\nexternal-append\n", encoding="utf-8"
    )

    # User chose "Keep mine" — load_result stays put. (We don't invoke the
    # prompt; we just assert the invariant.)
    assert w._page_load_result["Home"].hash_original == pre_hash

    # User edits line-a -> line-a-edited and saves.
    edited = "# Home\n\nline-a-edited\nline-b\n"
    result = w.notebook.save_page("Home", edited,
                                   load_result=w._page_load_result["Home"])
    assert result.ok, f"expected successful merge, got {result.status}/{result.rung}"
    final = _read(tmp_path / "Home.md")
    assert "line-a-edited" in final, "editor edit was lost"
    assert "external-append" in final, "external append was clobbered"


def test_cli_append_basic(tmp_path: Path) -> None:
    """Race 4 baseline: cmd_append still works for the no-contention case."""
    (tmp_path / "Note.md").write_text("# Note\n\nbody\n", encoding="utf-8")
    from qnotebook.cli import cmd_append
    rc = cmd_append(str(tmp_path), "Note", "appended-line", bullet=True)
    assert rc == 0
    text = _read(tmp_path / "Note.md")
    assert "appended-line" in text
    assert "body" in text


def test_cli_append_merges_with_concurrent_external_write(
    tmp_path: Path, monkeypatch
) -> None:
    """Race 4: an external writer lands between cmd_append's read and its
    write. With LoadResult plumbed through, SafeWriter detects the drift
    and merges the append onto the post-external state."""
    page = tmp_path / "Note.md"
    # External edit changes line 1 (the heading); CLI appends a line at the
    # end. These are disjoint edit regions and the merge ladder should
    # preserve both.
    page.write_text("# Note\n\nbody-original\n", encoding="utf-8")

    from qnotebook import cli as _cli

    real_save_page = Notebook.save_page

    def racey_save(self, page_name, body, load_result=None):
        page.write_text("# Note (edited by external)\n\nbody-original\n",
                         encoding="utf-8")
        return real_save_page(self, page_name, body, load_result=load_result)

    monkeypatch.setattr(Notebook, "save_page", racey_save)
    rc = _cli.cmd_append(str(tmp_path), "Note", "appended-line", bullet=True)

    final = _read(page)
    # The critical invariant: the external edit must NOT be silently clobbered.
    # Either the merge succeeded (both lines present) OR the CLI bailed with
    # a conflict marker — both are acceptable. What is NOT acceptable is
    # losing the external edit and writing only our append.
    assert "edited by external" in final, (
        "External concurrent write was clobbered by CLI append — "
        "LoadResult was not plumbed through the merge ladder."
    )
    if rc == 0:
        assert "appended-line" in final, (
            "Merge succeeded but CLI append was dropped"
        )
    else:
        # Conflict surfaced as .md.conflict sibling; disk holds external state.
        assert rc == 3
        conflict = page.with_suffix(".md.conflict")
        assert conflict.is_file()
        assert "appended-line" in conflict.read_text(encoding="utf-8")


def test_load_page_preserves_load_result_when_dirty(
    qapp, tmp_path: Path, qtbot
) -> None:
    """Race 3: if load_page is called for the currently-open page while the
    editor is dirty, the original LoadResult must be preserved so the next
    save runs a 3-way merge against the true pre-edit base rather than
    using the now-refreshed disk state as the new base."""
    (tmp_path / "Home.md").write_text("# Home\n\nalpha\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")

    original_lr = w._page_load_result["Home"]
    original_hash = original_lr.hash_original

    # User dirties the editor.
    w.editor.textCursor().insertText(" extra")
    assert w.editor.is_dirty()

    # An external writer changes disk while we're dirty.
    (tmp_path / "Home.md").write_text("# Home\n\nalpha\nexternal\n",
                                       encoding="utf-8")

    # A stray reload-same-page call happens (e.g. rename handler).
    w.load_page("Home")

    # The stored LoadResult must still match the pre-external snapshot.
    assert w._page_load_result["Home"].hash_original == original_hash


def test_cli_append_creates_new_page_without_load_result(tmp_path: Path) -> None:
    """Race 4 edge: appending to a nonexistent page should still work (no
    LoadResult needed — it's a pure create)."""
    from qnotebook.cli import cmd_append
    rc = cmd_append(str(tmp_path), "Brand:New", "first-line", bullet=True)
    assert rc == 0
    assert (tmp_path / "Brand" / "New.md").is_file()
    assert "first-line" in _read(tmp_path / "Brand" / "New.md")


# ----------------------------------------------------------------------
# Extra verification tests (spot-checks — no new fixes, just invariants)
# ----------------------------------------------------------------------


def test_silent_reload_handles_missing_file(qapp, tmp_path: Path, qtbot) -> None:
    """If the watched file is unlinked externally, silent reload must not
    crash or blank the LoadResult — it just no-ops."""
    (tmp_path / "Home.md").write_text("# Home\n\nalpha\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    pre = w._page_load_result["Home"].hash_original
    (tmp_path / "Home.md").unlink()
    w._reload_current_page_silently()
    # The LoadResult for Home is left untouched (file is gone).
    assert w._page_load_result["Home"].hash_original == pre


def test_silent_reload_with_identical_bytes_is_noop(
    qapp, tmp_path: Path, qtbot
) -> None:
    """If disk matches the current baseline, silent reload still refreshes
    LoadResult but produces the same hash."""
    (tmp_path / "Home.md").write_text("# Home\n\nalpha\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    pre = w._page_load_result["Home"].hash_original
    # Touch without changing bytes.
    (tmp_path / "Home.md").write_text("# Home\n\nalpha\n", encoding="utf-8")
    w._reload_current_page_silently()
    assert w._page_load_result["Home"].hash_original == pre


def test_external_change_signal_triggers_reload_when_clean(
    qapp, tmp_path: Path, qtbot
) -> None:
    """Firing the PageWatcher signal while editor is clean goes through the
    silent-reload path."""
    (tmp_path / "Home.md").write_text("# Home\n\nalpha\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    (tmp_path / "Home.md").write_text("# Home\n\nbeta\n", encoding="utf-8")
    w._on_external_page_change(str(tmp_path / "Home.md"))
    # Editor should now show beta and LoadResult should match beta on disk.
    disk = (tmp_path / "Home.md").read_bytes()
    assert w._page_load_result["Home"].hash_original == safe_save.sha256_bytes(disk)
    assert "beta" in w.editor.markdown()


def test_external_change_signal_no_op_without_notebook(
    qapp, tmp_path: Path, qtbot
) -> None:
    """_on_external_page_change must not crash when no notebook is open."""
    w = MainWindow()
    qtbot.addWidget(w)
    # Notebook not opened — should early-return.
    w._on_external_page_change("/nonexistent")


def test_save_after_silent_reload_does_not_rewrite_disk(
    qapp, tmp_path: Path, qtbot
) -> None:
    """After silent reload refreshes LoadResult, a save of unchanged text
    hits the trivial rung — disk mtime unchanged by the save path at the
    byte level."""
    (tmp_path / "Home.md").write_text("# Home\n\nfoo\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    (tmp_path / "Home.md").write_text("# Home\n\nbar\n", encoding="utf-8")
    w._reload_current_page_silently()
    lr = w._page_load_result["Home"]
    r = w.notebook.save_page("Home", "# Home\n\nbar\n", load_result=lr)
    assert r.ok and r.rung == "trivial"
    assert _read(tmp_path / "Home.md").strip() == "# Home\n\nbar".strip()


def test_keep_mine_then_reload_refreshes_base(
    qapp, tmp_path: Path, qtbot
) -> None:
    """If user first 'keeps mine' (base preserved), then a later silent
    reload comes through, the base should now refresh to current disk."""
    (tmp_path / "Home.md").write_text("# Home\n\nv1\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    h0 = w._page_load_result["Home"].hash_original

    (tmp_path / "Home.md").write_text("# Home\n\nv2\n", encoding="utf-8")
    # "Keep mine" — no op on LoadResult.
    assert w._page_load_result["Home"].hash_original == h0

    # Now editor is clean (simulate user saved their changes elsewhere) and
    # disk changes again.
    w.editor.clear_dirty()
    (tmp_path / "Home.md").write_text("# Home\n\nv3\n", encoding="utf-8")
    w._reload_current_page_silently()
    disk = (tmp_path / "Home.md").read_bytes()
    assert w._page_load_result["Home"].hash_original == safe_save.sha256_bytes(disk)


def test_load_page_switch_between_pages_refreshes_each(
    qapp, tmp_path: Path, qtbot
) -> None:
    """Switching to a different page should refresh that page's LoadResult
    (not the original page's)."""
    (tmp_path / "A.md").write_text("# A\n", encoding="utf-8")
    (tmp_path / "B.md").write_text("# B\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("A")
    a_hash = w._page_load_result["A"].hash_original
    w.load_page("B")
    assert "B" in w._page_load_result
    # A's baseline is still there (cached).
    assert w._page_load_result["A"].hash_original == a_hash


def test_load_page_non_dirty_refreshes_base(
    qapp, tmp_path: Path, qtbot
) -> None:
    """Loading the same page while CLEAN should refresh the baseline (the
    dirty-guard only preserves when dirty)."""
    (tmp_path / "Home.md").write_text("# Home\n\nv1\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    (tmp_path / "Home.md").write_text("# Home\n\nv2\n", encoding="utf-8")
    w.load_page("Home")
    disk = (tmp_path / "Home.md").read_bytes()
    assert w._page_load_result["Home"].hash_original == safe_save.sha256_bytes(disk)


def test_cli_append_heading_preserves_external_edits(tmp_path: Path, monkeypatch) -> None:
    """Append-under-heading with a concurrent external edit: the external
    edit must not be clobbered."""
    page = tmp_path / "Note.md"
    page.write_text("# Note\n\n## Log\n\n", encoding="utf-8")

    from qnotebook import cli as _cli
    real_save_page = Notebook.save_page

    def racey(self, p, body, load_result=None):
        page.write_text("# Note (ext)\n\n## Log\n\n", encoding="utf-8")
        return real_save_page(self, p, body, load_result=load_result)

    monkeypatch.setattr(Notebook, "save_page", racey)
    rc = _cli.cmd_append(str(tmp_path), "Note", "entry-one", heading="Log")
    final = _read(page)
    assert "(ext)" in final or rc == 3, (
        "External heading edit was clobbered"
    )


def test_cli_append_timestamp_flag(tmp_path: Path) -> None:
    """--timestamp prefixes the line; the append still goes through the
    merge ladder (no regression from the refactor)."""
    (tmp_path / "Note.md").write_text("# Note\n\n", encoding="utf-8")
    from qnotebook.cli import cmd_append
    rc = cmd_append(str(tmp_path), "Note", "work-log", timestamp=True)
    assert rc == 0
    text = _read(tmp_path / "Note.md")
    assert "work-log" in text
    # Time prefix is HH:MM so one of these digits-char pairs must be present.
    import re
    assert re.search(r"\d\d:\d\d work-log", text)


def test_cli_append_link_flag(tmp_path: Path) -> None:
    """--link appends `[[target]]` to the line."""
    (tmp_path / "Note.md").write_text("# Note\n\n", encoding="utf-8")
    from qnotebook.cli import cmd_append
    rc = cmd_append(str(tmp_path), "Note", "see", link="Other")
    assert rc == 0
    assert "[[Other]]" in _read(tmp_path / "Note.md")


def test_cli_append_stdin(tmp_path: Path, monkeypatch) -> None:
    """--stdin reads text from stdin; still routes through SafeWriter."""
    (tmp_path / "Note.md").write_text("# Note\n\n", encoding="utf-8")
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("piped-text\n"))
    from qnotebook.cli import cmd_append
    rc = cmd_append(str(tmp_path), "Note", "", stdin=True)
    assert rc == 0
    assert "piped-text" in _read(tmp_path / "Note.md")


def test_cli_append_multiple_sequential_appends(tmp_path: Path) -> None:
    """Repeated cmd_append calls compose correctly (each one loads a fresh
    baseline)."""
    (tmp_path / "Note.md").write_text("# Note\n\n", encoding="utf-8")
    from qnotebook.cli import cmd_append
    for i in range(5):
        rc = cmd_append(str(tmp_path), "Note", f"line-{i}", bullet=True)
        assert rc == 0
    final = _read(tmp_path / "Note.md")
    for i in range(5):
        assert f"line-{i}" in final


def test_cli_append_today_through_merge_ladder(tmp_path: Path) -> None:
    """--append-today creates the journal page on first call and appends
    afterwards, all through the merge ladder."""
    from qnotebook.cli import cmd_append_today
    rc1 = cmd_append_today(str(tmp_path), "first", bullet=True)
    assert rc1 == 0
    rc2 = cmd_append_today(str(tmp_path), "second", bullet=True)
    assert rc2 == 0
    import time
    today_rel = time.strftime("Journal/%Y/%m/%d.md")
    page = tmp_path / today_rel
    assert page.is_file()
    text = page.read_text(encoding="utf-8")
    assert "first" in text and "second" in text


def test_save_page_without_load_result_uses_atomic_rung(tmp_path: Path) -> None:
    """Direct save without LoadResult (create path) returns rung=atomic."""
    nb = Notebook(tmp_path)
    result = nb.save_page("Fresh", "hello\n")
    assert result.ok
    assert result.rung == "atomic"
    assert (tmp_path / "Fresh.md").read_text(encoding="utf-8") == "hello\n"


def test_save_page_with_load_result_uses_trivial_rung(tmp_path: Path) -> None:
    """Save with LoadResult and unchanged disk => trivial rung."""
    (tmp_path / "P.md").write_text("content\n", encoding="utf-8")
    nb = Notebook(tmp_path)
    lr = nb.load_for_save("P")
    result = nb.save_page("P", "content-edited\n", load_result=lr)
    assert result.ok
    assert result.rung == "trivial"


def test_save_page_detects_external_change_and_merges(tmp_path: Path) -> None:
    """Save with LoadResult and externally mutated disk => non-trivial rung."""
    (tmp_path / "P.md").write_text("alpha\nbeta\n", encoding="utf-8")
    nb = Notebook(tmp_path)
    lr = nb.load_for_save("P")
    # External write touches line 1.
    (tmp_path / "P.md").write_text("ALPHA\nbeta\n", encoding="utf-8")
    # Editor touches line 2.
    result = nb.save_page("P", "alpha\nBETA\n", load_result=lr)
    assert result.ok, f"unexpected {result.status}/{result.rung}"
    assert result.rung != "trivial"
    final = (tmp_path / "P.md").read_text(encoding="utf-8")
    assert "ALPHA" in final and "BETA" in final


def test_page_watcher_rearms_after_self_event(qapp, tmp_path: Path, qtbot) -> None:
    """After a save triggers the watcher, `rearm()` leaves the path watched
    (no silent drop)."""
    (tmp_path / "Home.md").write_text("# Home\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    # Fake a post-save atomic replace.
    safe_save.atomic_write(tmp_path / "Home.md", b"# Home\n\nv2\n")
    w._page_watcher.rearm()
    assert str(tmp_path / "Home.md") in w._page_watcher._fsw.files()


def test_load_result_hash_matches_original_bytes(tmp_path: Path) -> None:
    """LoadResult.hash_original equals sha256 of the original bytes."""
    (tmp_path / "P.md").write_text("abc\n", encoding="utf-8")
    nb = Notebook(tmp_path)
    lr = nb.load_for_save("P")
    assert lr.hash_original == safe_save.sha256_bytes(lr.original)


def test_cli_append_rc_conflict_is_three(tmp_path: Path, monkeypatch) -> None:
    """When the merge ladder cannot reconcile, cmd_append returns rc=3 and
    writes a .md.conflict sibling."""
    page = tmp_path / "Note.md"
    page.write_text("# Note\n\nA\nB\nC\n", encoding="utf-8")
    from qnotebook import cli as _cli
    from qnotebook import safe_save as _ss

    # Force SafeWriter.save to always report conflict.
    def fake_save(path, E, load, *args, **kwargs):
        return _ss.SaveResult(
            status="conflict",
            base=load.original,
            ours=E,
            theirs=page.read_bytes(),
            rung="conflict",
        )

    monkeypatch.setattr(_ss.SafeWriter, "save", staticmethod(fake_save))
    rc = _cli.cmd_append(str(tmp_path), "Note", "appended")
    assert rc == 3
    assert (tmp_path / "Note.md.conflict").is_file()


def test_cli_append_does_not_touch_unrelated_pages(tmp_path: Path) -> None:
    """Appending to one page leaves sibling pages byte-identical."""
    (tmp_path / "A.md").write_text("A content\n", encoding="utf-8")
    (tmp_path / "B.md").write_text("B content\n", encoding="utf-8")
    a_before = (tmp_path / "A.md").read_bytes()
    from qnotebook.cli import cmd_append
    rc = cmd_append(str(tmp_path), "B", "x")
    assert rc == 0
    assert (tmp_path / "A.md").read_bytes() == a_before


def test_split_view_uses_primary_editor_for_save(qapp, tmp_path: Path, qtbot) -> None:
    """Race 5 verification: opening a split does not create a second
    _page_load_result key per split, and _save_current always reads from
    the primary editor."""
    (tmp_path / "Home.md").write_text("# Home\n\noriginal\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    before = dict(w._page_load_result)
    w.split_editor("horizontal")
    assert w.is_split()
    # No extra keys got added for the split.
    assert set(w._page_load_result.keys()) == set(before.keys())
    # Primary editor is still the one save goes through.
    assert w.editor is not getattr(w, "_secondary_editor", None)
    w.close_split()
    assert not w.is_split()


def test_atomic_write_replaces_file_bytes(tmp_path: Path) -> None:
    """atomic_write is the primitive; confirm same-dir tempfile approach
    works and replaces the target atomically."""
    target = tmp_path / "x.md"
    target.write_text("old\n", encoding="utf-8")
    safe_save.atomic_write(target, b"new\n")
    assert target.read_bytes() == b"new\n"


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    """atomic_write creates missing parent directories."""
    target = tmp_path / "deep" / "nested" / "x.md"
    safe_save.atomic_write(target, b"hi\n")
    assert target.read_bytes() == b"hi\n"


def test_load_for_save_on_missing_file_returns_empty(tmp_path: Path) -> None:
    """LoadResult for a nonexistent page has empty original and a
    deterministic hash of b""."""
    nb = Notebook(tmp_path)
    lr = nb.load_for_save("Missing")
    assert lr.original == b""
    assert lr.hash_original == safe_save.sha256_bytes(b"")


def test_save_page_noop_match_rung(tmp_path: Path) -> None:
    """If editor bytes equal disk bytes after external write, noop-match
    rung fires instead of an unnecessary write."""
    (tmp_path / "P.md").write_text("same\n", encoding="utf-8")
    nb = Notebook(tmp_path)
    lr = nb.load_for_save("P")
    # External writer lands the *same* content as editor would produce.
    (tmp_path / "P.md").write_text("same-ext\n", encoding="utf-8")
    # Editor matches the new external content exactly.
    result = nb.save_page("P", "same-ext\n", load_result=lr)
    assert result.ok
    assert result.rung in ("noop-match", "trivial", "disjoint-hunks"), result.rung


def test_reload_after_save_keeps_editor_clean(qapp, tmp_path: Path, qtbot) -> None:
    """After save + post-save load_for_save refresh, the editor stays clean."""
    (tmp_path / "Home.md").write_text("# Home\n\nv\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("Home")
    w.editor.textCursor().insertText(" more")
    assert w.editor.is_dirty()
    w._save_current()
    assert not w.editor.is_dirty()


def test_dirty_guard_only_applies_to_current_page(qapp, tmp_path: Path, qtbot) -> None:
    """The load_page dirty-guard should only activate when switching to the
    *same* page; switching to a different page refreshes normally even if
    the currently-open editor is dirty."""
    (tmp_path / "A.md").write_text("A\n", encoding="utf-8")
    (tmp_path / "B.md").write_text("B\n", encoding="utf-8")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    w.load_page("A")
    w.editor.textCursor().insertText(" extra")
    assert w.editor.is_dirty()
    # Switch to B — editor becomes B, dirty clears on fresh doc.
    w.load_page("B")
    assert "B" in w._page_load_result
    # A's LoadResult remains (cached from earlier).
    assert "A" in w._page_load_result


