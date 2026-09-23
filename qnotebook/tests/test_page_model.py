from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QMimeData, QModelIndex, Qt
from qnotebook.index import Index
from qnotebook.notebook import Notebook
from qnotebook.page_model import PAGE_MIME_TYPE, PageTreeModel


def test_root_row_count(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    n = model.rowCount(QModelIndex())
    # Home, Sub, Other
    assert n == 3


def test_names(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    names = []
    for r in range(model.rowCount(QModelIndex())):
        idx = model.index(r, 0, QModelIndex())
        names.append(model.data(idx, Qt.ItemDataRole.DisplayRole))
    assert sorted(names) == ["Home", "Other", "Sub"]


def test_children_of_sub(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    # Find Sub
    for r in range(model.rowCount(QModelIndex())):
        idx = model.index(r, 0, QModelIndex())
        if model.data(idx) == "Sub":
            assert model.hasChildren(idx)
            child = model.index(0, 0, idx)
            assert model.data(child) == "Child"
            break


def test_index_for_page(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    idx = model.index_for_page("Sub:Child")
    assert idx.isValid()
    assert model.data(idx) == "Child"


def test_page_for_index(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    idx = model.index_for_page("Home")
    ref = model.page_for_index(idx)
    assert ref is not None and ref.path == "Home"


def test_parent(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    idx = model.index_for_page("Sub:Child")
    parent = model.parent(idx)
    assert parent.isValid()
    assert model.data(parent) == "Sub"


def test_refresh(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    _ = model.rowCount(QModelIndex())
    nb.create_page("NewOne", "x\n")
    model.refresh()
    assert model.rowCount(QModelIndex()) == 4


# ---- drag-drop ----


def _mime_for(path: str) -> QMimeData:
    md = QMimeData()
    md.setData(PAGE_MIME_TYPE, path.encode("utf-8"))
    return md


def test_drop_moves_page_under_parent(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    idx = Index(nb)
    idx.rebuild()
    model = PageTreeModel(nb)
    model.set_index(idx)
    parent_idx = model.index_for_page("Sub")
    data = _mime_for("Other")
    ok = model.dropMimeData(
        data, Qt.DropAction.MoveAction, -1, -1, parent_idx
    )
    assert ok
    assert nb.exists("Sub:Other")
    assert not nb.exists("Other")
    idx.close()


def test_drop_drains_pending_commit_before_move(qapp, tmp_notebook: Path):
    """A drag-and-drop page move must drain any queued async versioning commit
    first, so the saved content is committed at the old path (not lost as a
    spurious deletion) before the file moves."""
    import subprocess

    from qnotebook import versioning
    nb = Notebook(tmp_notebook)
    idx = Index(nb)
    idx.rebuild()
    versioning.init_repo(tmp_notebook)
    model = PageTreeModel(nb)
    model.set_index(idx)
    # Edit + queue an async commit for "Other", then immediately drag it.
    (tmp_notebook / "Other.md").write_text("# Other\n\ndnd-race line\n")
    versioning.commit_page_async(tmp_notebook, "Other", rung="trivial")
    parent_idx = model.index_for_page("Sub")
    ok = model.dropMimeData(
        _mime_for("Other"), Qt.DropAction.MoveAction, -1, -1, parent_idx
    )
    assert ok
    # The pre-move content was committed at the old path before the move.
    sha = subprocess.run(
        ["git", "log", "-1", "--pretty=%H", "--", "Other.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout.strip()
    assert sha, "queued commit was lost — drop raced the async commit"
    content = subprocess.run(
        ["git", "show", f"{sha}:Other.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "dnd-race line" in content
    idx.close()


def test_drop_on_root_moves_to_top(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    idx = Index(nb)
    idx.rebuild()
    model = PageTreeModel(nb)
    model.set_index(idx)
    data = _mime_for("Sub:Child")
    ok = model.dropMimeData(
        data, Qt.DropAction.MoveAction, -1, -1, QModelIndex()
    )
    assert ok
    assert nb.exists("Child")
    assert not nb.exists("Sub:Child")
    idx.close()


def test_drop_onto_self_rejected(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    sub_idx = model.index_for_page("Sub")
    data = _mime_for("Sub")
    ok = model.dropMimeData(
        data, Qt.DropAction.MoveAction, -1, -1, sub_idx
    )
    assert not ok
    assert nb.exists("Sub")


def test_drop_into_descendant_rejected(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    model = PageTreeModel(nb)
    child_idx = model.index_for_page("Sub:Child")
    data = _mime_for("Sub")
    ok = model.dropMimeData(
        data, Qt.DropAction.MoveAction, -1, -1, child_idx
    )
    assert not ok


def test_drop_rewrites_inbound_links(qapp, tmp_notebook: Path):
    nb = Notebook(tmp_notebook)
    idx = Index(nb)
    idx.rebuild()
    model = PageTreeModel(nb)
    model.set_index(idx)
    # Home has [[Other]]; move Other under Sub → link should become [[Sub:Other]]
    sub_idx = model.index_for_page("Sub")
    data = _mime_for("Other")
    model.dropMimeData(data, Qt.DropAction.MoveAction, -1, -1, sub_idx)
    assert "[[Sub:Other]]" in nb.get_page("Home")
    idx.close()
