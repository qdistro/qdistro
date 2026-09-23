"""MainWindow: tree + WYSIWYG editor + backlinks dock."""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtCore import (
    QModelIndex,
    QObject,
    QRunnable,
    QSettings,
    Qt,
    QThreadPool,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QKeySequence, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QTreeView,
    QWidget,
)

from .editor import MarkdownEditor
from .notebook import PageRef
from .search import Hit, Search


def _extract_heading_section(md_text: str, heading: str) -> str:
    """Return the body under the first heading whose text matches `heading`,
    stopping at the next same-or-higher-level heading."""
    target = heading.strip().lower()
    lines = md_text.splitlines()
    out: list[str] = []
    start_level: int | None = None
    capturing = False
    for line in lines:
        m_h = None
        stripped = line.lstrip()
        if stripped.startswith("#"):
            # Count leading # up to 6
            k = 0
            while k < len(stripped) and stripped[k] == "#" and k < 6:
                k += 1
            if k > 0 and (k >= len(stripped) or stripped[k] == " "):
                htext = stripped[k + 1:].strip() if k < len(stripped) else ""
                m_h = (k, htext)
        if capturing:
            if m_h is not None and m_h[0] <= (start_level or 99):
                break
            out.append(line)
            continue
        if m_h is not None and m_h[1].lower() == target:
            capturing = True
            start_level = m_h[0]
    return "\n".join(out).strip() + ("\n" if out else "")


def _unique_path(desired: Path) -> Path:
    """Return `desired`, or `desired` with a `-1`, `-2`, ... suffix on collision."""
    if not desired.exists():
        return desired
    stem = desired.stem
    suffix = desired.suffix
    parent = desired.parent
    n = 1
    while True:
        candidate = parent / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


class FindBar(QWidget):
    """Inline find-in-page bar. Hidden by default; Ctrl+F toggles."""

    def __init__(self, editor: MarkdownEditor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = editor
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)

        self.input = QLineEdit(self)
        self.input.setPlaceholderText("Find")
        self.input.textChanged.connect(self._on_text_changed)
        self.input.returnPressed.connect(self.find_next)

        self.btn_prev = QPushButton("Prev", self)
        self.btn_prev.clicked.connect(self.find_prev)
        self.btn_next = QPushButton("Next", self)
        self.btn_next.clicked.connect(self.find_next)

        self.case_checkbox = QCheckBox("Aa", self)
        self.case_checkbox.setToolTip("Case sensitive")
        self.case_checkbox.stateChanged.connect(lambda _: self._recount())

        self.count_label = QLabel("", self)

        self.btn_close = QPushButton("x", self)
        self.btn_close.setFixedWidth(24)
        self.btn_close.clicked.connect(self.close_bar)

        layout.addWidget(self.input, 1)
        layout.addWidget(self.btn_prev)
        layout.addWidget(self.btn_next)
        layout.addWidget(self.case_checkbox)
        layout.addWidget(self.count_label)
        layout.addWidget(self.btn_close)

        self.hide()

    # ---- public API ----

    def open_bar(self) -> None:
        self.show()
        cur = self.editor.textCursor()
        if cur.hasSelection():
            self.input.setText(cur.selectedText())
        self.input.setFocus()
        self.input.selectAll()
        self._recount()

    def close_bar(self) -> None:
        self.hide()
        self.editor.setFocus()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        super().keyPressEvent(e)

    # ---- search ----

    def _flags(self, backward: bool = False) -> QTextDocument.FindFlag:
        f = QTextDocument.FindFlag(0)
        if backward:
            f |= QTextDocument.FindFlag.FindBackward
        if self.case_checkbox.isChecked():
            f |= QTextDocument.FindFlag.FindCaseSensitively
        return f

    def find_next(self) -> bool:
        return self._find(backward=False)

    def find_prev(self) -> bool:
        return self._find(backward=True)

    def _find(self, backward: bool) -> bool:
        needle = self.input.text()
        if not needle:
            return False
        found = self.editor.find(needle, self._flags(backward))
        if not found:
            # Wrap around
            cur = self.editor.textCursor()
            wrap = QTextCursor(self.editor.document())
            if backward:
                wrap.movePosition(QTextCursor.MoveOperation.End)
            # Start: default cursor position is 0
            self.editor.setTextCursor(wrap)
            found = self.editor.find(needle, self._flags(backward))
            if not found:
                self.editor.setTextCursor(cur)
        self._update_count_label()
        return found

    def _on_text_changed(self, _text: str) -> None:
        self._recount()
        # Auto-advance to next match on typing
        needle = self.input.text()
        if not needle:
            return
        cur = self.editor.textCursor()
        cur.setPosition(cur.selectionStart())
        self.editor.setTextCursor(cur)
        self.find_next()

    def _recount(self) -> None:
        self._update_count_label()

    def _match_count(self) -> int:
        needle = self.input.text()
        if not needle:
            return 0
        doc = self.editor.document()
        flags = self._flags(False)
        count = 0
        cur = QTextCursor(doc)
        while True:
            cur = doc.find(needle, cur, flags)
            if cur.isNull():
                break
            count += 1
        return count

    def _update_count_label(self) -> None:
        needle = self.input.text()
        if not needle:
            self.count_label.setText("")
            return
        n = self._match_count()
        self.count_label.setText(f"{n} match" + ("" if n == 1 else "es"))
class SearchDock(QWidget):
    """Full-text search across the notebook. Results grouped by page."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QVBoxLayout
        self._TreeWidget = QTreeWidget
        self._TreeWidgetItem = QTreeWidgetItem
        self._search: Search | None = None
        self._on_activate_page = lambda path, line: None
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("Search")
        self.input.returnPressed.connect(self._run)
        self.case_cb = QCheckBox("Aa", self)
        self.case_cb.setToolTip("Case sensitive")
        self.word_cb = QCheckBox("Wo", self)
        self.word_cb.setToolTip("Whole word")
        self.regex_cb = QCheckBox(".*", self)
        self.regex_cb.setToolTip("Regex")
        self.btn = QPushButton("Search", self)
        self.btn.clicked.connect(self._run)
        top.addWidget(self.input, 1)
        top.addWidget(self.case_cb)
        top.addWidget(self.word_cb)
        top.addWidget(self.regex_cb)
        top.addWidget(self.btn)
        v.addLayout(top)
        self.results = QTreeWidget(self)
        self.results.setHeaderHidden(True)
        self.results.itemActivated.connect(self._on_item_activated)
        v.addWidget(self.results, 1)
        self.status = QLabel("", self)
        v.addWidget(self.status)

    def set_search(self, search: Search | None) -> None:
        self._search = search

    def set_on_activate(self, cb) -> None:
        self._on_activate_page = cb

    def focus_query(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def set_query(self, text: str) -> None:
        self.input.setText(text)
        self._run()

    def _run(self) -> None:
        self.results.clear()
        if self._search is None:
            self.status.setText("No notebook open")
            return
        text = self.input.text()
        if not text:
            self.status.setText("")
            return
        hits = self._search.query(
            text,
            case=self.case_cb.isChecked(),
            whole_word=self.word_cb.isChecked(),
            regex=self.regex_cb.isChecked(),
        )
        by_page: dict[str, list[Hit]] = {}
        for h in hits:
            by_page.setdefault(h.page_path, []).append(h)
        for page, page_hits in by_page.items():
            top = self._TreeWidgetItem([f"{page}  ({len(page_hits)})"])
            top.setData(0, Qt.ItemDataRole.UserRole, (page, -1))
            for h in page_hits:
                snippet = h.line_text.strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                child = self._TreeWidgetItem([f"{h.line_no}: {snippet}"])
                child.setData(0, Qt.ItemDataRole.UserRole, (page, h.line_no))
                top.addChild(child)
            self.results.addTopLevelItem(top)
            top.setExpanded(True)
        self.status.setText(
            f"{len(hits)} hit" + ("" if len(hits) == 1 else "s")
            + f" in {len(by_page)} page" + ("" if len(by_page) == 1 else "s")
        )

    def _on_item_activated(self, item, _col) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        page, line = data
        self._on_activate_page(page, line)


class TagsDock(QWidget):
    """Tag cloud: flat list of tags sorted by count. Click → search."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout
        self._ListWidgetItem = QListWidgetItem
        self._on_tag_clicked = lambda name: None
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)
        self.list = QListWidget(self)
        self.list.itemActivated.connect(self._on_activated)
        self.list.itemClicked.connect(self._on_activated)
        v.addWidget(self.list, 1)

    def set_on_clicked(self, cb) -> None:
        self._on_tag_clicked = cb

    def refresh(self, tag_counts: list[tuple[str, int]]) -> None:
        self.list.clear()
        for tag, count in tag_counts:
            item = self._ListWidgetItem(f"#{tag}  ({count})")
            item.setData(Qt.ItemDataRole.UserRole, tag)
            self.list.addItem(item)

    def _on_activated(self, item) -> None:
        tag = item.data(Qt.ItemDataRole.UserRole)
        if tag:
            self._on_tag_clicked(tag)


from .history import NavigationHistory  # noqa: E402
from .index import Index  # noqa: E402
from .notebook import Notebook  # noqa: E402
from .page_model import PageTreeModel  # noqa: E402


class _SaveMergeSignals(QObject):
    finished = pyqtSignal(str, str, object, object)


class _SaveMergeTask(QRunnable):
    def __init__(self, page: str, md: str, deferred) -> None:
        super().__init__()
        self.page = page
        self.md = md
        self.deferred = deferred
        self.signals = _SaveMergeSignals()

    def run(self) -> None:
        result = None
        error = None
        try:
            from . import safe_save
            result = safe_save.run_deferred_save(self.deferred)
        except Exception as exc:  # pragma: no cover - surfaced through signal
            error = exc
        self.signals.finished.emit(self.page, self.md, result, error)


