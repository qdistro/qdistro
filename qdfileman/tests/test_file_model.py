"""Tests for file model."""

import os
import time
from pathlib import Path

import pytest
from qfileman.file_model import FileItem, FileModel, is_safe_rename_name


def test_file_item_name(tmp_dir):
    """Test FileItem name property."""
    item = FileItem(tmp_dir / "file1.txt")
    assert item.name == "file1.txt"


def test_file_item_is_dir(tmp_dir):
    """Test FileItem is_dir property."""
    dir_item = FileItem(tmp_dir / "subdir")
    file_item = FileItem(tmp_dir / "file1.txt")
    assert dir_item.is_dir is True
    assert file_item.is_dir is False


def test_file_item_is_file(tmp_dir):
    """Test FileItem is_file property."""
    dir_item = FileItem(tmp_dir / "subdir")
    file_item = FileItem(tmp_dir / "file1.txt")
    assert dir_item.is_file is False
    assert file_item.is_file is True


def test_file_item_size(tmp_dir):
    """Test FileItem size returns the exact byte count."""
    # The conftest fixture writes "content1" (8 bytes) to file1.txt
    item = FileItem(tmp_dir / "file1.txt")
    assert item.size == 8


def test_file_item_size_dir_is_zero(tmp_dir):
    """Directory FileItem.size should be 0 regardless of contents."""
    item = FileItem(tmp_dir / "subdir")
    assert item.size == 0


def test_file_item_size_missing_path_is_zero(tmp_path):
    """Missing path returns 0 rather than raising."""
    item = FileItem(tmp_path / "does_not_exist.txt")
    assert item.size == 0


def test_file_item_extension(tmp_dir):
    """Test FileItem extension property."""
    assert FileItem(tmp_dir / "file1.txt").extension == ".txt"
    assert FileItem(tmp_dir / "file3.md").extension == ".md"
    assert FileItem(tmp_dir / "subdir").extension == ""


def test_file_item_modified(tmp_dir):
    """Test FileItem modified matches the file's mtime."""
    path = tmp_dir / "file1.txt"
    expected_ts = path.stat().st_mtime
    item = FileItem(path)
    # FileItem.modified is a datetime; compare via timestamp for stability.
    assert item.modified is not None
    assert abs(item.modified.timestamp() - expected_ts) < 1.0


def test_file_item_modified_missing_path_is_none(tmp_path):
    item = FileItem(tmp_path / "does_not_exist.txt")
    assert item.modified is None


def test_file_model_init():
    """Test FileModel initialization."""
    model = FileModel()
    assert model.current_path == Path(os.path.expanduser("~"))


def test_file_model_set_path(tmp_dir):
    """Test setting current path."""
    model = FileModel()
    result = model.set_path(str(tmp_dir))
    assert result is True
    assert model.current_path == tmp_dir


def test_file_model_set_invalid_path():
    """Test setting invalid path returns False and leaves path unchanged."""
    model = FileModel()
    original = model.current_path
    result = model.set_path("/nonexistent/path/xyz123")
    assert result is False
    assert model.current_path == original


def test_file_model_set_path_invalidates_cache(tmp_dir, tmp_path):
    """Switching directories must invalidate the file cache."""
    model = FileModel(str(tmp_dir))
    first_listing = [f.name for f in model.get_files()]
    assert "file1.txt" in first_listing

    other = tmp_path / "other_dir"
    other.mkdir()
    (other / "only_file.txt").write_text("x")
    assert model.set_path(str(other)) is True

    second_listing = [f.name for f in model.get_files()]
    assert second_listing == ["only_file.txt"], (
        f"Expected cache invalidation on set_path; got {second_listing}"
    )


def test_file_model_go_up(nested_tmp_dir):
    """go_up moves to the literal parent path."""
    child = nested_tmp_dir / "child_dir"
    model = FileModel(str(child))
    assert model.go_up() is True
    assert model.current_path == nested_tmp_dir


def test_file_model_go_up_at_root_returns_false():
    """Going up from the filesystem root should return False."""
    model = FileModel("/")
    assert model.go_up() is False
    assert model.current_path == Path("/")


def test_file_model_go_home():
    """Test going to home directory."""
    model = FileModel("/")
    model.go_home()
    assert model.current_path == Path(os.path.expanduser("~"))


