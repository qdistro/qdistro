"""QAbstractItemModel walking the notebook filesystem lazily."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QAbstractItemModel, QMimeData, QModelIndex, Qt, pyqtSignal

from .notebook import Notebook, PageRef

PAGE_MIME_TYPE = "application/x-qnotebook-page-path"
INVALID_INDEX = QModelIndex()


class _Node:
    __slots__ = ("page", "parent", "children", "loaded")

    def __init__(self, page: PageRef | None, parent: _Node | None) -> None:
        self.page = page  # None for root
        self.parent = parent
        self.children: list[_Node] = []
        self.loaded = False


class PageTreeModel(QAbstractItemModel):
    pageMoved = pyqtSignal(str, str)  # old_path, new_path

    def __init__(self, notebook: Notebook, parent: Any = None) -> None:
        super().__init__(parent)
        self.notebook = notebook
        self._root = _Node(None, None)
        self._index = None  # optional Index for link-rewrite on drop

    # ---- helpers ----

    def _node(self, index: QModelIndex) -> _Node:
        if not index.isValid():
            return self._root
        return index.internalPointer()

    def _load(self, node: _Node) -> None:
        if node.loaded:
            return
        page = node.page.path if node.page else None
        for child in self.notebook.children(page):
            node.children.append(_Node(child, node))
        node.loaded = True

    def page_for_index(self, index: QModelIndex) -> PageRef | None:
        node = self._node(index)
        return node.page

    def index_for_page(self, page: str) -> QModelIndex:
        """Walk the tree to build an index for the given page path."""
        parts = page.split(":")
        parent_idx = QModelIndex()
        cumulative = ""
        for i, part in enumerate(parts):
            cumulative = part if i == 0 else cumulative + ":" + part
            node = self._node(parent_idx)
            self._load(node)
            found = -1
            for row, child in enumerate(node.children):
                if child.page and child.page.path == cumulative:
                    found = row
                    break
            if found < 0:
                return QModelIndex()
            parent_idx = self.index(found, 0, parent_idx)
        return parent_idx

    def refresh(self) -> None:
        self.beginResetModel()
        self._root = _Node(None, None)
        self.endResetModel()

    # ---- QAbstractItemModel ----

    def rowCount(self, parent: QModelIndex = INVALID_INDEX) -> int:
        node = self._node(parent)
        self._load(node)
        return len(node.children)

    def columnCount(self, parent: QModelIndex = INVALID_INDEX) -> int:
        return 1

    def index(self, row: int, column: int, parent: QModelIndex = INVALID_INDEX) -> QModelIndex:
        if column != 0:
            return QModelIndex()
        node = self._node(parent)
        self._load(node)
        if 0 <= row < len(node.children):
            return self.createIndex(row, column, node.children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node: _Node = index.internalPointer()
        p = node.parent
        if p is None or p is self._root:
            return QModelIndex()
        gp = p.parent
        if gp is None:
            return QModelIndex()
        row = gp.children.index(p)
        return self.createIndex(row, 0, p)

    def hasChildren(self, parent: QModelIndex = INVALID_INDEX) -> bool:
        node = self._node(parent)
        if not node.loaded:
            # Fast path: avoid loading full subtree
            if node.page is None:
                return True  # root: let it load on expand
            return self.notebook.dir_for(node.page.path).is_dir()
        return bool(node.children)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        node: _Node = index.internalPointer()
        if node.page is None:
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._display_name(node.page)
        if role == Qt.ItemDataRole.ToolTipRole:
            return node.page.path
        return None

    def _display_name(self, page: PageRef) -> str:
        """Prefer frontmatter title if present; fall back to page basename."""
        try:
            if not self.notebook.exists(page.path):
                return page.name
            text = self.notebook.get_page(page.path)
            if text.startswith("---"):
                from .frontmatter import title_for
                return title_for(page.path, text)
        except Exception:
            pass
        return page.name

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not index.isValid():
            # Root accepts drops.
            return base | Qt.ItemFlag.ItemIsDropEnabled
        return base | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled

    # ---- drag-and-drop ----

    def set_index(self, index) -> None:
        """Attach an Index so that dropMimeData can rewrite wikilinks."""
        self._index = index

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [PAGE_MIME_TYPE]

    def mimeData(self, indexes: list[QModelIndex]) -> QMimeData:
        md = QMimeData()
        paths: list[str] = []
        for idx in indexes:
            if idx.column() != 0:
                continue
            ref = self.page_for_index(idx)
            if ref is not None:
                paths.append(ref.path)
        md.setData(PAGE_MIME_TYPE, "\n".join(paths).encode("utf-8"))
        return md

    def canDropMimeData(
        self, data: QMimeData, action: Qt.DropAction,
        row: int, column: int, parent: QModelIndex,
    ) -> bool:
        if not data.hasFormat(PAGE_MIME_TYPE):
            return False
        return True

    def dropMimeData(
        self, data: QMimeData, action: Qt.DropAction,
        row: int, column: int, parent: QModelIndex,
    ) -> bool:
        if action == Qt.DropAction.IgnoreAction:
            return True
        if not data.hasFormat(PAGE_MIME_TYPE):
            return False
        raw = bytes(data.data(PAGE_MIME_TYPE)).decode("utf-8")
        paths = [p for p in raw.split("\n") if p]
        if not paths:
            return False
        src_path = paths[0]
        parent_ref = self.page_for_index(parent) if parent.isValid() else None
        leaf = src_path.rsplit(":", 1)[-1]
        if parent_ref is None:
            new_path = leaf
        else:
            if parent_ref.path == src_path:
                return False
            # Don't drop into a descendant of self
            if parent_ref.path.startswith(src_path + ":"):
                return False
            new_path = parent_ref.path + ":" + leaf
        if new_path == src_path:
            return False
        if self.notebook.exists(new_path):
            return False
        # Land any queued async versioning commit before the page path moves,
        # so the deferred path-scoped commit lands at the still-current path
        # rather than recording a spurious deletion of the old one.
        try:
            from . import versioning
            versioning.wait_for_pending_commits(-1)
        except Exception:
            pass
        if self._index is not None:
            self._index.move_page_and_rewrite(src_path, new_path)
        else:
            self.notebook.move_page(src_path, new_path)
        self.refresh()
        self.pageMoved.emit(src_path, new_path)
        return True