class MainWindow(QMainWindow):
    def __init__(self, notebook_path: str | None = None) -> None:
        super().__init__()
        self.setWindowTitle("qnotebook")
        self.resize(1100, 720)

        self._settings = QSettings("qnotebook", "qnotebook")
        self._recent: list[str] = []
        self._bookmarks: list[str] = []
        self.notebook: Notebook | None = None
        self.index: Index | None = None
        self.model: PageTreeModel | None = None
        self.search: Search | None = None
        self.history = NavigationHistory()
        self._current_page: str | None = None
        self._pending_save_merges: dict[str, _SaveMergeTask] = {}
        self._pending_merge_again: set[str] = set()

        self._build_ui()
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self.apply_custom_shortcuts()

        path = notebook_path or self._settings.value("last_notebook", type=str)
        if path:
            self.open_notebook(path)

    # ---- UI ----

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.tree = QTreeView(self)
        self.tree.setHeaderHidden(True)
        self.tree.clicked.connect(self._on_tree_clicked)
        self.tree.activated.connect(self._on_tree_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        from PyQt6.QtWidgets import QAbstractItemView
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)

        # Alt-arrow tree navigation shortcuts (window-wide).
        for seq, slot in [
            ("Alt+Down", "tree_nav_down"),
            ("Alt+Up", "tree_nav_up"),
            ("Alt+Right", "tree_nav_expand"),
            ("Alt+Shift+Left", "tree_nav_collapse"),  # Alt+Left taken by history back
            ("Alt+Return", "tree_nav_open"),
            ("Alt+Enter", "tree_nav_open"),
        ]:
            act = QAction(self)
            act.setShortcut(QKeySequence(seq))
            act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            act.triggered.connect(lambda _c=False, s=slot: getattr(self, s)())
            self.addAction(act)

        self.editor = MarkdownEditor(self)
        self.editor.linkActivated.connect(self._on_link_activated)
        self.editor.dirtyChanged.connect(self._on_dirty_changed)
        self.editor.imageDropped.connect(self._on_image_dropped)
        self.editor.imagePasted.connect(self._on_image_pasted)
        self.editor.fileDropped.connect(self._on_file_dropped)
        self.editor.autoSaveRequested.connect(self._auto_save)
        self.editor.escapePressed.connect(self._clear_search_highlights)
        self.editor.textChanged.connect(self._update_status)
        # Debounce toc refresh
        from PyQt6.QtCore import QTimer
        self._toc_refresh_timer = QTimer(self)
        self._toc_refresh_timer.setSingleShot(True)
        self._toc_refresh_timer.setInterval(200)
        self._toc_refresh_timer.timeout.connect(self._refresh_toc)
        self.editor.textChanged.connect(self._toc_refresh_timer.start)
        # Load autosave prefs
        autosave_ms = int(self._settings.value("autosave_ms", 30000, type=int))
        autosave_on = self._settings.value("autosave_enabled", True, type=bool)
        self.editor.set_autosave_interval_ms(autosave_ms)
        self.editor.set_autosave_enabled(bool(autosave_on))

        self.find_bar = FindBar(self.editor, self)

        # External-change watcher for the current page.
        from .watchdog import PageWatcher
        self._page_watcher = PageWatcher(self)
        self._page_watcher.fileChanged.connect(self._on_external_page_change)

        from . import safe_save as _safe_save
        self._page_load_result: dict[str, _safe_save.LoadResult] = {}

        # Primary editor pane (wrapped in a vbox with the find bar).
        from PyQt6.QtWidgets import QVBoxLayout
        primary_pane = QWidget(self)
        pv = QVBoxLayout(primary_pane)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(0)
        pv.addWidget(self.editor, 1)
        pv.addWidget(self.find_bar)
        primary_pane._zimqt_editor = self.editor  # type: ignore[attr-defined]
        self._primary_pane = primary_pane

        # A QSplitter that holds 1 or 2 editor panes (horizontal or vertical).
        self._editor_split = QSplitter(Qt.Orientation.Horizontal, self)
        self._editor_split.addWidget(primary_pane)
        self._secondary_pane = None  # created on demand
        self._secondary_editor = None

        editor_container = QWidget(self)
        ec_layout = QHBoxLayout(editor_container)
        ec_layout.setContentsMargins(0, 0, 0, 0)
        ec_layout.setSpacing(0)
        ec_layout.addWidget(self._editor_split, 1)

        splitter.addWidget(self.tree)
        splitter.addWidget(editor_container)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 860])
        self.setCentralWidget(splitter)

        self.backlinks_list = QListWidget(self)
        self.backlinks_list.itemActivated.connect(self._on_backlink_activated)
        dock = QDockWidget("Backlinks", self)
        dock.setWidget(self.backlinks_list)
        dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        self.backlinks_dock = dock

        self.tags_dock_widget = TagsDock(self)
        self.tags_dock_widget.set_on_clicked(self._on_tag_clicked)
        tags_dock = QDockWidget("Tags", self)
        tags_dock.setWidget(self.tags_dock_widget)
        tags_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, tags_dock)
        tags_dock.hide()
        self.tags_dock = tags_dock

        from .linkmap import LinkMapDock
        self.linkmap_widget = LinkMapDock(self)
        self.linkmap_widget.set_on_navigate(self.load_page)
        self.linkmap_widget.set_on_hops_changed(lambda _h: self._refresh_linkmap())
        linkmap_dock = QDockWidget("Link Map", self)
        linkmap_dock.setWidget(self.linkmap_widget)
        linkmap_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, linkmap_dock)
        linkmap_dock.hide()
        self.linkmap_dock = linkmap_dock

        from .toc import TocDock
        self.toc_widget = TocDock(self)
        self.toc_widget.set_on_activated(self._jump_to_line_from_toc)
        toc_dock = QDockWidget("Table of Contents", self)
        toc_dock.setWidget(self.toc_widget)
        toc_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, toc_dock)
        toc_dock.hide()
        self.toc_dock = toc_dock

        from .journal import CalendarDock
        self.calendar_widget = CalendarDock(self)
        self.calendar_widget.set_on_date_activated(self._on_calendar_date_activated)
        self.calendar_widget.set_page_exists(
            lambda p: self.notebook is not None and self.notebook.exists(p)
        )
        calendar_dock = QDockWidget("Calendar", self)
        calendar_dock.setWidget(self.calendar_widget)
        calendar_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, calendar_dock)
        calendar_dock.hide()
        self.calendar_dock = calendar_dock

        self.search_dock_widget = SearchDock(self)
        self.search_dock_widget.set_on_activate(self._on_search_hit_activated)
        search_dock = QDockWidget("Search", self)
        search_dock.setWidget(self.search_dock_widget)
        search_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, search_dock)
        search_dock.hide()
        self.search_dock = search_dock

        self.setStatusBar(QStatusBar(self))
        self._status_label = QLabel("")
        self.statusBar().addPermanentWidget(self._status_label)

        from PyQt6.QtWidgets import QPushButton

        from .sync_conflict import ConflictWatcher
        self._conflict_badge = QPushButton("")
        self._conflict_badge.setFlat(True)
        self._conflict_badge.hide()
        self._conflict_badge.clicked.connect(self._open_conflict_resolver)
        self.statusBar().addPermanentWidget(self._conflict_badge)
        self._conflict_watcher = ConflictWatcher(self)
        self._conflict_watcher.conflictFileFound.connect(self._on_conflict_found)

    def _build_actions(self) -> None:
        self.act_open = QAction("&Open Notebook...", self)
        self.act_open.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open.triggered.connect(self._choose_notebook)

        self.act_new_page = QAction("&New Page...", self)
        self.act_new_page.setShortcut(QKeySequence("Ctrl+N"))
        self.act_new_page.triggered.connect(self._new_page)

        self.act_save = QAction("&Save", self)
        self.act_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_save.triggered.connect(self._save_current)

        self.act_back = QAction("Back", self)
        self.act_back.setShortcut(QKeySequence("Alt+Left"))
        self.act_back.triggered.connect(self._go_back)

        self.act_forward = QAction("Forward", self)
        self.act_forward.setShortcut(QKeySequence("Alt+Right"))
        self.act_forward.triggered.connect(self._go_forward)

        self.act_bold = QAction("Bold", self)
        self.act_bold.setShortcut(QKeySequence("Ctrl+B"))
        self.act_bold.triggered.connect(lambda: self.editor.toggle_bold())

        self.act_italic = QAction("Italic", self)
        self.act_italic.setShortcut(QKeySequence("Ctrl+I"))
        self.act_italic.triggered.connect(lambda: self.editor.toggle_italic())

        self.act_strike = QAction("Strike", self)
        self.act_strike.triggered.connect(lambda: self.editor.toggle_strike())

        self.act_code = QAction("Code", self)
        self.act_code.triggered.connect(lambda: self.editor.toggle_code())

        self.act_h1 = QAction("H1", self)
        self.act_h1.triggered.connect(lambda: self.editor.set_heading(1))
        self.act_h2 = QAction("H2", self)
        self.act_h2.triggered.connect(lambda: self.editor.set_heading(2))
        self.act_h3 = QAction("H3", self)
        self.act_h3.triggered.connect(lambda: self.editor.set_heading(3))
        self.act_para = QAction("Paragraph", self)
        self.act_para.triggered.connect(lambda: self.editor.set_heading(0))

        self.act_find = QAction("&Find...", self)
        self.act_find.setShortcut(QKeySequence.StandardKey.Find)
        self.act_find.triggered.connect(self._open_find)

        self.act_find_next = QAction("Find Next", self)
        self.act_find_next.setShortcuts([QKeySequence("Ctrl+G"), QKeySequence("F3")])
        self.act_find_next.triggered.connect(self._find_next)

        self.act_find_prev = QAction("Find Previous", self)
        self.act_find_prev.setShortcut(QKeySequence("Shift+F3"))
        self.act_find_prev.triggered.connect(self._find_prev)

        self.act_search = QAction("&Search Notebook...", self)
        self.act_search.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self.act_search.triggered.connect(self._open_search)

        self.act_insert_image = QAction("Insert &Image...", self)
        self.act_insert_image.triggered.connect(self._insert_image_dialog)

        self.act_insert_date = QAction("Insert &Date", self)
        self.act_insert_date.setShortcut(QKeySequence("Ctrl+D"))
        self.act_insert_date.triggered.connect(self._insert_date)

        self.act_insert_time = QAction("Insert &Time", self)
        self.act_insert_time.triggered.connect(self._insert_time)

        self.act_insert_datetime = QAction("Insert Date+Time", self)
        self.act_insert_datetime.triggered.connect(self._insert_datetime)

        self.act_insert_hr = QAction("Insert &Horizontal Rule", self)
        self.act_insert_hr.triggered.connect(self._insert_hr)

        self.act_insert_symbol = QAction("Insert &Symbol...", self)
        self.act_insert_symbol.triggered.connect(self._insert_symbol_dialog)

        self.act_insert_attachment = QAction("Insert &Attachment...", self)
        self.act_insert_attachment.triggered.connect(self._insert_attachment_dialog)

        self.act_bookmark = QAction("&Bookmark This Page", self)
        self.act_bookmark.triggered.connect(self._toggle_bookmark_current)

        self.act_manage_bookmarks = QAction("Manage Bookmarks...", self)
        self.act_manage_bookmarks.triggered.connect(self._manage_bookmarks)

        self.act_export_page_html = QAction("Current page as &HTML...", self)
        self.act_export_page_html.triggered.connect(self._export_page_html)

        self.act_export_notebook_html = QAction("Whole &notebook as HTML...", self)
        self.act_export_notebook_html.triggered.connect(self._export_notebook_html)

        self.act_export_page_pdf = QAction("Current page as &PDF...", self)
        self.act_export_page_pdf.triggered.connect(self._export_page_pdf)

        self.act_toggle_versioning = QAction("Enable &Version History", self, checkable=True)
        self.act_toggle_versioning.triggered.connect(self._toggle_versioning)

        self.act_print = QAction("&Print...", self)
        self.act_print.setShortcut(QKeySequence("Ctrl+Alt+P"))
        self.act_print.triggered.connect(self._print_current_page)

        self.act_page_history = QAction("Page &History...", self)
        self.act_page_history.triggered.connect(self._open_page_history)
        self.act_snapshots = QAction("&Snapshots...", self)
        self.act_snapshots.triggered.connect(self._open_snapshots)

        self.act_quick_note = QAction("&Quick Note", self)
        self.act_quick_note.setShortcut(QKeySequence("Ctrl+Alt+N"))
        self.act_quick_note.triggered.connect(self._open_quick_note)

        self.act_quick_switch = QAction("&Quick Switch...", self)
        self.act_quick_switch.setShortcut(QKeySequence("Ctrl+P"))
        self.act_quick_switch.triggered.connect(self._open_quick_switcher)
        self.act_command_palette = QAction("Command &Palette...", self)
        self.act_command_palette.setShortcut(QKeySequence("Ctrl+Shift+P"))
        self.act_command_palette.triggered.connect(self._open_command_palette)
        self.addAction(self.act_command_palette)

        self.act_settings = QAction("Settings...", self)
        self.act_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.act_settings.triggered.connect(self._open_settings)

        self.act_quit = QAction("&Quit", self)
        self.act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.act_quit.triggered.connect(self.close)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        m_file.addAction(self.act_open)
        self.m_recent_notebooks = m_file.addMenu("Recent &Notebooks")
        self.m_recent_notebooks.aboutToShow.connect(self._populate_recent_notebooks_menu)
        m_file.addAction(self.act_new_page)
        self.m_new_from_template = m_file.addMenu("New from &Template")
        self.m_new_from_template.aboutToShow.connect(self._populate_templates_menu)
        m_file.addAction(self.act_save)
        m_file.addSeparator()
        self.m_import = m_file.addMenu("&Import")
        self.act_import_zim = QAction("From &Zim notebook...", self)
        self.act_import_zim.triggered.connect(self._import_from_zim)
        self.m_import.addAction(self.act_import_zim)
        self.m_export = m_file.addMenu("&Export")
        self.m_export.addAction(self.act_export_page_html)
        self.m_export.addAction(self.act_export_notebook_html)
        self.m_export.addAction(self.act_export_page_pdf)
        self.m_export.addSeparator()
        act_css = QAction("Edit Export &CSS...", self)
        act_css.triggered.connect(self._edit_export_css)
        self.m_export.addAction(act_css)
        m_file.addSeparator()
        m_file.addAction(self.act_print)
        m_file.addSeparator()
        m_file.addAction(self.act_toggle_versioning)
        m_file.addAction(self.act_snapshots)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)
        m_edit = mb.addMenu("&Edit")
        m_edit.addAction(self.act_find)
        m_edit.addAction(self.act_find_next)
        m_edit.addAction(self.act_find_prev)
        m_edit.addAction(self.act_search)
        m_edit.addAction(self.act_quick_switch)
        m_edit.addAction(self.act_quick_note)
        m_edit.addSeparator()
        # qdistro Send-To — populated lazily so the menu reflects the
        # live broker state rather than what was running at startup.
        self.m_send_to = m_edit.addMenu("Send &To")
        self.m_send_to.aboutToShow.connect(self._populate_send_to_menu)
        m_edit.addSeparator()
        m_edit.addAction(self.act_settings)
        m_insert = mb.addMenu("&Insert")
        m_insert.addAction(self.act_insert_image)
        m_insert.addAction(self.act_insert_date)
        m_insert.addAction(self.act_insert_time)
        m_insert.addAction(self.act_insert_datetime)
        m_insert.addAction(self.act_insert_hr)
        m_insert.addAction(self.act_insert_symbol)
        m_insert.addAction(self.act_insert_attachment)
        self.m_insert = m_insert
        m_nav = mb.addMenu("&Navigate")
        m_nav.addAction(self.act_back)
        m_nav.addAction(self.act_forward)
        self.m_go = mb.addMenu("&Go")
        self.m_go.aboutToShow.connect(self._populate_go_menu)
        m_view = mb.addMenu("&View")
        self.act_toggle_calendar = QAction("&Calendar", self, checkable=True)
        self.act_toggle_calendar.triggered.connect(self._toggle_calendar_dock)
        m_view.addAction(self.act_toggle_calendar)
        self.act_toggle_toc = QAction("&Table of Contents", self, checkable=True)
        self.act_toggle_toc.triggered.connect(self._toggle_toc_dock)
        m_view.addAction(self.act_toggle_toc)
        from .spell import HAS_ENCHANT
        self.act_toggle_linkmap = QAction("&Link Map", self, checkable=True)
        self.act_toggle_linkmap.triggered.connect(self._toggle_linkmap_dock)
        m_view.addAction(self.act_toggle_linkmap)
        m_view.addAction(self.act_page_history)
        m_view.addSeparator()
        from PyQt6.QtGui import QActionGroup
        self.act_view_tree = QAction("Show: Page &Tree", self, checkable=True)
        self.act_view_recent = QAction("Show: &Recent List", self, checkable=True)
        grp = QActionGroup(self)
        grp.setExclusive(True)
        grp.addAction(self.act_view_tree)
        grp.addAction(self.act_view_recent)
        self.act_view_tree.setChecked(True)
        self.act_view_tree.triggered.connect(lambda: self._set_outline_mode("tree"))
        self.act_view_recent.triggered.connect(lambda: self._set_outline_mode("recent"))
        m_view.addAction(self.act_view_tree)
        m_view.addAction(self.act_view_recent)
        m_view.addSeparator()
        self.act_dark = QAction("&Dark Mode", self, checkable=True)
        self.act_dark.toggled.connect(self._toggle_dark_mode)
        m_view.addAction(self.act_dark)
        if bool(self._settings.value("dark_mode", False, type=bool)):
            self.act_dark.setChecked(True)
        self.act_toggle_spell = QAction("&Spell Check", self, checkable=True)
        self.act_toggle_spell.setEnabled(HAS_ENCHANT)
        self.act_toggle_spell.triggered.connect(self._toggle_spell_check)
        m_view.addAction(self.act_toggle_spell)
        # Persist preference
        spell_pref = self._settings.value("spell_enabled", False, type=bool)
        if HAS_ENCHANT and bool(spell_pref):
            self.act_toggle_spell.setChecked(True)
            self._toggle_spell_check(True)
        m_view.addSeparator()
        m_split = m_view.addMenu("&Split")
        self.act_split_horizontal = QAction("Split &Horizontal", self)
        self.act_split_horizontal.triggered.connect(lambda: self.split_editor("horizontal"))
        m_split.addAction(self.act_split_horizontal)
        self.act_split_vertical = QAction("Split &Vertical", self)
        self.act_split_vertical.triggered.connect(lambda: self.split_editor("vertical"))
        m_split.addAction(self.act_split_vertical)
        self.act_split_close = QAction("&Close Split", self)
        self.act_split_close.triggered.connect(self.close_split)
        m_split.addAction(self.act_split_close)
        self.m_view = m_view
        m_tools = mb.addMenu("&Tools")
        self.act_statistics = QAction("&Statistics...", self)
        self.act_statistics.triggered.connect(self._show_statistics)
        m_tools.addAction(self.act_statistics)
        self.m_tools = m_tools
        self.m_plugins = mb.addMenu("&Plugins")
        m_fmt = mb.addMenu("F&ormat")
        m_fmt.addAction(self.act_bold)
        m_fmt.addAction(self.act_italic)
        m_fmt.addAction(self.act_strike)
        m_fmt.addAction(self.act_code)
        m_fmt.addSeparator()
        m_fmt.addAction(self.act_h1)
        m_fmt.addAction(self.act_h2)
        m_fmt.addAction(self.act_h3)
        m_fmt.addAction(self.act_para)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main", self)
        tb.addAction(self.act_back)
        tb.addAction(self.act_forward)
        tb.addSeparator()
        tb.addAction(self.act_save)
        tb.addAction(self.act_new_page)
        tb.addSeparator()
        tb.addAction(self.act_bold)
        tb.addAction(self.act_italic)
        tb.addAction(self.act_strike)
        tb.addAction(self.act_code)
        tb.addSeparator()
        tb.addAction(self.act_h1)
        tb.addAction(self.act_h2)
        tb.addAction(self.act_h3)
        tb.addAction(self.act_para)
        tb.addSeparator()
        tb.addAction(self.act_insert_image)
        tb.addAction(self.act_insert_attachment)
        tb.addAction(self.act_find)
        tb.addAction(self.act_search)
        self.addToolBar(tb)
        self.toolbar = tb

    # ---- notebook ops ----

    def _choose_notebook(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Open Notebook")
        if path:
            self.open_notebook(path)

    def _setup_plugins(self) -> None:
        """Discover plugins, populate Plugins menu, activate enabled ones."""
        from . import plugins as plugins_mod
        # Reset plugins menu
        if hasattr(self, "m_plugins"):
            self.m_plugins.clear()
        infos = plugins_mod.discover(
            self.notebook.root if self.notebook is not None else None
        )
        self._plugin_infos = infos
        enabled_raw = self._settings.value("plugins_enabled", [], type=list) or []
        enabled = set(str(x) for x in enabled_raw)
        for info in infos:
            act = QAction(f"{info.name}", self)
            act.setCheckable(True)
            token = plugins_mod.enabled_token(info)
            act.setChecked(token is not None and token in enabled)
            act.setToolTip(info.description)
            act.toggled.connect(
                lambda checked, key=info.key, action=act: self._toggle_plugin(
                    key, checked, action
                )
            )
            self.m_plugins.addAction(act)
        plugins_mod.setup_enabled(self, infos, enabled)

    def _confirm_enable_user_plugin(self, info) -> bool:
        """Confirm before enabling a user plugin — enabling executes its code."""
        path = str(info.path) if info.path is not None else "(unknown)"
        resp = QMessageBox.question(
            self,
            "Enable notebook plugin?",
            f"“{info.name}” is a plugin bundled with this notebook.\n"
            f"Enabling it runs its Python code with your privileges every time "
            f"this notebook opens.\n\nOnly enable plugins from notebooks you trust.\n\n"
            f"{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return resp == QMessageBox.StandardButton.Yes

    def _toggle_plugin(self, key: str, enabled: bool, action=None) -> None:
        from . import plugins as plugins_mod
        info = next(
            (i for i in getattr(self, "_plugin_infos", []) if i.key == key), None
        )
        token = plugins_mod.enabled_token(info) if info is not None else None
        if token is None:
            # Not discoverable / not hashable → cannot be trusted; refuse silently.
            return
        raw = self._settings.value("plugins_enabled", [], type=list) or []
        cur = set(str(x) for x in raw)
        if enabled:
            # User plugins execute arbitrary code — gate the explicit opt-in.
            if info.source == "user" and not self._confirm_enable_user_plugin(info):
                # Revert this exact action's check without recursing back in.
                # Match by identity, not display name (names can collide).
                if action is not None:
                    action.blockSignals(True)
                    action.setChecked(False)
                    action.blockSignals(False)
                return
            cur.add(token)
            plugins_mod.setup_enabled(self, [info], {token})
        else:
            cur.discard(token)
        self._settings.setValue("plugins_enabled", sorted(cur))

    def open_notebook(self, path: str) -> None:
        from . import locks as _locks
        from .templates import ensure_builtin_templates
        root = Path(path)
        # Attempt to take the lock. Existing + alive lock prompts the user.
        acquired, existing = _locks.acquire(root)
        self._read_only = False
        if not acquired:
            choice = self._prompt_lock_conflict(existing)
            if choice == "cancel":
                return
            elif choice == "readonly":
                self._read_only = True
            elif choice == "force":
                _locks.force_acquire(root)
        self.notebook = Notebook(root)
        ensure_builtin_templates(self.notebook)
        self.index = Index(self.notebook)
        self.index.rebuild()
        self.search = Search(self.notebook, self.index)
        self.search_dock_widget.set_search(self.search)
        self._load_recent_and_bookmarks()
        self._refresh_tags()
        self.model = PageTreeModel(self.notebook, self)
        self.model.set_index(self.index)
        self.model.pageMoved.connect(self._on_page_moved)
        self.tree.setModel(self.model)
        self._settings.setValue("last_notebook", str(self.notebook.root))
        self._push_recent_notebook(str(self.notebook.root))
        self._apply_versioning_policy(root)
        if hasattr(self, "_conflict_watcher"):
            self._conflict_watcher.set_root(root)
            self._refresh_conflict_badge()
        try:
            from .single_instance import CommandServer
            if not hasattr(self, "_cmd_server"):
                self._cmd_server = CommandServer(self)
            self._cmd_server.start(root)
        except Exception:
            pass
        self.act_toggle_versioning.setChecked(
            bool(self._settings.value("versioning_enabled", False, type=bool))
        )
        self._update_title()
        self._setup_plugins()
        first = next(iter(self.notebook.pages()), None)
        if first:
            self.load_page(first.path)
        # Restore session if enabled (default on).
        if bool(self._settings.value("session_restore_enabled", True, type=bool)):
            try:
                from . import session as _session
                data = _session.load(self.notebook.root)
                if data:
                    _session.restore(self, data)
            except Exception:
                pass

    def load_page(self, page_path: str) -> None:
        if self.notebook is None:
            return
        if not self.notebook.exists(page_path):
            self.notebook.create_page(page_path)
            if self.index:
                self.index.update_page(page_path, "")
            if self.model:
                self.model.refresh()
                self.tree.setModel(self.model)
        text = self.notebook.get_page(page_path)
        # Defensive: if the editor is still dirty on the same page (e.g. a
        # programmatic reload triggered without going through save/discard),
        # don't clobber the original LoadResult — keep the pre-load baseline
        # so a subsequent save runs a correct 3-way merge instead of
        # snapshotting the external state as the new base.
        preserve_lr = (
            page_path in self._page_load_result
            and self._current_page == page_path
            and self.editor.is_dirty()
        )
        if not preserve_lr:
            self._page_load_result[page_path] = self.notebook.load_for_save(
                page_path)
        self.editor.load_markdown(
            text, page_path=page_path,
            base_path=self.notebook.file_for(page_path).parent,
            transclusion_resolver=self._make_transclusion_resolver(page_path),
        )
        self._current_page = page_path
        self._push_recent(page_path)
        if hasattr(self, "_page_watcher"):
            self._page_watcher.watch(self.notebook.file_for(page_path))
        self.history.push(page_path)
        self._refresh_backlinks()
        self._refresh_linkmap()
        self._update_status()
        self._update_title()
        if self.model:
            idx = self.model.index_for_page(page_path)
            if idx.isValid():
                self.tree.setCurrentIndex(idx)

    def _save_current(self) -> None:
        if self.notebook is None or self._current_page is None:
            return
        page = self._current_page
        if page in self._pending_save_merges:
            self._pending_merge_again.add(page)
            return
        md = self.editor.markdown()
        lr = self._page_load_result.get(page)
        result = self.notebook.save_page(
            page, md, load_result=lr, allow_subprocess=False,
        )
        if result.needs_merge:
            if result.deferred is None:
                QMessageBox.warning(
                    self, "Save failed",
                    "Save requires a merge, but no deferred merge payload was created.",
                )
                return
            self._dispatch_save_merge(page, md, result.deferred)
            return
        self._finish_save_result(page, md, result)

    def _dispatch_save_merge(self, page: str, md: str, deferred) -> None:
        if page == self._current_page and hasattr(self, "_page_watcher"):
            self._page_watcher.watch(None)
        task = _SaveMergeTask(page, md, deferred)
        self._pending_save_merges[page] = task
        task.signals.finished.connect(
            self._on_save_merge_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        QThreadPool.globalInstance().start(task)

    def _on_save_merge_finished(self, page: str, md: str, result, error) -> None:
        self._pending_save_merges.pop(page, None)
        rerun = page in self._pending_merge_again
        self._pending_merge_again.discard(page)
        if page == self._current_page:
            self._watch_current_page()
        if error is not None:
            if page == self._current_page:
                QMessageBox.warning(self, "Save failed", str(error))
            return
        if result is None:
            return
        if page != self._current_page or self.editor.markdown() != md:
            if page == self._current_page and self.editor.is_dirty():
                self._save_current()
            return
        self._finish_save_result(page, md, result)
        if rerun and page == self._current_page and self.editor.is_dirty():
            self._save_current()

    def _finish_save_result(self, page: str, md: str,
                            result) -> None:
        if self.notebook is None:
            return
        if result.conflict:
            from .merge_dialog import MergeDialog
            dlg = MergeDialog(result.base, result.ours, result.theirs,
                              page_name=page, parent=self)
            if dlg.exec() and dlg.outcome == MergeDialog.RESULT_MERGED:
                from . import safe_save as _ss
                _ss.atomic_write(self.notebook.file_for(page), dlg.result_bytes)
                md = dlg.result_bytes.decode("utf-8", errors="replace")
                rung = "user-merged"
            elif dlg.outcome == MergeDialog.RESULT_CONFLICT_FILE:
                conflict_path = self.notebook.file_for(page).with_suffix(
                    ".md.conflict")
                from . import safe_save as _ss
                _ss.atomic_write(conflict_path, dlg.result_bytes)
                return
            else:
                return
        else:
            rung = result.rung
            if (
                page == self._current_page
                and result.ok
                and result.bytes
                and result.bytes != md.encode("utf-8")
            ):
                md = result.bytes.decode("utf-8", errors="replace")
                self.editor.load_markdown(
                    md,
                    page_path=page,
                    base_path=self.notebook.file_for(page).parent,
                    transclusion_resolver=self._make_transclusion_resolver(page),
                )
        if self.index:
            self.index.update_page(page, md)
        self.editor.clear_dirty()
        if page == self._current_page:
            self._watch_current_page()
        self._maybe_versioning_commit(page, rung=rung)
        # Refresh baseline for future saves
        self._page_load_result[page] = self.notebook.load_for_save(page)
        self._refresh_backlinks()
        self._refresh_tags()
        self._refresh_toc()
        self._update_status()

    def _watch_current_page(self) -> None:
        if (
            self.notebook is not None
            and self._current_page is not None
            and hasattr(self, "_page_watcher")
        ):
            self._page_watcher.watch(self.notebook.file_for(self._current_page))

    def _maybe_versioning_commit(self, page: str, rung: str | None = None) -> None:
        if self.notebook is None:
            return
        if not bool(self._settings.value("versioning_enabled", False, type=bool)):
            return
        from . import versioning
        # Dispatch the git commit to a background worker thread: `git add` +
        # `commit` can take seconds on a large repo and would otherwise freeze
        # the editor on every (auto)save. The commit is a fire-and-forget side
        # effect — its result isn't consumed by the UI.
        versioning.commit_page_async(self.notebook.root, page, rung=rung)

    def _drain_pending_commits(self) -> None:
        """Block until queued async commits finish.

        Called before any operation that mutates page *paths* (rename, move,
        delete): a queued commit stages by page-relative path at worker time,
        so it must land against the still-current path before the file moves —
        otherwise the deferred commit would record a spurious deletion of the
        old path and leave the saved content uncommitted at the new one."""
        try:
            self._drain_pending_save_merges(-1)
            from . import versioning
            versioning.wait_for_pending_commits(-1)
        except Exception:
            pass

    def _drain_pending_save_merges(self, timeout_ms: int = -1) -> bool:
        app = QApplication.instance()
        start = time.monotonic()
        while self._pending_save_merges:
            if app is not None:
                app.processEvents()
            else:
                time.sleep(0.01)
            if timeout_ms >= 0 and (time.monotonic() - start) * 1000 >= timeout_ms:
                return False
        return True

    def _apply_versioning_policy(self, root: Path) -> None:
        """New notebooks get versioning on silently. Existing notebooks that
        haven't been prompted yet see a one-time recommendation dialog."""
        from . import nb_settings
        if nb_settings.is_new_notebook(root):
            nb_settings.set_value(root, "versioning_enabled", True)
            nb_settings.set_value(root, "versioning_prompted", True)
            self._settings.setValue("versioning_enabled", True)
            from . import versioning as _v
            _v.init_repo(root)
            return
        if not nb_settings.get(root, "versioning_prompted", False):
            reply = QMessageBox.question(
                self, "Enable version history?",
                "Enable version history for this notebook?\n"
                "Every save will be committed to a local git repo in the "
                "notebook root, so you can review and restore past changes.\n\n"
                "(Recommended.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            enabled = (reply == QMessageBox.StandardButton.Yes)
            nb_settings.set_value(root, "versioning_enabled", enabled)
            nb_settings.set_value(root, "versioning_prompted", True)
            self._settings.setValue("versioning_enabled", enabled)
            if enabled:
                from . import versioning as _v
                _v.init_repo(root)
        else:
            enabled = bool(nb_settings.get(root, "versioning_enabled", False))
            self._settings.setValue("versioning_enabled", enabled)

    def _toggle_versioning(self, checked: bool) -> None:
        self._settings.setValue("versioning_enabled", bool(checked))
        if checked and self.notebook is not None:
            from . import versioning
            versioning.init_repo(self.notebook.root)

    def _new_page(self, template_name: str | None = None) -> None:
        if self.notebook is None:
            return
        from PyQt6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout

        from .templates import list_templates, load_template, render_template
        dlg = QDialog(self)
        dlg.setWindowTitle("New Page")
        form = QFormLayout(dlg)
        name_edit = QLineEdit(dlg)
        name_edit.setPlaceholderText("Foo or Foo:Bar")
        form.addRow("Page path:", name_edit)
        combo = QComboBox(dlg)
        available = list_templates(self.notebook)
        combo.addItems(available)
        if template_name and template_name in available:
            combo.setCurrentText(template_name)
        form.addRow("Template:", combo)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        path = name_edit.text().strip()
        if not path:
            return
        chosen_template = combo.currentText()
        if self.notebook.exists(path):
            QMessageBox.warning(self, "Exists", f"Page {path} already exists")
            return
        if chosen_template == "Blank":
            initial = f"# {path.rsplit(':', 1)[-1]}\n"
        else:
            tpl = load_template(self.notebook, chosen_template)
            initial = render_template(tpl, path)
        self.notebook.create_page(path, initial)
        if self.index:
            self.index.update_page(path)
        if self.model:
            self.model.refresh()
            self.tree.setModel(self.model)
        self.load_page(path)

    def create_page_from_template(self, page_path: str, template_name: str) -> None:
        """Programmatic helper: create a page using a named template.

        Used by New-from-template menu entries, calendar/journal, etc."""
        if self.notebook is None:
            return
        from .templates import load_template, render_template
        if self.notebook.exists(page_path):
            self.load_page(page_path)
            return
        tpl = load_template(self.notebook, template_name)
        initial = render_template(tpl, page_path) if tpl else f"# {page_path.rsplit(':', 1)[-1]}\n"
        self.notebook.create_page(page_path, initial)
        if self.index:
            self.index.update_page(page_path)
        if self.model:
            self.model.refresh()
            self.tree.setModel(self.model)
        self.load_page(page_path)

    # ---- navigation ----

    def _on_tree_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        # Recent-list mode: model items carry path in UserRole.
        cur_model = self.tree.model()
        if cur_model is not self.model:
            data = index.data(0x0100)
            if data:
                self.load_page(str(data))
                return
        if self.model is None:
            return
        ref = self.model.page_for_index(index)
        if ref is None:
            return
        self.load_page(ref.path)

    def _on_link_activated(self, target: str) -> None:
        if self.notebook is None:
            return
        if target.startswith(("http://", "https://", "mailto:")):
            return
        # Split off optional #Heading anchor.
        heading = ""
        if "#" in target:
            target, _, heading = target.partition("#")
        page = target.replace("/", ":").strip(":")
        # [[#Heading]] = same-page anchor: stay on the current page.
        if not page and heading and self._current_page:
            page = self._current_page
        # Alias resolution (frontmatter aliases)
        if page and self.index is not None and not self.notebook.exists(page):
            resolved = self.index.resolve_alias(page)
            if resolved:
                page = resolved
        if not page:
            return
        self.load_page(page)
        if heading:
            self._scroll_to_heading(heading)

    # ---- reveal / terminal ----

    # ---- session locks ----

    def _prompt_lock_conflict(self, existing: dict | None) -> str:
        """Default implementation shows a QMessageBox. Tests override this."""
        box = QMessageBox(self)
        box.setWindowTitle("Notebook already open")
        pid = existing.get("pid") if existing else "?"
        host = existing.get("host") if existing else "?"
        box.setText(
            f"This notebook appears to be open by PID {pid} on {host}. "
            "Open read-only, force open, or cancel?"
        )
        ro = box.addButton("Open read-only", QMessageBox.ButtonRole.AcceptRole)
        force = box.addButton("Force open", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is ro:
            return "readonly"
        if clicked is force:
            return "force"
        _ = cancel
        return "cancel"

    # ---- keyboard tree navigation ----

    def tree_nav_down(self) -> None:
        self._tree_nav_vert(1)

    def tree_nav_up(self) -> None:
        self._tree_nav_vert(-1)

    def tree_nav_expand(self) -> None:
        idx = self.tree.currentIndex()
        if idx.isValid():
            self.tree.expand(idx)

    def tree_nav_collapse(self) -> None:
        idx = self.tree.currentIndex()
        if idx.isValid():
            self.tree.collapse(idx)

    def tree_nav_open(self) -> None:
        """Open currently-selected tree page in editor, pushing to history,
        but keep focus in the editor afterwards."""
        if self.model is None:
            return
        idx = self.tree.currentIndex()
        if not idx.isValid():
            return
        ref = self.model.page_for_index(idx)
        if ref is not None:
            self.load_page(ref.path)
            self.editor.setFocus()

    def _tree_nav_vert(self, delta: int) -> None:
        if self.model is None:
            return
        idx = self.tree.currentIndex()
        if not idx.isValid():
            # Start at root's first child.
            new = self.model.index(0, 0)
        else:
            row = idx.row()
            parent = idx.parent()
            target_row = row + delta
            if 0 <= target_row < self.model.rowCount(parent):
                new = self.model.index(target_row, 0, parent)
            else:
                return
        if new.isValid():
            self.tree.setCurrentIndex(new)

    def reveal_in_file_manager(self, page_path: str | None) -> None:
        import subprocess
        if self.notebook is None:
            return
        if page_path:
            target_dir = self.notebook.file_for(page_path).parent
        else:
            target_dir = self.notebook.root
        try:
            subprocess.Popen(["xdg-open", str(target_dir)])
        except FileNotFoundError:
            pass

    def open_terminal_here(self, page_path: str | None) -> None:
        import os
        import subprocess
        if self.notebook is None:
            return
        cwd = (self.notebook.file_for(page_path).parent if page_path
               else self.notebook.root)
        term = os.environ.get("TERMINAL")
        candidates = [term] if term else []
        candidates += ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]
        for cmd in candidates:
            if not cmd:
                continue
            try:
                subprocess.Popen([cmd], cwd=str(cwd))
                return
            except FileNotFoundError:
                continue

    # ---- statistics ----

    def _show_statistics(self) -> None:
        if self.notebook is None or self.index is None:
            return
        from .statistics import show_dashboard
        show_dashboard(self, self.notebook, self.index)

    # ---- import ----

    def _import_from_zim(self) -> None:
        src = QFileDialog.getExistingDirectory(self, "Select Zim notebook directory")
        if not src:
            return
        dst = QFileDialog.getExistingDirectory(self, "Select target directory for markdown notebook")
        if not dst:
            return
        from .importers.zim_wiki import import_notebook
        written = import_notebook(Path(src), Path(dst))
        QMessageBox.information(
            self, "Import complete",
            f"Converted {len(written)} page(s). Open the target dir as a notebook.",
        )

    # ---- external change ----

    def _on_external_page_change(self, _path: str) -> None:
        """Called when the currently-open page's file changed on disk.

        If the editor is clean: reload silently. If dirty: prompt the user
        to keep-mine, reload, or view a diff."""
        if self.notebook is None or self._current_page is None:
            return
        if not self.editor.is_dirty():
            self._reload_current_page_silently()
            return
        self._external_change_prompt()

    def _reload_current_page_silently(self) -> None:
        if self.notebook is None or self._current_page is None:
            return
        page = self._current_page
        if not self.notebook.exists(page):
            return
        text = self.notebook.get_page(page)
        self.editor.load_markdown(
            text, page_path=page,
            base_path=self.notebook.file_for(page).parent,
            transclusion_resolver=self._make_transclusion_resolver(page),
        )
        # Baseline is now disk — next save's 3-way merge must use fresh bytes,
        # not the pre-external-change snapshot.
        self._page_load_result[page] = self.notebook.load_for_save(page)

    def _external_change_prompt(self) -> None:
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("File changed on disk")
        box.setText(
            f"'{self._current_page}' changed on disk but you have unsaved edits."
        )
        keep = box.addButton("Keep mine", QMessageBox.ButtonRole.RejectRole)
        reload_btn = box.addButton("Reload from disk", QMessageBox.ButtonRole.AcceptRole)
        diff_btn = box.addButton("Show diff", QMessageBox.ButtonRole.ActionRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is reload_btn:
            self._reload_current_page_silently()
        elif clicked is diff_btn:
            self._show_external_diff()
        # keep: no-op
        _ = keep  # unused

    def _show_external_diff(self) -> None:
        import difflib

        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout
        on_disk = self.notebook.get_page(self._current_page).splitlines(keepends=True)
        in_mem = self.editor.markdown().splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(
            on_disk, in_mem, fromfile="disk", tofile="buffer",
        ))
        dlg = QDialog(self)
        dlg.setWindowTitle("External change diff")
        v = QVBoxLayout(dlg)
        te = QPlainTextEdit(diff or "(no textual difference)")
        te.setReadOnly(True)
        v.addWidget(te)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        v.addWidget(btns)
        dlg.resize(720, 480)
        dlg.exec()

    # ---- split view ----

    def split_editor(self, orientation: str = "horizontal") -> None:
        """Open a second editor pane. `orientation` is 'horizontal' (side-by-side)
        or 'vertical' (stacked). If already split, reorient the splitter."""
        orient = (Qt.Orientation.Horizontal if orientation == "horizontal"
                  else Qt.Orientation.Vertical)
        self._editor_split.setOrientation(orient)
        if self._secondary_pane is not None:
            return
        from PyQt6.QtWidgets import QVBoxLayout
        sec_editor = MarkdownEditor(self)
        sec_editor.linkActivated.connect(self._on_link_activated)
        sec_editor.autoSaveRequested.connect(self._auto_save)
        pane = QWidget(self)
        pv = QVBoxLayout(pane)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(0)
        pv.addWidget(sec_editor, 1)
        pane._zimqt_editor = sec_editor  # type: ignore[attr-defined]
        self._editor_split.addWidget(pane)
        self._secondary_pane = pane
        self._secondary_editor = sec_editor
        # Mirror the currently-loaded page into the second pane so it starts
        # with meaningful content.
        if self._current_page and self.notebook is not None:
            sec_editor.load_markdown(
                self.notebook.get_page(self._current_page),
                page_path=self._current_page,
                base_path=self.notebook.file_for(self._current_page).parent,
            )
        # Make the secondary pane half the primary's size.
        total = self._editor_split.size().width() if orient == Qt.Orientation.Horizontal else self._editor_split.size().height()
        if total > 0:
            self._editor_split.setSizes([total // 2, total // 2])

    def close_split(self) -> None:
        """Close the secondary editor pane, if any."""
        if self._secondary_pane is None:
            return
        self._secondary_pane.setParent(None)
        self._secondary_pane.deleteLater()
        self._secondary_pane = None
        self._secondary_editor = None

    def is_split(self) -> bool:
        return self._secondary_pane is not None

    def _make_transclusion_resolver(self, origin_page: str):
        """Return a resolver callback that returns the target page body
        (or a specific heading section) as markdown, guarding against loops."""
        nb = self.notebook
        if nb is None:
            return None
        idx = self.index

        def resolve(target: str, _seen: set[str] | None = None) -> str | None:
            if _seen is None:
                _seen = set()
            page_part, heading = (target.split("#", 1) + [""])[:2] if "#" in target else (target, "")
            page = page_part.replace("/", ":").strip(":")
            if not page or page == origin_page or page in _seen:
                return None
            if not nb.exists(page):
                if idx is not None:
                    alt = idx.resolve_alias(page)
                    if alt:
                        page = alt
                    else:
                        return None
                else:
                    return None
            if page in _seen or page == origin_page:
                return None
            _seen = _seen | {page}
            body = nb.get_page(page)
            # Strip frontmatter
            try:
                from . import frontmatter as _fm
                _, body = _fm.split(body)
            except Exception:
                pass
            if heading:
                body = _extract_heading_section(body, heading)
            return body

        return resolve

    def _scroll_to_heading(self, heading: str) -> None:
        """Move the editor cursor to the first heading whose text matches."""
        target = heading.strip().lower()
        if not target:
            return
        doc = self.editor.document()
        block = doc.firstBlock()
        while block.isValid():
            bfmt = block.blockFormat()
            from .md_to_qdoc import BLOCK_KIND
            if (bfmt.property(BLOCK_KIND) or "") == "h":
                if block.text().strip().lower() == target:
                    cur = QTextCursor(block)
                    self.editor.setTextCursor(cur)
                    self.editor.ensureCursorVisible()
                    return
            block = block.next()

    def _go_back(self) -> None:
        p = self.history.go_back()
        if p:
            self._load_without_history(p)

    def _go_forward(self) -> None:
        p = self.history.go_forward()
        if p:
            self._load_without_history(p)

    def _load_without_history(self, page: str) -> None:
        if self.notebook is None or not self.notebook.exists(page):
            return
        text = self.notebook.get_page(page)
        self.editor.load_markdown(
            text, page_path=page,
            base_path=self.notebook.file_for(page).parent,
        )
        self._current_page = page
        self._refresh_backlinks()
        self._update_status()

    # ---- rename ----

    def rename_page(self, old: str, new: str) -> list[str]:
        """Rename a page, rewrite inbound wikilinks, reload editor if open."""
        if self.notebook is None or self.index is None:
            return []
        # Land any queued async commit before the page path changes.
        self._drain_pending_commits()
        modified = self.index.rename_page_and_rewrite(old, new)
        if self.model:
            self.model.refresh()
            self.tree.setModel(self.model)
        if self._current_page == old:
            # Editor text is stale (it points to the old path).
            self._current_page = new
            text = self.notebook.get_page(new)
            self.editor.load_markdown(
                text, page_path=new,
                base_path=self.notebook.file_for(new).parent,
            )
            self._refresh_backlinks()
            self._update_status()
        elif self._current_page in modified:
            # Source of the currently-open page was rewritten on disk.
            text = self.notebook.get_page(self._current_page)
            self.editor.load_markdown(
                text, page_path=self._current_page,
                base_path=self.notebook.file_for(self._current_page).parent,
            )
            self._refresh_backlinks()
            self._update_status()
        return modified

    def _on_page_moved(self, old: str, new: str) -> None:
        if self._current_page == old:
            self._current_page = new
            if self.notebook and self.notebook.exists(new):
                text = self.notebook.get_page(new)
                self.editor.load_markdown(
                    text, page_path=new,
                    base_path=self.notebook.file_for(new).parent,
                )
        if self.model:
            idx = self.model.index_for_page(new)
            if idx.isValid():
                self.tree.setCurrentIndex(idx)
        self._refresh_backlinks()
        self._update_status()

    # ---- tree context menu & page ops ----

    def _on_tree_context_menu(self, pos) -> None:
        if self.notebook is None or self.model is None:
            return
        idx = self.tree.indexAt(pos)
        ref = self.model.page_for_index(idx) if idx.isValid() else None
        menu = QMenu(self.tree)
        act_new = menu.addAction("New child page...")
        act_rename = menu.addAction("Rename...")
        act_move = menu.addAction("Move to...")
        act_copy = menu.addAction("Copy to...")
        menu.addSeparator()
        act_props = menu.addAction("Properties...")
        menu.addSeparator()
        act_reveal = menu.addAction("Reveal in file manager")
        act_terminal = menu.addAction("Open terminal here")
        menu.addSeparator()
        act_delete = menu.addAction("Delete...")
        if ref is None:
            act_rename.setEnabled(False)
            act_move.setEnabled(False)
            act_copy.setEnabled(False)
            act_delete.setEnabled(False)
            act_props.setEnabled(False)
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_new:
            self._new_child_page(ref)
        elif chosen is act_rename and ref:
            self._rename_page_dialog(ref.path)
        elif chosen is act_move and ref:
            self._move_page_dialog(ref.path)
        elif chosen is act_copy and ref:
            self._copy_page_dialog(ref.path)
        elif chosen is act_props and ref:
            self.show_page_properties(ref.path)
        elif chosen is act_reveal:
            self.reveal_in_file_manager(ref.path if ref else None)
        elif chosen is act_terminal:
            self.open_terminal_here(ref.path if ref else None)
        elif chosen is act_delete and ref:
            self._delete_page_dialog(ref.path)

    def _new_child_page(self, parent: PageRef | None) -> None:
        if self.notebook is None:
            return
        prefix = (parent.path + ":") if parent else ""
        name, ok = QInputDialog.getText(
            self, "New Page", f"New page under {prefix or '(root)'}:"
        )
        if not ok or not name.strip():
            return
        leaf = name.strip()
        if ":" in leaf or "/" in leaf:
            QMessageBox.warning(self, "Invalid name", "Name cannot contain : or /")
            return
        full = prefix + leaf
        if self.notebook.exists(full):
            QMessageBox.warning(self, "Exists", f"Page {full} already exists")
            return
        self.notebook.create_page(full, f"# {leaf}\n")
        if self.index:
            self.index.update_page(full)
        if self.model:
            self.model.refresh()
            self.tree.setModel(self.model)
        self.load_page(full)

    def _rename_page_dialog(self, old: str) -> None:
        leaf = old.rsplit(":", 1)[-1]
        parent = old.rsplit(":", 1)[0] if ":" in old else ""
        new_leaf, ok = QInputDialog.getText(
            self, "Rename Page", f"New name for {old}:", text=leaf,
        )
        if not ok or not new_leaf.strip():
            return
        new_leaf = new_leaf.strip()
        if ":" in new_leaf or "/" in new_leaf:
            QMessageBox.warning(self, "Invalid name", "Name cannot contain : or /")
            return
        new = (parent + ":" + new_leaf) if parent else new_leaf
        if new == old:
            return
        if self.notebook and self.notebook.exists(new):
            QMessageBox.warning(self, "Exists", f"Page {new} already exists")
            return
        self.rename_page(old, new)

    def _move_page_dialog(self, old: str) -> None:
        leaf = old.rsplit(":", 1)[-1]
        new_parent, ok = QInputDialog.getText(
            self, "Move Page",
            f"Move {old} under (colon-path, blank for root):",
        )
        if not ok:
            return
        new_parent = new_parent.strip().strip(":")
        new = (new_parent + ":" + leaf) if new_parent else leaf
        if new == old:
            return
        if self.notebook and self.notebook.exists(new):
            QMessageBox.warning(self, "Exists", f"Page {new} already exists")
            return
        self.move_page(old, new)

    def _copy_page_dialog(self, src: str) -> None:
        dst, ok = QInputDialog.getText(
            self, "Copy Page", f"Copy {src} to (colon-path):",
            text=src + "-copy",
        )
        if not ok or not dst.strip():
            return
        dst = dst.strip().strip(":")
        if self.notebook and self.notebook.exists(dst):
            QMessageBox.warning(self, "Exists", f"Page {dst} already exists")
            return
        self.copy_page(src, dst)

    def _delete_page_dialog(self, page: str) -> None:
        if self.index is None or self.notebook is None:
            return
        inbound = len(self.index.backlinks(page))
        msg = f"Delete page {page}?"
        if inbound:
            msg += f"\n\n{inbound} other page(s) link here; those links will break."
        reply = QMessageBox.question(
            self, "Delete Page", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.delete_page(page)

    def move_page(self, old: str, new: str) -> list[str]:
        """Move a page (like rename but to different parent)."""
        return self.rename_page(old, new)

    def copy_page(self, src: str, dst: str) -> None:
        if self.notebook is None or self.index is None:
            return
        self.index.copy_page(src, dst)
        if self.model:
            self.model.refresh()
            self.tree.setModel(self.model)
        self.load_page(dst)

    def delete_page(self, page: str) -> None:
        if self.notebook is None or self.index is None:
            return
        # Land any queued async commit before the page is removed.
        self._drain_pending_commits()
        was_current = (self._current_page == page)
        self.index.delete_page_and_cleanup(page)
        if self.model:
            self.model.refresh()
            self.tree.setModel(self.model)
        if was_current:
            self._current_page = None
            self.editor.load_markdown("", page_path=None, base_path=None)
            first = next(iter(self.notebook.pages()), None)
            if first:
                self.load_page(first.path)
            else:
                self._refresh_backlinks()
                self._update_status()

    # ---- page properties ----

    def page_properties(self, page: str) -> dict:
        """Return a dict of page metadata: path, size, ctime, mtime, words,
        chars, inbound_links, tags."""
        from datetime import datetime
        if self.notebook is None:
            return {}
        f = self.notebook.file_for(page)
        text = ""
        try:
            text = self.notebook.get_page(page)
        except Exception:
            pass
        st = f.stat() if f.exists() else None
        from .index import extract_tags
        tags = extract_tags(text) if text else []
        info = {
            "path": page,
            "file": str(f),
            "size": st.st_size if st else 0,
            "ctime": datetime.fromtimestamp(st.st_ctime).isoformat(timespec="seconds") if st else "",
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds") if st else "",
            "words": len(text.split()),
            "chars": len(text),
            "inbound": len(self.index.backlinks(page)) if self.index else 0,
            "tags": tags,
        }
        return info

    def show_page_properties(self, page: str) -> None:
        info = self.page_properties(page)
        if not info:
            return
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Properties: {page}")
        form = QFormLayout(dlg)
        form.addRow("Path:", QLabel(info["path"]))
        form.addRow("File:", QLabel(info["file"]))
        form.addRow("Size:", QLabel(f"{info['size']} bytes"))
        form.addRow("Created:", QLabel(info["ctime"]))
        form.addRow("Modified:", QLabel(info["mtime"]))
        form.addRow("Words:", QLabel(str(info["words"])))
        form.addRow("Characters:", QLabel(str(info["chars"])))
        form.addRow("Inbound links:", QLabel(str(info["inbound"])))
        tag_str = ", ".join(f"#{t}" for t in info["tags"]) or "(none)"
        form.addRow("Tags:", QLabel(tag_str))
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dlg)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        btns.button(QDialogButtonBox.StandardButton.Close).clicked.connect(dlg.accept)
        form.addRow(btns)
        dlg.exec()

    # ---- recent + bookmarks ----

    def _settings_key(self, suffix: str) -> str:
        nb_key = str(self.notebook.root) if self.notebook else ""
        return f"notebooks/{nb_key}/{suffix}"

    def _load_recent_and_bookmarks(self) -> None:
        if self.notebook is None:
            self._recent = []
            self._bookmarks = []
            return
        r = self._settings.value(self._settings_key("recent"), [], type=list) or []
        b = self._settings.value(self._settings_key("bookmarks"), [], type=list) or []
        self._recent = [str(x) for x in r]
        self._bookmarks = [str(x) for x in b]

    def _save_recent(self) -> None:
        if self.notebook is None:
            return
        self._settings.setValue(self._settings_key("recent"), self._recent)

    def _save_bookmarks(self) -> None:
        if self.notebook is None:
            return
        self._settings.setValue(self._settings_key("bookmarks"), self._bookmarks)

    def _push_recent(self, page: str) -> None:
        if not page:
            return
        if page in self._recent:
            self._recent.remove(page)
        self._recent.insert(0, page)
        del self._recent[20:]
        self._save_recent()

    def _toggle_bookmark_current(self) -> None:
        if self._current_page is None:
            return
        if self._current_page in self._bookmarks:
            self._bookmarks.remove(self._current_page)
        else:
            self._bookmarks.append(self._current_page)
        self._save_bookmarks()

    def _manage_bookmarks(self) -> None:
        from PyQt6.QtWidgets import QDialog, QListWidget, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("Manage Bookmarks")
        v = QVBoxLayout(dlg)
        lst = QListWidget(dlg)
        for b in self._bookmarks:
            lst.addItem(b)
        v.addWidget(lst)
        row = QHBoxLayout()
        btn_remove = QPushButton("Remove", dlg)
        btn_close = QPushButton("Close", dlg)
        row.addWidget(btn_remove)
        row.addStretch(1)
        row.addWidget(btn_close)
        v.addLayout(row)

        def do_remove():
            it = lst.currentItem()
            if it is None:
                return
            name = it.text()
            if name in self._bookmarks:
                self._bookmarks.remove(name)
            lst.takeItem(lst.currentRow())
            self._save_bookmarks()

        btn_remove.clicked.connect(do_remove)
        btn_close.clicked.connect(dlg.accept)
        dlg.exec()

    def _populate_go_menu(self) -> None:
        self.m_go.clear()
        self.m_go.addAction(self.act_bookmark)
        self.m_go.addAction(self.act_manage_bookmarks)
        self.m_go.addSeparator()
        if self._bookmarks:
            self.m_go.addSection("Bookmarks")
            for b in self._bookmarks:
                act = self.m_go.addAction(b)
                act.triggered.connect(
                    lambda _checked=False, p=b: self.load_page(p)
                )
        if self._recent:
            self.m_go.addSection("Recent")
            for r in self._recent[:10]:
                act = self.m_go.addAction(r)
                act.triggered.connect(
                    lambda _checked=False, p=r: self.load_page(p)
                )

    # ---- search ----

    def _open_search(self) -> None:
        self.search_dock.show()
        self.search_dock.raise_()
        self.search_dock_widget.focus_query()

    def _on_tag_clicked(self, tag: str) -> None:
        self.search_dock.show()
        self.search_dock.raise_()
        self.search_dock_widget.set_query(f"#{tag}")

    def _refresh_tags(self) -> None:
        if self.index is None:
            return
        self.tags_dock_widget.refresh(self.index.tags())
        self._refresh_completion_sources()

    def _refresh_completion_sources(self) -> None:
        if self.index is None:
            return
        pages = self.index.all_pages()
        tags = [t for t, _ in self.index.tags()]
        self.editor.set_completion_sources(pages, tags)

    def _on_search_hit_activated(self, page: str, line: int) -> None:
        if self.notebook is None or not self.notebook.exists(page):
            return
        self.load_page(page)
        if line > 0:
            self._jump_to_line(line)
        needle = self.search_dock_widget.input.text().strip()
        if needle:
            self._highlight_all_occurrences(needle)

    def _highlight_all_occurrences(self, needle: str) -> None:
        from PyQt6.QtGui import QColor, QTextCharFormat
        from PyQt6.QtWidgets import QTextEdit
        doc = self.editor.document()
        selections: list[QTextEdit.ExtraSelection] = []
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#fff48a"))
        cursor = QTextCursor(doc)
        while True:
            cursor = doc.find(needle, cursor)
            if cursor.isNull():
                break
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = fmt
            selections.append(sel)
        self.editor.setExtraSelections(selections)

    def _clear_search_highlights(self) -> None:
        self.editor.setExtraSelections([])

    def _jump_to_line(self, line_no: int) -> None:
        doc = self.editor.document()
        block = doc.findBlockByNumber(max(0, line_no - 1))
        if not block.isValid():
            return
        cur = self.editor.textCursor()
        cur.setPosition(block.position())
        self.editor.setTextCursor(cur)
        self.editor.ensureCursorVisible()

    # ---- page history ----

    def _open_page_history(self) -> None:
        if self.notebook is None or self._current_page is None:
            return
        from . import versioning
        if not versioning.is_repo(self.notebook.root):
            QMessageBox.information(
                self, "No History",
                "Versioning is not enabled for this notebook (no git repo).",
            )
            return
        page_file = self.notebook.file_for(self._current_page)
        rel = str(page_file.relative_to(self.notebook.root))
        from .history_viewer import HistoryViewer
        dlg = HistoryViewer(
            self.notebook.root,
            self._current_page,
            rel,
            self.editor.markdown(),
            on_restore=self._restore_page_text,
            parent=self,
        )
        dlg.exec()

    def _on_conflict_found(self, _cf) -> None:
        self._refresh_conflict_badge()

    def _refresh_conflict_badge(self) -> None:
        if not hasattr(self, "_conflict_watcher"):
            return
        n = len(self._conflict_watcher.current())
        if n == 0:
            self._conflict_badge.hide()
        else:
            self._conflict_badge.setText(f"\u26a0 {n} conflict" + ("s" if n > 1 else ""))
            self._conflict_badge.show()

    def _open_conflict_resolver(self) -> None:
        if self.notebook is None:
            return
        from .plugins.builtin.syncthing_resolver import ResolverDialog
        dlg = ResolverDialog(self.notebook.root, parent=self)
        dlg.exec()
        self._refresh_conflict_badge()

    def _open_snapshots(self) -> None:
        if self.notebook is None or self._current_page is None:
            return
        page_file = self.notebook.file_for(self._current_page)
        from .snapshots_dialog import SnapshotsDialog
        dlg = SnapshotsDialog(
            self.notebook.root, page_file,
            self.editor.markdown(),
            on_restore=lambda: self.load_page(self._current_page),
            parent=self,
        )
        dlg.exec()

    def _restore_page_text(self, text: str) -> None:
        if self.notebook is None or self._current_page is None:
            return
        self.notebook.save_page(self._current_page, text)
        if self.index:
            self.index.update_page(self._current_page, text)
        self._maybe_versioning_commit(self._current_page)
        self.load_page(self._current_page)

    # ---- outline mode ----

    def _set_outline_mode(self, mode: str) -> None:
        """Switch between 'tree' (PageTreeModel) and 'recent' (flat by mtime)."""
        if self.notebook is None or self.model is None:
            return
        if mode == "recent":
            from PyQt6.QtGui import QStandardItem, QStandardItemModel
            recent_model = QStandardItemModel(self)
            pages = list(self.notebook.pages())
            pages.sort(
                key=lambda r: self.notebook.file_for(r.path).stat().st_mtime,
                reverse=True,
            )
            for p in pages:
                it = QStandardItem(p.path)
                it.setEditable(False)
                it.setData(p.path, 0x0100)
                recent_model.appendRow(it)
            self.tree.setModel(recent_model)
            self._outline_mode = "recent"
        else:
            self.tree.setModel(self.model)
            self._outline_mode = "tree"

    def outline_mode(self) -> str:
        return getattr(self, "_outline_mode", "tree")

    # ---- custom shortcuts ----

    def _all_named_actions(self) -> list[tuple[str, QAction]]:
        out: list[tuple[str, QAction]] = []
        for name, val in vars(self).items():
            if name.startswith("act_") and isinstance(val, QAction):
                label = val.text().replace("&", "") or name
                out.append((label, val))
        out.sort(key=lambda t: t[0].lower())
        return out

    def apply_custom_shortcuts(self) -> None:
        """Load shortcut overrides from QSettings and apply to actions."""
        raw = self._settings.value("shortcuts", {}, type=dict) or {}
        for label, action in self._all_named_actions():
            override = raw.get(label)
            if override:
                action.setShortcut(QKeySequence(str(override)))

    def set_action_shortcut(self, label: str, key_text: str) -> None:
        for lab, action in self._all_named_actions():
            if lab == label:
                action.setShortcut(QKeySequence(key_text))
                raw = self._settings.value("shortcuts", {}, type=dict) or {}
                raw = dict(raw)
                raw[label] = key_text
                self._settings.setValue("shortcuts", raw)
                return

    def _open_settings(self) -> None:
        from .settings_dialog import SettingsDialog
        SettingsDialog(self).exec()

    # ---- quick note ----

    def _open_quick_note(self) -> None:
        if self.notebook is None:
            return
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("Quick Note")
        dlg.resize(420, 300)
        v = QVBoxLayout(dlg)
        edit = QPlainTextEdit(dlg)
        edit.setPlaceholderText("Type a quick note... (Ctrl+Enter to save)")
        v.addWidget(edit, 1)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            text = edit.toPlainText().strip()
            if text:
                self.append_to_scratch(text)

    def append_to_scratch(self, text: str) -> str:
        """Append `text` to the `Scratch` page with a timestamp header.
        Creates the page if absent. Returns the page path."""
        from datetime import datetime
        if self.notebook is None:
            return ""
        page = "Scratch"
        if not self.notebook.exists(page):
            self.notebook.create_page(page, "# Scratch\n\n")
        existing = self.notebook.get_page(page)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        appended = existing.rstrip() + f"\n\n## {ts}\n\n{text}\n"
        self.notebook.save_page(page, appended)
        if self.index:
            self.index.update_page(page, appended)
        if self.model:
            self.model.refresh()
            self.tree.setModel(self.model)
        return page

    # ---- quick switcher ----

    def _open_quick_switcher(self) -> None:
        if self.index is None:
            return
        from .quickswitcher import QuickSwitcher
        pages = self.index.all_pages()
        dlg = QuickSwitcher(pages, self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            chosen = dlg.chosen()
            if chosen:
                self.load_page(chosen)

    def _open_command_palette(self) -> None:
        from .command_palette import CommandPalette, collect_actions
        actions = collect_actions(self.menuBar())
        if not actions:
            return
        dlg = CommandPalette(actions, self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            a = dlg.chosen()
            if a is not None and a.isEnabled():
                a.trigger()

    # ---- find ----

    def _open_find(self) -> None:
        self.find_bar.open_bar()

    def _find_next(self) -> None:
        if not self.find_bar.isVisible():
            self.find_bar.open_bar()
        else:
            self.find_bar.find_next()

    def _find_prev(self) -> None:
        if not self.find_bar.isVisible():
            self.find_bar.open_bar()
        else:
            self.find_bar.find_prev()

    # ---- insert helpers ----

    def _insert_date(self) -> None:
        from datetime import date
        self.editor.insert_text_at_cursor(date.today().isoformat())

    def _insert_time(self) -> None:
        from datetime import datetime
        self.editor.insert_text_at_cursor(datetime.now().strftime("%H:%M"))

    def _insert_datetime(self) -> None:
        from datetime import datetime
        self.editor.insert_text_at_cursor(datetime.now().strftime("%Y-%m-%d %H:%M"))

    def _insert_hr(self) -> None:
        self.editor.insert_horizontal_rule()

    def _insert_symbol_dialog(self) -> None:
        from PyQt6.QtWidgets import QDialog, QGridLayout, QPushButton, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("Insert Symbol")
        v = QVBoxLayout(dlg)
        grid = QGridLayout()
        symbols = [
            "→", "←", "↑", "↓", "↔", "⇒", "⇐", "⇔",
            "€", "£", "¥", "¢", "$", "₹", "₽", "¤",
            "×", "÷", "±", "≈", "≠", "≤", "≥", "∞",
            "°", "•", "…", "§", "¶", "†", "‡", "©",
        ]
        for i, sym in enumerate(symbols):
            btn = QPushButton(sym, dlg)
            btn.clicked.connect(
                lambda _checked=False, s=sym: (
                    self.editor.insert_text_at_cursor(s),
                    dlg.accept(),
                )
            )
            grid.addWidget(btn, i // 8, i % 8)
        v.addLayout(grid)
        dlg.exec()

    # ---- image insertion ----

    def _insert_image_dialog(self) -> None:
        if self.notebook is None or self._current_page is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Insert Image",
            "",
            "Images (*.png *.jpg *.jpeg *.gif *.svg *.webp)",
        )
        if not path:
            return
        self._insert_image_from_path(Path(path))

    def _on_image_dropped(self, src_path: str) -> None:
        self._insert_image_from_path(Path(src_path))

    def _on_image_pasted(self, image) -> None:
        from datetime import datetime

        from PyQt6.QtGui import QImage
        if self.notebook is None or self._current_page is None:
            return
        if not isinstance(image, QImage) or image.isNull():
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        rel = self._copy_image_bytes(image, f"pasted-{stamp}.png")
        if rel:
            self._insert_image_markdown(rel)

    def _insert_image_from_path(self, src: Path) -> None:
        if self.notebook is None or self._current_page is None:
            return
        if not src.is_file():
            return
        rel = self._copy_image_file(src)
        if rel:
            self._insert_image_markdown(rel)

    def _resources_dir_for_current(self) -> Path:
        """Directory that holds images for the currently-open page."""
        assert self.notebook is not None and self._current_page is not None
        page_file = self.notebook.file_for(self._current_page)
        return page_file.parent / "_resources"

    def _copy_image_file(self, src: Path) -> str | None:
        resdir = self._resources_dir_for_current()
        resdir.mkdir(parents=True, exist_ok=True)
        dst = _unique_path(resdir / src.name)
        from . import safe_save
        safe_save.atomic_write(dst, src.read_bytes())
        return f"_resources/{dst.name}"

    def _copy_image_bytes(self, image, default_name: str) -> str | None:
        resdir = self._resources_dir_for_current()
        resdir.mkdir(parents=True, exist_ok=True)
        dst = _unique_path(resdir / default_name)
        image.save(str(dst), "PNG")
        return f"_resources/{dst.name}"

    def _insert_attachment_dialog(self) -> None:
        if self.notebook is None or self._current_page is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Insert Attachment", "", "All Files (*)")
        if not path:
            return
        self._insert_attachment_from_path(Path(path))

    def _on_file_dropped(self, src_path: str) -> None:
        self._insert_attachment_from_path(Path(src_path))

    def _insert_attachment_from_path(self, src: Path) -> None:
        if self.notebook is None or self._current_page is None:
            return
        if not src.is_file():
            return
        resdir = self._resources_dir_for_current()
        resdir.mkdir(parents=True, exist_ok=True)
        dst = _unique_path(resdir / src.name)
        from . import safe_save
        safe_save.atomic_write(dst, src.read_bytes())
        rel = f"_resources/{dst.name}"
        link_md = f"[{dst.name}]({rel})"
        self.editor.insert_text_at_cursor(link_md)

    def _insert_image_markdown(self, rel_path: str) -> None:
        from pathlib import PurePosixPath
        alt = PurePosixPath(rel_path).stem
        assert self.notebook is not None and self._current_page is not None
        page_dir = self.notebook.file_for(self._current_page).parent
        abs_path = (page_dir / rel_path).resolve()
        self.editor.insert_image(rel_path, alt, str(abs_path))

    def _populate_templates_menu(self) -> None:
        self.m_new_from_template.clear()
        if self.notebook is None:
            act = self.m_new_from_template.addAction("(no notebook open)")
            act.setEnabled(False)
            return
        from .templates import list_templates
        for name in list_templates(self.notebook):
            act = self.m_new_from_template.addAction(name)
            act.triggered.connect(
                lambda _checked=False, n=name: self._new_page(template_name=n)
            )

    # ---- link map ----

    def _toggle_linkmap_dock(self, checked: bool) -> None:
        if checked:
            self.linkmap_dock.show()
            self.linkmap_dock.raise_()
            self._refresh_linkmap()
        else:
            self.linkmap_dock.hide()

    def _refresh_linkmap(self) -> None:
        if self.index is None or self._current_page is None:
            self.linkmap_widget.build(None, [], [])
            return
        hops = self.linkmap_widget.hops()
        if hops <= 1:
            fwd = self.index.forward_links(self._current_page)
            back = self.index.backlinks(self._current_page)
            self.linkmap_widget.build(self._current_page, fwd, back)
            return
        # Multi-hop BFS using forward + backward links.
        seen = {self._current_page}
        frontier = {self._current_page}
        edges: list[tuple[str, str]] = []
        for _ in range(hops):
            next_frontier: set[str] = set()
            for p in frontier:
                for d in self.index.forward_links(p):
                    edges.append((p, d))
                    if d not in seen:
                        next_frontier.add(d)
                for s in self.index.backlinks(p):
                    edges.append((s, p))
                    if s not in seen:
                        next_frontier.add(s)
            seen |= next_frontier
            frontier = next_frontier
        self.linkmap_widget.build_multihop(self._current_page, edges)

    # ---- spell check ----

    def _toggle_spell_check(self, checked: bool) -> None:
        from .spell import HAS_ENCHANT, SpellHighlighter
        if not HAS_ENCHANT:
            self.act_toggle_spell.setChecked(False)
            return
        if checked:
            if not hasattr(self, "_spell_highlighter") or self._spell_highlighter is None:
                pers = None
                if self.notebook is not None:
                    pers = self.notebook.root / ".qnotebook" / "dictionary.txt"
                self._spell_highlighter = SpellHighlighter(
                    self.editor.document(), personal_dict_path=pers,
                )
                self.editor.attach_spell_highlighter(self._spell_highlighter)
        else:
            if getattr(self, "_spell_highlighter", None) is not None:
                self._spell_highlighter.setDocument(None)
                self._spell_highlighter = None
                self.editor.attach_spell_highlighter(None)
        self._settings.setValue("spell_enabled", bool(checked))

    # ---- dark mode ----

    def _toggle_dark_mode(self, on: bool) -> None:
        from PyQt6.QtGui import QColor, QPalette
        from PyQt6.QtWidgets import QApplication
        self._settings.setValue("dark_mode", bool(on))
        app = QApplication.instance()
        if app is None:
            return
        if on:
            pal = QPalette()
            pal.setColor(QPalette.ColorRole.Window, QColor("#2b2b2b"))
            pal.setColor(QPalette.ColorRole.WindowText, QColor("#d4d4d4"))
            pal.setColor(QPalette.ColorRole.Base, QColor("#1e1e1e"))
            pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#2d2d2d"))
            pal.setColor(QPalette.ColorRole.Text, QColor("#d4d4d4"))
            pal.setColor(QPalette.ColorRole.Button, QColor("#3a3a3a"))
            pal.setColor(QPalette.ColorRole.ButtonText, QColor("#d4d4d4"))
            pal.setColor(QPalette.ColorRole.Highlight, QColor("#264f78"))
            pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
            pal.setColor(QPalette.ColorRole.Link, QColor("#9cdcfe"))
            app.setStyle("Fusion")
            app.setPalette(pal)
            self.editor.setStyleSheet(
                "QTextEdit { background: #1e1e1e; color: #d4d4d4; "
                "selection-background-color: #264f78; }"
            )
        else:
            app.setPalette(QPalette())
            self.editor.setStyleSheet("")

    # ---- toc ----

    def _toggle_toc_dock(self, checked: bool) -> None:
        if checked:
            self.toc_dock.show()
            self.toc_dock.raise_()
            self._refresh_toc()
        else:
            self.toc_dock.hide()

    def _refresh_toc(self) -> None:
        self.toc_widget.refresh(self.editor.markdown())

    def _jump_to_line_from_toc(self, line: int) -> None:
        self._jump_to_line(line + 1)
        self.editor.setFocus()

    # ---- calendar / journal ----

    def _toggle_calendar_dock(self, checked: bool) -> None:
        if checked:
            self.calendar_dock.show()
            self.calendar_dock.raise_()
            self.calendar_widget.refresh_highlights()
        else:
            self.calendar_dock.hide()

    def _on_calendar_date_activated(self, d) -> None:
        if self.notebook is None:
            return
        from .journal import journal_page_for_date
        page = journal_page_for_date(d)
        if not self.notebook.exists(page):
            self.create_page_from_template(page, "Daily Journal")
        else:
            self.load_page(page)
        self.calendar_widget.refresh_highlights()

    # ---- recent notebooks ----

    def _recent_notebooks(self) -> list[str]:
        raw = self._settings.value("recent_notebooks", [], type=list) or []
        return [str(x) for x in raw]

    def _push_recent_notebook(self, path: str) -> None:
        existing = self._recent_notebooks()
        if path in existing:
            existing.remove(path)
        existing.insert(0, path)
        del existing[5:]
        self._settings.setValue("recent_notebooks", existing)

    def _populate_send_to_menu(self) -> None:
        """Rebuild the qdistro Send-To submenu from the broker.

        Pulls the current page's text (or the selection if any) as the
        payload, then asks the broker for every registered receiver
        and offers one menu entry per peer. Same-silo deliveries take
        the broker's fast path; cross-silo trips the admin approval
        prompt.
        """
        self.m_send_to.clear()
        try:
            from qnotebook import qdistro_integration as _qi
        except ImportError:
            act = self.m_send_to.addAction("(qdistro SDK not available)")
            act.setEnabled(False)
            return
        payload = self._collect_send_to_payload()
        targets = _qi.send_to_targets(kind="text/plain")
        if not targets:
            act = self.m_send_to.addAction("(no receivers running)")
            act.setEnabled(False)
            return
        for row in targets:
            label = row["name"]
            silo = row.get("silo") or ""
            if silo:
                label = f"{label}  [{silo}]"
            act = self.m_send_to.addAction(label)
            if not payload:
                act.setEnabled(False)
                act.setToolTip("Open a page first")
            else:
                uid = int(row["uid"])
                svc = str(row["service"])
                act.triggered.connect(
                    lambda _checked=False, u=uid, s=svc, p=payload:
                        _qi.send_payload(u, s, p, kind="text/plain"))

    def _collect_send_to_payload(self) -> str:
        editor = getattr(self, "editor", None)
        if editor is None:
            return ""
        try:
            cur = editor.textCursor()
            sel = cur.selectedText()
            if sel:
                # QTextCursor returns U+2029 for paragraph breaks;
                # normalise to \n so receivers don't see funky chars.
                return str(sel).replace("\u2029", "\n").replace("\u2028", "\n")
            return str(editor.toPlainText())
        except Exception:
            return ""

    def _populate_recent_notebooks_menu(self) -> None:
        self.m_recent_notebooks.clear()
        entries = self._recent_notebooks()
        if not entries:
            act = self.m_recent_notebooks.addAction("(none)")
            act.setEnabled(False)
            return
        for p in entries:
            act = self.m_recent_notebooks.addAction(p)
            act.triggered.connect(
                lambda _checked=False, path=p: self.open_notebook(path)
            )

    # ---- export ----

    def _export_page_html(self) -> None:
        if self.notebook is None or self._current_page is None:
            return
        from .export import export_page_html, load_notebook_css
        default_name = self._current_page.replace(":", "_") + ".html"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Page as HTML", default_name, "HTML Files (*.html)"
        )
        if not path:
            return
        export_page_html(
            self.notebook, self._current_page, Path(path),
            css=load_notebook_css(self.notebook),
        )

    def _export_notebook_html(self) -> None:
        if self.notebook is None:
            return
        from .export import export_notebook_html, load_notebook_css
        out = QFileDialog.getExistingDirectory(self, "Export Notebook to Directory")
        if not out:
            return
        export_notebook_html(
            self.notebook, Path(out),
            css=load_notebook_css(self.notebook),
        )

    def _edit_export_css(self) -> None:
        if self.notebook is None:
            return
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

        from .export import load_notebook_css, save_notebook_css
        dlg = QDialog(self)
        dlg.setWindowTitle("Export CSS")
        dlg.resize(720, 540)
        v = QVBoxLayout(dlg)
        edit = QPlainTextEdit(dlg)
        edit.setPlainText(load_notebook_css(self.notebook))
        v.addWidget(edit)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dlg,
        )
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            save_notebook_css(self.notebook, edit.toPlainText())

    def _export_page_pdf(self) -> None:
        if self.notebook is None or self._current_page is None:
            return
        from .export import export_page_pdf
        default_name = self._current_page.replace(":", "_") + ".pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Page as PDF", default_name, "PDF Files (*.pdf)"
        )
        if not path:
            return
        export_page_pdf(self.notebook, self._current_page, Path(path))

    def _print_current_page(self) -> None:
        from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
        if self.notebook is None or self._current_page is None:
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dlg = QPrintDialog(printer, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        self.editor.document().print(printer)

    # ---- misc ----

    def _refresh_backlinks(self) -> None:
        self.backlinks_list.clear()
        if self.index is None or self._current_page is None:
            return
        for src in self.index.backlinks(self._current_page):
            self.backlinks_list.addItem(src)

    def _on_backlink_activated(self, item) -> None:
        self.load_page(item.text())

    def _on_dirty_changed(self, dirty: bool) -> None:
        self._update_status()
        self._update_title()

    def _auto_save(self) -> None:
        if self._current_page and self.editor.is_dirty():
            self._save_current()

    def _update_title(self) -> None:
        name = self.notebook.root.name if self.notebook else ""
        prefix = "*" if self.editor.is_dirty() else ""
        parts: list[str] = []
        if self._current_page:
            parts.append(self._current_page)
        if name:
            parts.append(name)
        parts.append("qnotebook")
        self.setWindowTitle(prefix + " — ".join(parts))

    def _update_status(self) -> None:
        parts = []
        text = self.editor.toPlainText() if hasattr(self.editor, "toPlainText") else ""
        words = len(text.split())
        chars = len(text)
        mins = max(1, round(words / 200)) if words else 0
        if self.notebook is not None:
            parts.append(self.notebook.root.name)
        if self.index is not None:
            total = len(self.index.all_pages())
            parts.append(f"{total} page" + ("" if total == 1 else "s"))
        if words:
            parts.append(f"{words} words")
        if chars:
            parts.append(f"{chars} chars")
        if mins:
            parts.append(f"~{mins} min")
        if self._current_page:
            parts.append(self._current_page)
        if self.editor.is_dirty():
            parts.append("[modified]")
        self._status_label.setText("  |  ".join(parts))

    def _maybe_save_dirty(self) -> bool:
        """Hook for tests to bypass unsaved-changes prompt."""
        return True

    def closeEvent(self, event) -> None:
        if self.editor.is_dirty():
            if not self._maybe_save_dirty():
                event.ignore()
                return
        # Save session snapshot.
        if self.notebook is not None and bool(
            self._settings.value("session_restore_enabled", True, type=bool)
        ):
            try:
                from . import session as _session
                _session.save(self.notebook.root, _session.capture(self))
            except Exception:
                pass
        # Drain any in-flight async git commits so the last (auto)save isn't
        # lost when the window closes. Wait unbounded: these operations are
        # short on a normal repo, and dropping the final save/version-history
        # update is worse than a brief close delay.
        try:
            self._drain_pending_save_merges(-1)
            from . import versioning as _versioning
            _versioning.wait_for_pending_commits(-1)
        except Exception:
            pass
        if self.index:
            self.index.close()
        # Release the per-notebook lock (only if we own it — read-only sessions
        # should not remove another process's lock).
        if self.notebook is not None and not getattr(self, "_read_only", False):
            try:
                from . import locks as _locks
                _locks.remove(self.notebook.root)
            except Exception:
                pass
        super().closeEvent(event)