def test_file_model_refresh(tmp_dir):
    """Refresh should produce the expected names (4 visible, hidden filtered)."""
    model = FileModel(str(tmp_dir))
    model.refresh()
    names = sorted(f.name for f in model.get_files())
    assert names == ["file1.txt", "file2.txt", "file3.md", "subdir"]


def test_file_model_get_files_caches(tmp_dir):
    """get_files returns the same list on repeated calls without an explicit refresh."""
    model = FileModel(str(tmp_dir))
    first = model.get_files()
    second = model.get_files()
    assert first is second, "get_files should cache; it should not rebuild on every call"


def test_file_model_show_hidden(tmp_dir):
    """Test showing hidden files."""
    model = FileModel(str(tmp_dir))

    model.set_show_hidden(False)
    names = [f.name for f in model.get_files()]
    assert ".hidden" not in names

    model.set_show_hidden(True)
    names = [f.name for f in model.get_files()]
    assert ".hidden" in names


def test_file_model_sort_by_name(tmp_dir):
    """Test sorting by name."""
    model = FileModel(str(tmp_dir))
    model.set_sort("name")
    names = [f.name for f in model.get_files()]
    assert names == sorted(names, key=str.lower)


def test_file_model_sort_by_size(tmp_path):
    """Sort by size returns smallest first."""
    test_dir = tmp_path / "sort"
    test_dir.mkdir()
    (test_dir / "small.txt").write_text("x")              # 1 byte
    (test_dir / "medium.txt").write_text("x" * 100)       # 100 bytes
    (test_dir / "large.txt").write_text("x" * 10000)      # 10000 bytes

    model = FileModel(str(test_dir))
    model.set_sort("size")
    sizes = [f.size for f in model.get_files()]
    assert sizes == sorted(sizes), f"Sizes should be ascending, got {sizes}"
    assert [f.name for f in model.get_files()] == [
        "small.txt", "medium.txt", "large.txt",
    ]


def test_file_model_sort_by_date(tmp_path):
    """Sort by date returns oldest first when sort_order='asc'."""
    test_dir = tmp_path / "datesort"
    test_dir.mkdir()
    old = test_dir / "old.txt"
    new = test_dir / "new.txt"
    old.write_text("old")
    new.write_text("new")

    base = time.time()
    os.utime(old, (base - 1000, base - 1000))
    os.utime(new, (base, base))

    model = FileModel(str(test_dir))
    model.set_sort("date")
    names = [f.name for f in model.get_files()]
    # Default sort_order is "asc"; date ascending = oldest first.
    assert names == ["old.txt", "new.txt"]


def test_file_model_sort_desc(tmp_path):
    """Reverse order returns largest first when sorting by size."""
    test_dir = tmp_path / "descsort"
    test_dir.mkdir()
    (test_dir / "a.txt").write_text("x")
    (test_dir / "b.txt").write_text("x" * 100)
    (test_dir / "c.txt").write_text("x" * 10000)

    model = FileModel(str(test_dir))
    model.set_sort("size", sort_order="desc")
    names = [f.name for f in model.get_files()]
    assert names == ["c.txt", "b.txt", "a.txt"]


def test_file_model_create_directory(tmp_dir):
    """Test creating a directory."""
    model = FileModel(str(tmp_dir))
    assert model.create_directory("new_test_dir") is True
    assert (tmp_dir / "new_test_dir").is_dir()
    # Should appear in the refreshed listing.
    assert "new_test_dir" in [f.name for f in model.get_files()]


def test_file_model_create_directory_collision_logs_warning(tmp_dir, caplog):
    """Creating an existing directory returns False and logs."""
    model = FileModel(str(tmp_dir))
    # subdir already exists in tmp_dir fixture
    with caplog.at_level("WARNING", logger="qfileman.file_model"):
        assert model.create_directory("subdir") is False
    assert any("create_directory" in r.message for r in caplog.records)


def test_file_model_delete_file(tmp_dir):
    """Test deleting a file."""
    test_file = tmp_dir / "to_delete.txt"
    test_file.write_text("delete me")

    model = FileModel(str(tmp_dir))
    assert model.delete(str(test_file)) is True
    assert not test_file.exists()


