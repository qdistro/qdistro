from __future__ import annotations

from pathlib import Path

import pytest
from qnotebook.index import Index, rewrite_wikilinks
from qnotebook.notebook import (
    Notebook,
    PageRef,
    page_to_dirpath,
    page_to_relpath,
    relpath_to_page,
)


def test_page_to_relpath_roundtrip():
    for page in ("Foo", "Foo:Bar", "Foo:Bar:Baz"):
        rel = page_to_relpath(page)
        assert relpath_to_page(rel) == page


def test_page_to_relpath_rejects_empty():
    with pytest.raises(ValueError):
        page_to_relpath("")


def test_page_to_relpath_rejects_slash():
    with pytest.raises(ValueError):
        page_to_relpath("Foo/Bar")


@pytest.mark.parametrize("page", ["..", "Foo:..", ".", "Foo:."])
def test_page_helpers_reject_dot_components(page):
    with pytest.raises(ValueError):
        page_to_relpath(page)
    with pytest.raises(ValueError):
        page_to_dirpath(page)


def test_page_to_dirpath_rejects_separator_components():
    with pytest.raises(ValueError):
        page_to_dirpath("Foo/Bar")
    with pytest.raises(ValueError):
        page_to_dirpath("Foo\\Bar")


def test_create_and_get(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Foo", "# Foo\n")
    assert nb.exists("Foo")
    assert nb.get_page("Foo") == "# Foo\n"


def test_nested_page_creates_dirs(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("A:B:C", "hi\n")
    assert (nb.root / "A" / "B" / "C.md").is_file()


def test_save_adds_trailing_newline(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.save_page("X", "hello")
    assert nb.get_page("X") == "hello\n"


def test_save_empty_is_empty(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.save_page("X", "")
    assert nb.get_page("X") == ""


def test_duplicate_create_raises(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Foo")
    with pytest.raises(FileExistsError):
        nb.create_page("Foo")


def test_pages_iterator(tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    paths = sorted(p.path for p in nb.pages())
    assert "Home" in paths
    assert "Sub" in paths
    assert "Sub:Child" in paths
    assert "Other" in paths


def test_children_root(tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    children = [p.path for p in nb.children(None)]
    assert "Home" in children
    assert "Sub" in children
    assert "Other" in children


def test_children_sub(tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    children = [p.path for p in nb.children("Sub")]
    assert children == ["Sub:Child"]


def test_rename_page(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Foo", "content\n")
    nb.rename_page("Foo", "Bar")
    assert not nb.exists("Foo")
    assert nb.exists("Bar")
    assert nb.get_page("Bar") == "content\n"


def test_rename_moves_child_dir(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Foo", "x\n")
    nb.create_page("Foo:Child", "y\n")
    nb.rename_page("Foo", "Bar")
    assert nb.exists("Bar")
    assert nb.exists("Bar:Child")


def test_rename_conflict_raises(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("A")
    nb.create_page("B")
    with pytest.raises(FileExistsError):
        nb.rename_page("A", "B")


def test_delete_page(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("X", "hi\n")
    nb.delete_page("X")
    assert not nb.exists("X")


def test_page_ref_parent():
    assert PageRef("A:B:C").parent == PageRef("A:B")
    assert PageRef("A").parent is None


def test_page_ref_name():
    assert PageRef("A:B:C").name == "C"


def test_special_chars_in_name(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Hello World", "hi\n")
    assert nb.exists("Hello World")


def test_dotdir_created(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    assert (nb.root / ".qnotebook").is_dir()


# ---- rename with link rewrite ----


def _nb_with_index(tmp_path: Path) -> tuple[Notebook, Index]:
    nb = Notebook(tmp_path / "nb")
    idx = Index(nb)
    return nb, idx


def test_rename_with_no_inbound_links(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Foo", "# Foo\n")
    nb.create_page("Bar", "# Bar\n\nno link.\n")
    idx.rebuild()
    modified = idx.rename_page_and_rewrite("Foo", "Renamed")
    assert modified == []
    assert nb.exists("Renamed")
    assert not nb.exists("Foo")
    idx.close()


def test_rename_with_one_inbound_link(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Foo", "# Foo\n")
    nb.create_page("Bar", "See [[Foo]] here.\n")
    idx.rebuild()
    modified = idx.rename_page_and_rewrite("Foo", "Renamed")
    assert "Bar" in modified
    assert nb.get_page("Bar") == "See [[Renamed]] here.\n"
    assert idx.backlinks("Renamed") == ["Bar"]
    idx.close()


def test_rename_with_many_inbound_links(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Target", "x\n")
    nb.create_page("A", "[[Target]]\n")
    nb.create_page("B", "Link to [[Target]] again.\n")
    nb.create_page("C", "Two refs [[Target]] and [[Target]] here.\n")
    idx.rebuild()
    modified = idx.rename_page_and_rewrite("Target", "New")
    assert set(modified) == {"A", "B", "C"}
    assert "[[New]]" in nb.get_page("A")
    assert nb.get_page("C").count("[[New]]") == 2
    assert "[[Target]]" not in nb.get_page("C")
    idx.close()


def test_rename_preserves_aliases(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Foo", "x\n")
    nb.create_page("A", "See [[Foo|the foo]] here.\n")
    idx.rebuild()
    idx.rename_page_and_rewrite("Foo", "Renamed")
    assert "[[Renamed|the foo]]" in nb.get_page("A")
    idx.close()


def test_rename_doesnt_touch_lookalike_text(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("old", "x\n")
    nb.create_page("other", "x\n")
    nb.create_page("A", "links to [[old]] but also [[old-thing]] and [[older]] and [[other]].\n")
    nb.create_page("old-thing", "x\n")
    nb.create_page("older", "x\n")
    idx.rebuild()
    idx.rename_page_and_rewrite("old", "new")
    body = nb.get_page("A")
    assert "[[new]]" in body
    assert "[[old-thing]]" in body
    assert "[[older]]" in body
    assert "[[other]]" in body
    idx.close()


def test_rename_page_with_children(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Parent", "# Parent\n")
    nb.create_page("Parent:Child", "# Child\n\nlink back to [[Parent]].\n")
    nb.create_page("Other", "See [[Parent]] and [[Parent:Child]].\n")
    idx.rebuild()
    idx.rename_page_and_rewrite("Parent", "Renamed")
    assert nb.exists("Renamed")
    assert nb.exists("Renamed:Child")
    assert not nb.exists("Parent")
    assert "[[Renamed]]" in nb.get_page("Other")
    # Child's inbound link to old "Parent" gets rewritten too.
    assert "[[Renamed]]" in nb.get_page("Renamed:Child")
    idx.close()


def test_rename_slash_form_link_rewritten(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Foo:Bar", "x\n")
    nb.create_page("A", "See [[Foo/Bar]] here.\n")
    idx.rebuild()
    idx.rename_page_and_rewrite("Foo:Bar", "Foo:Renamed")
    assert "[[Foo:Renamed]]" in nb.get_page("A")
    idx.close()


def test_delete_page_removes_file(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Foo", "hi\n")
    nb.delete_page("Foo")
    assert not nb.exists("Foo")


def test_delete_page_removes_empty_child_dir(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    # Create a page and a grandchild, then delete the grandchild.
    nb.create_page("Parent:Child", "y\n")
    nb.create_page("Parent:Child:Grand", "z\n")
    nb.delete_page("Parent:Child:Grand")
    # Parent/Child/ directory (held Grand.md) should be gone now since empty.
    assert not (nb.root / "Parent" / "Child").is_dir()
    assert nb.exists("Parent:Child")


def test_delete_page_keeps_nonempty_child_dir(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Parent", "x\n")
    nb.create_page("Parent:Child", "y\n")
    nb.delete_page("Parent")
    # Parent.md removed, but Parent/ dir stays because Child.md is there.
    assert not nb.exists("Parent")
    assert nb.exists("Parent:Child")


def test_move_page(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Foo", "body\n")
    nb.create_page("Target", "x\n")
    nb.move_page("Foo", "Target:Foo")
    assert not nb.exists("Foo")
    assert nb.exists("Target:Foo")
    assert nb.get_page("Target:Foo") == "body\n"


def test_copy_page(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("Foo", "original\n")
    nb.copy_page("Foo", "Bar")
    assert nb.exists("Foo")
    assert nb.exists("Bar")
    assert nb.get_page("Bar") == "original\n"


def test_copy_page_conflict(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    nb.create_page("A", "a\n")
    nb.create_page("B", "b\n")
    with pytest.raises(FileExistsError):
        nb.copy_page("A", "B")


def test_copy_page_missing_source(tmp_path: Path):
    nb = Notebook(tmp_path / "nb")
    with pytest.raises(FileNotFoundError):
        nb.copy_page("Missing", "Dst")


def test_index_delete_page_returns_inbound_count(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Target", "hi\n")
    nb.create_page("A", "See [[Target]].\n")
    idx.rebuild()
    count = idx.delete_page_and_cleanup("Target")
    assert count == 1
    assert not nb.exists("Target")
    idx.close()


def test_index_copy_page_indexes_new(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Foo", "See [[Bar]].\n")
    nb.create_page("Bar", "x\n")
    idx.rebuild()
    idx.copy_page("Foo", "Foo2")
    assert "Foo2" in idx.all_pages()
    assert "Foo2" in idx.backlinks("Bar") or idx.forward_links("Foo2") == ["Bar"]
    idx.close()


def test_index_move_page_rewrites_links(tmp_path: Path):
    nb, idx = _nb_with_index(tmp_path)
    nb.create_page("Foo", "x\n")
    nb.create_page("Parent", "y\n")
    nb.create_page("A", "See [[Foo]].\n")
    idx.rebuild()
    idx.move_page_and_rewrite("Foo", "Parent:Foo")
    assert nb.exists("Parent:Foo")
    assert "[[Parent:Foo]]" in nb.get_page("A")
    idx.close()


def test_rewrite_wikilinks_skips_fenced_code(tmp_path: Path):
    text = "Before [[Foo]]\n```\nsome [[Foo]] in code\n```\nAfter [[Foo]]\n"
    out = rewrite_wikilinks(text, "Foo", "Bar")
    assert out == "Before [[Bar]]\n```\nsome [[Foo]] in code\n```\nAfter [[Bar]]\n"