@pytest.mark.cheat_aware(
    protects="FileModel.delete recursively removes a directory and the "
    "directory is actually gone afterward",
    severity="critical",
    cheats=[
        "drop the `not test_dir.exists()` post-condition assertion",
        "weaken to only assert the return value without checking the fs",
    ],
    consequence="a 'successful' delete that leaves data behind (or the "
    "inverse: a delete path that silently fails) ships unnoticed",
)
def test_file_model_delete_directory(tmp_dir):
    """Test deleting a directory."""
    test_dir = tmp_dir / "to_delete_dir"
    test_dir.mkdir()

    model = FileModel(str(tmp_dir))
    assert model.delete(str(test_dir)) is True
    assert not test_dir.exists()


def test_file_model_delete_missing_logs_warning(tmp_dir, caplog):
    """Deleting something that doesn't exist returns False and logs."""
    model = FileModel(str(tmp_dir))
    with caplog.at_level("WARNING", logger="qfileman.file_model"):
        assert model.delete(str(tmp_dir / "no_such_file")) is False
    assert any("delete" in r.message for r in caplog.records)


def test_file_model_rename(tmp_dir):
    """Test renaming a file."""
    test_file = tmp_dir / "old_name.txt"
    test_file.write_text("rename me")

    model = FileModel(str(tmp_dir))
    assert model.rename(str(test_file), "new_name.txt") is True
    assert (tmp_dir / "new_name.txt").exists()
    assert not (tmp_dir / "old_name.txt").exists()


def test_file_model_rename_missing_logs_warning(tmp_dir, caplog):
    """Renaming a path that doesn't exist returns False and logs."""
    model = FileModel(str(tmp_dir))
    with caplog.at_level("WARNING", logger="qfileman.file_model"):
        assert model.rename(str(tmp_dir / "no_such"), "whatever.txt") is False
    assert any("rename" in r.message for r in caplog.records)


@pytest.mark.parametrize("bad_name", ["../escape.txt", "sub/name.txt", "/tmp/escape.txt", ".", ".."])
def test_file_model_rename_rejects_non_basename(tmp_dir, bad_name):
    src = tmp_dir / "old_name.txt"
    src.write_text("rename me")
    model = FileModel(str(tmp_dir))
    assert model.rename(str(src), bad_name) is False
    assert src.exists()


def test_is_safe_rename_name_rejects_nul_and_accepts_basename():
    assert is_safe_rename_name("new_name.txt") is True
    assert is_safe_rename_name("bad\x00name") is False


def test_file_model_view_state_properties_default(tmp_dir):
    """The public state properties expose initial defaults."""
    model = FileModel(str(tmp_dir))
    assert model.show_hidden is False
    assert model.sort_by == "name"
    assert model.sort_order == "asc"


def test_file_model_view_state_properties_reflect_setters(tmp_dir):
    model = FileModel(str(tmp_dir))
    model.set_show_hidden(True)
    model.set_sort("size", sort_order="desc")
    assert model.show_hidden is True
    assert model.sort_by == "size"
    assert model.sort_order == "desc"


def test_file_model_apply_state_batches_refresh(tmp_path):
    """apply_state mutates state then refreshes exactly once."""
    test_dir = tmp_path / "apply"
    test_dir.mkdir()
    (test_dir / "z.txt").write_text("x")
    (test_dir / ".hidden").write_text("h")

    model = FileModel(str(test_dir))
    # Track how often refresh() runs by patching.
    calls = []
    original_refresh = model.refresh

    def counting_refresh():
        calls.append(1)
        original_refresh()

    model.refresh = counting_refresh
    model.apply_state(show_hidden=True, sort_by="size", sort_order="desc")
    assert len(calls) == 1, f"Expected one refresh; got {len(calls)}"
    # And the state should be applied.
    assert model.show_hidden is True
    assert model.sort_by == "size"
    assert model.sort_order == "desc"
    names = [f.name for f in model.get_files()]
    assert ".hidden" in names


def test_file_model_apply_state_partial_keeps_other_fields(tmp_dir):
    """Only the specified arguments mutate; others stay as-is."""
    model = FileModel(str(tmp_dir))
    model.set_show_hidden(True)
    model.set_sort("date", sort_order="desc")
    model.apply_state(sort_by="size")  # only sort_by changes
    assert model.show_hidden is True
    assert model.sort_by == "size"
    assert model.sort_order == "desc"


def test_file_model_add_filter_rejects_plain_function(tmp_dir):
    """add_filter must reject callables that don't expose filter_files()."""
    model = FileModel(str(tmp_dir))
    with pytest.raises(TypeError, match="filter_files"):
        model.add_filter(lambda paths: paths)


def test_file_model_add_filter_accepts_object_with_filter_files(tmp_dir):
    """A FileFilter-shaped object should be accepted and applied."""
    class OnlyTxt:
        def filter_files(self, paths):
            return [p for p in paths if p.endswith(".txt")]

    model = FileModel(str(tmp_dir))
    model.add_filter(OnlyTxt())
    names = [f.name for f in model.get_files()]
    # .md and the subdir should both be filtered out.
    assert all(n.endswith(".txt") for n in names), names
    assert "file1.txt" in names
    assert "file3.md" not in names
    assert "subdir" not in names


def test_file_model_clear_filters_restores_full_listing(tmp_dir):
    """clear_filters should remove previously-installed filters."""
    class DropAll:
        def filter_files(self, paths):
            return []

    model = FileModel(str(tmp_dir))
    model.add_filter(DropAll())
    assert model.get_files() == []
    model.clear_filters()
    assert len(model.get_files()) > 0


def test_file_model_unreadable_directory_logs_warning(tmp_path, caplog):
    """Listing a directory we cannot read should log and return []."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("nope")
    locked.chmod(0o000)
    try:
        model = FileModel(str(locked))
        with caplog.at_level("WARNING", logger="qfileman.file_model"):
            files = model.get_files()
        assert files == []
        # Root cannot be locked out, so skip the log assertion when running as root.
        if os.geteuid() != 0:
            assert any(
                "permission" in r.message.lower() or "error listing" in r.message.lower()
                for r in caplog.records
            )
    finally:
        locked.chmod(0o700)


# --- destination clobber guards (rename/copy/move) ---------------------------


def test_file_model_rename_refuses_existing_sibling(tmp_dir):
    """rename must not silently destroy an existing destination file."""
    src = tmp_dir / "a.txt"
    src.write_text("source")
    victim = tmp_dir / "b.txt"
    victim.write_text("victim")

    model = FileModel(str(tmp_dir))
    assert model.rename(str(src), "b.txt") is False
    # Both files survive untouched.
    assert src.read_text() == "source"
    assert victim.read_text() == "victim"


def test_file_model_rename_overwrite_opt_in(tmp_dir):
    """rename(overwrite=True) replaces the destination."""
    src = tmp_dir / "a.txt"
    src.write_text("source")
    victim = tmp_dir / "b.txt"
    victim.write_text("victim")

    model = FileModel(str(tmp_dir))
    assert model.rename(str(src), "b.txt", overwrite=True) is True
    assert not src.exists()
    assert (tmp_dir / "b.txt").read_text() == "source"


def test_file_model_rename_case_only_self_is_allowed(tmp_dir):
    """Renaming a file to a name that resolves to itself isn't a clobber."""
    src = tmp_dir / "keep.txt"
    src.write_text("data")
    model = FileModel(str(tmp_dir))
    # Renaming to the identical name is a no-op that must succeed (samefile).
    assert model.rename(str(src), "keep.txt") is True
    assert src.read_text() == "data"


def test_file_model_copy_refuses_existing_file(tmp_dir):
    src = tmp_dir / "a.txt"
    src.write_text("source")
    victim = tmp_dir / "dst.txt"
    victim.write_text("victim")

    model = FileModel(str(tmp_dir))
    assert model.copy(str(src), str(victim)) is False
    assert victim.read_text() == "victim"


def test_file_model_copy_into_existing_dir_refuses_same_name(tmp_dir):
    """Copying into a directory that already holds the same basename clobbers
    dst/basename, not the directory — guard the real target."""
    src = tmp_dir / "a.txt"
    src.write_text("source")
    dst_dir = tmp_dir / "into"
    dst_dir.mkdir()
    (dst_dir / "a.txt").write_text("victim")

    model = FileModel(str(tmp_dir))
    assert model.copy(str(src), str(dst_dir)) is False
    assert (dst_dir / "a.txt").read_text() == "victim"


def test_file_model_copy_into_dir_new_name_succeeds(tmp_dir):
    src = tmp_dir / "a.txt"
    src.write_text("source")
    dst_dir = tmp_dir / "into"
    dst_dir.mkdir()

    model = FileModel(str(tmp_dir))
    assert model.copy(str(src), str(dst_dir)) is True
    assert (dst_dir / "a.txt").read_text() == "source"


def test_file_model_copy_overwrite_opt_in(tmp_dir):
    src = tmp_dir / "a.txt"
    src.write_text("source")
    victim = tmp_dir / "dst.txt"
    victim.write_text("victim")

    model = FileModel(str(tmp_dir))
    assert model.copy(str(src), str(victim), overwrite=True) is True
    assert victim.read_text() == "source"


def test_file_model_move_refuses_existing_file(tmp_dir):
    src = tmp_dir / "a.txt"
    src.write_text("source")
    victim = tmp_dir / "dst.txt"
    victim.write_text("victim")

    model = FileModel(str(tmp_dir))
    assert model.move(str(src), str(victim)) is False
    assert src.read_text() == "source"
    assert victim.read_text() == "victim"


def test_file_model_move_overwrite_opt_in(tmp_dir):
    src = tmp_dir / "a.txt"
    src.write_text("source")
    victim = tmp_dir / "dst.txt"
    victim.write_text("victim")

    model = FileModel(str(tmp_dir))
    assert model.move(str(src), str(victim), overwrite=True) is True
    assert not src.exists()
    assert victim.read_text() == "source"


def test_file_model_copy_dir_overwrite_merges(tmp_dir):
    """copytree must accept an existing dir only on explicit overwrite."""
    src = tmp_dir / "srcdir"
    src.mkdir()
    (src / "f.txt").write_text("new")
    dst = tmp_dir / "dstdir"
    dst.mkdir()
    (dst / "old.txt").write_text("kept")

    model = FileModel(str(tmp_dir))
    # Without overwrite the existing dir is protected.
    assert model.copy(str(src), str(dst)) is False
    # With overwrite, contents merge in.
    assert model.copy(str(src), str(dst), overwrite=True) is True
    assert (dst / "f.txt").read_text() == "new"
    assert (dst / "old.txt").read_text() == "kept"


def test_would_clobber_helpers(tmp_dir):
    from qfileman.file_model import effective_copy_target, would_clobber

    a = tmp_dir / "a.txt"
    a.write_text("x")
    b = tmp_dir / "b.txt"
    assert would_clobber(str(a), str(b)) is False  # b missing
    b.write_text("y")
    assert would_clobber(str(a), str(b)) is True
    # same file is not a clobber
    assert would_clobber(str(a), str(a)) is False
    # effective target resolves into-directory drops
    d = tmp_dir / "dir"
    d.mkdir()
    assert effective_copy_target(str(a), str(d)) == d / "a.txt"
    assert effective_copy_target(str(a), str(b)) == b


# --- broken-symlink destinations & atomic rename -----------------------------


def test_file_model_move_refuses_broken_symlink_dest(tmp_dir):
    """A dangling symlink occupies its name; move must not silently replace
    it (Path.exists() is False for a broken link, so the naive check missed)."""
    src = tmp_dir / "a.txt"
    src.write_text("source")
    link = tmp_dir / "dangling"
    link.symlink_to(tmp_dir / "no_such_target")
    assert not link.exists() and link.is_symlink()

    model = FileModel(str(tmp_dir))
    assert model.move(str(src), str(link)) is False
    assert link.is_symlink()
    assert src.read_text() == "source"


def test_file_model_copy_refuses_broken_symlink_dest(tmp_dir):
    """copy2 would write *through* a dangling symlink, creating its target;
    the guard must treat the symlink name as occupied."""
    src = tmp_dir / "a.txt"
    src.write_text("source")
    link = tmp_dir / "dangling"
    target = tmp_dir / "no_such_target"
    link.symlink_to(target)

    model = FileModel(str(tmp_dir))
    assert model.copy(str(src), str(link)) is False
    assert not target.exists()  # nothing was written through the link
    assert link.is_symlink()


def test_rename_exclusive_does_not_clobber(tmp_dir):
    import pytest as _pytest
    from qfileman.file_model import rename_exclusive

    src = tmp_dir / "a.txt"
    src.write_text("source")
    victim = tmp_dir / "b.txt"
    victim.write_text("victim")
    with _pytest.raises(FileExistsError):
        rename_exclusive(src, victim)
    assert victim.read_text() == "victim"
    assert src.read_text() == "source"
    # overwrite=True proceeds.
    rename_exclusive(src, victim, overwrite=True)
    assert victim.read_text() == "source"
    assert not src.exists()


def test_rename_exclusive_self_is_noop(tmp_dir):
    from qfileman.file_model import rename_exclusive
    src = tmp_dir / "a.txt"
    src.write_text("source")
    rename_exclusive(src, src)  # must not raise
    assert src.read_text() == "source"


def test_renameat2_fallback_path_still_guards(tmp_dir, monkeypatch):
    """Force the non-syscall fallback and confirm it still refuses a clobber."""
    import qfileman.file_model as fm
    monkeypatch.setattr(fm, "_renameat2_noreplace", lambda old, new: False)
    src = tmp_dir / "a.txt"
    src.write_text("source")
    victim = tmp_dir / "b.txt"
    victim.write_text("victim")
    import pytest as _pytest
    with _pytest.raises(FileExistsError):
        fm.rename_exclusive(src, victim)
    assert victim.read_text() == "victim"


def test_copy_move_conflict_rsync_dir_detects_child_clash(tmp_dir):
    """rsync of a dir merges contents into dest; a child collision must be
    detected even though dest/srcdir itself does not exist."""
    from qfileman.file_model import copy_move_conflict

    srcdir = tmp_dir / "srcdir"
    srcdir.mkdir()
    (srcdir / "shared.txt").write_text("new")
    dest = tmp_dir / "dest"
    dest.mkdir()
    (dest / "shared.txt").write_text("victim")

    # rsync semantics: contents merge into dest -> shared.txt collides.
    assert copy_move_conflict(str(srcdir), str(dest), rsync=True) == "shared.txt"
    # shutil semantics: whole tree lands at dest/srcdir (which is absent).
    assert copy_move_conflict(str(srcdir), str(dest), rsync=False) is None


def test_effective_target_symlink_to_dir(tmp_dir):
    """A symlink pointing at a directory is treated as a directory by
    shutil.copy2/move (they follow os.path.isdir), so the source lands at
    ``link/src.name`` — not overwriting the link. A symlink-to-file stays a
    leaf (overwrite). This mirrors what shutil actually does on disk."""
    from qfileman.file_model import copy_move_conflict, effective_copy_target

    src = tmp_dir / "a.txt"
    src.write_text("x")

    realdir = tmp_dir / "realdir"
    realdir.mkdir()
    dirlink = tmp_dir / "dirlink"
    dirlink.symlink_to(realdir)

    # copy/move of src into the symlinked dir lands at dirlink/a.txt.
    assert effective_copy_target(str(src), str(dirlink)) == dirlink / "a.txt"
    # No child yet -> no clobber; once dirlink/a.txt exists, it is the clash.
    assert copy_move_conflict(str(src), str(dirlink)) is None
    (realdir / "a.txt").write_text("victim")
    assert copy_move_conflict(str(src), str(dirlink)) == "a.txt"

    # Regression: a symlink to a *file* is a leaf -> resolves to the link
    # itself (overwrite), never appending src.name.
    realfile = tmp_dir / "realfile"
    realfile.write_text("y")
    filelink = tmp_dir / "filelink"
    filelink.symlink_to(realfile)
    assert effective_copy_target(str(src), str(filelink)) == filelink
    assert copy_move_conflict(str(src), str(filelink)) == "filelink"


def test_copy_move_conflict_file_into_dir(tmp_dir):
    from qfileman.file_model import copy_move_conflict
    src = tmp_dir / "a.txt"
    src.write_text("x")
    dest = tmp_dir / "into"
    dest.mkdir()
    assert copy_move_conflict(str(src), str(dest)) is None
    (dest / "a.txt").write_text("victim")
    assert copy_move_conflict(str(src), str(dest)) == "a.txt"
