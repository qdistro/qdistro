"""Settings dialog for qnotebook.

Left pane: search + category list.  Right pane: stacked category pages.
General and Shortcuts categories only, for now.

Storage:
  - Per-notebook settings: routed through `nb_settings` (.qnotebook/settings.json).
  - Global settings + shortcuts: routed through QSettings("qnotebook", "qnotebook").
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QModelIndex, QSettings, Qt
from PyQt6.QtGui import QAction, QBrush, QColor, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import nb_settings


class _KeySequenceDelegate(QStyledItemDelegate):
    """Item delegate that edits a cell with a QKeySequenceEdit.

    Stores the resulting sequence back as a portable string
    (e.g. "Ctrl+Shift+P").
    """

    def createEditor(self, parent, option, index):  # noqa: N802
        editor = QKeySequenceEdit(parent)
        return editor

    def setEditorData(self, editor: QKeySequenceEdit, index: QModelIndex) -> None:  # noqa: N802
        text = index.data(Qt.ItemDataRole.EditRole) or ""
        editor.setKeySequence(QKeySequence(str(text)))

    def setModelData(self, editor: QKeySequenceEdit, model, index: QModelIndex) -> None:  # noqa: N802
        model.setData(index, editor.keySequence().toString(), Qt.ItemDataRole.EditRole)


class SettingsDialog(QDialog):
    """qnotebook settings dialog (General + Shortcuts)."""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window
        self._settings: QSettings = window._settings
        self._nb_root: Path | None = (
            window.notebook.root if window.notebook is not None else None
        )

        self.setWindowTitle("qnotebook Settings")
        self.resize(720, 520)
        self.setMinimumSize(600, 420)

        outer = QVBoxLayout(self)

        body = QHBoxLayout()
        body.setSpacing(0)
        outer.addLayout(body, 1)

        # Left pane
        left = QWidget(self)
        left.setFixedWidth(200)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        self._search = QLineEdit(left)
        self._search.setPlaceholderText("Search")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_categories)
        left_layout.addWidget(self._search)

        self._category_list = QListWidget(left)
        self._category_list.setFrameShape(QFrame.Shape.NoFrame)
        left_layout.addWidget(self._category_list, 1)

        body.addWidget(left)

        sep = QFrame(self)
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        body.addWidget(sep)

        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)
        right_layout.setSpacing(8)

        from PyQt6.QtWidgets import QLabel
        self._title = QLabel(right)
        title_font = self._title.font()
        title_font.setPointSize(max(title_font.pointSize() + 4, 14))
        title_font.setBold(True)
        self._title.setFont(title_font)
        right_layout.addWidget(self._title)

        self._stack = QStackedWidget(right)
        right_layout.addWidget(self._stack, 1)
        body.addWidget(right, 1)

        self._add_category("General", self._build_general_page())
        self._add_category("Shortcuts", self._build_shortcuts_page())

        self._category_list.currentRowChanged.connect(self._on_category_changed)
        # Restore last-selected category if it still exists.
        last = str(self._settings.value("settings_last_category", "", type=str) or "")
        initial = 0
        if last:
            for i in range(self._category_list.count()):
                if self._category_list.item(i).text() == last:
                    initial = i
                    break
        self._category_list.setCurrentRow(initial)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._apply_and_close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        outer.addWidget(buttons)

        self._load()

    # ------------------------------------------------------------------ layout

    def _add_category(self, label: str, page: QWidget) -> None:
        self._category_list.addItem(QListWidgetItem(label))
        self._stack.addWidget(page)

    def _on_category_changed(self, index: int) -> None:
        if index < 0:
            return
        self._stack.setCurrentIndex(index)
        item = self._category_list.item(index)
        if item is not None:
            self._title.setText(item.text())
            # Persist immediately — purely UI navigation, doesn't need Apply.
            self._settings.setValue("settings_last_category", item.text())

    def _filter_categories(self, text: str) -> None:
        needle = text.strip().lower()
        first_visible = -1
        for i in range(self._category_list.count()):
            item = self._category_list.item(i)
            visible = (not needle) or (needle in item.text().lower())
            item.setHidden(not visible)
            if visible and first_visible < 0:
                first_visible = i
        cur = self._category_list.currentRow()
        if cur < 0 or self._category_list.item(cur).isHidden():
            if first_visible >= 0:
                self._category_list.setCurrentRow(first_visible)

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        return scroll

    # --------------------------------------------------------- General page

    def _build_general_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        # Per-notebook group
        nb_group = QGroupBox("This notebook")
        nb_form = QFormLayout(nb_group)

        self._chk_versioning = QCheckBox("Enable version history (per-save git commits)")
        nb_form.addRow(self._chk_versioning)

        self._chk_strict_preserve = QCheckBox(
            "Strict preserve (refuse to rewrite regions that didn't round-trip cleanly)"
        )
        nb_form.addRow(self._chk_strict_preserve)

        if self._nb_root is None:
            self._chk_versioning.setEnabled(False)
            self._chk_strict_preserve.setEnabled(False)
            nb_form.addRow(
                _muted_label("(Open a notebook to edit per-notebook settings.)")
            )

        layout.addWidget(nb_group)

        # Global group
        global_group = QGroupBox("Application")
        global_form = QFormLayout(global_group)

        self._chk_autosave = QCheckBox("Autosave open page")
        global_form.addRow(self._chk_autosave)

        self._spin_autosave_secs = QSpinBox()
        self._spin_autosave_secs.setRange(1, 600)
        self._spin_autosave_secs.setSuffix(" s")
        global_form.addRow("Autosave interval:", self._spin_autosave_secs)

        self._chk_spell = QCheckBox("Spell check")
        global_form.addRow(self._chk_spell)

        self._chk_dark = QCheckBox("Dark mode")
        global_form.addRow(self._chk_dark)

        self._chk_session_restore = QCheckBox("Restore last session on open")
        global_form.addRow(self._chk_session_restore)

        layout.addWidget(global_group)
        layout.addStretch()

        return self._wrap_scroll(page)

    # -------------------------------------------------------- Shortcuts page

    def _build_shortcuts_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        info = _muted_label(
            "Double-click a shortcut to record a new key combination. "
            "Right-click a row to clear it. Duplicates are highlighted in red."
        )
        layout.addWidget(info)

        actions = self._window._all_named_actions()
        self._shortcut_actions: list[tuple[str, QAction]] = actions
        # Defaults captured from the current QAction shortcuts at dialog open,
        # so "Reset to default" doesn't pull from a stale QSettings override.
        self._shortcut_defaults: dict[str, str] = {
            label: act.shortcut().toString() for label, act in actions
        }
        # label -> bool, mirrors _refresh_shortcut_conflicts().
        self._shortcut_conflicts: dict[str, bool] = {}

        self._shortcut_table = QTableWidget(len(actions), 2, page)
        self._shortcut_table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        self._shortcut_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._shortcut_table.verticalHeader().setVisible(False)
        self._shortcut_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._shortcut_table.setItemDelegateForColumn(1, _KeySequenceDelegate(self._shortcut_table))
        self._shortcut_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._shortcut_table.customContextMenuRequested.connect(self._on_shortcut_context_menu)

        for r, (label, act) in enumerate(actions):
            action_item = QTableWidgetItem(label)
            action_item.setFlags(action_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._shortcut_table.setItem(r, 0, action_item)
            self._shortcut_table.setItem(r, 1, QTableWidgetItem(act.shortcut().toString()))
        layout.addWidget(self._shortcut_table, 1)

        # Re-paint conflicts on every edit. itemChanged fires for the
        # programmatic setText() above too, which is fine — we just paint
        # the initial state.
        self._shortcut_table.itemChanged.connect(self._on_shortcut_item_changed)
        self._refresh_shortcut_conflicts()

        return page  # table fills the page; no scroll wrap needed

    def _on_shortcut_item_changed(self, item) -> None:
        if item.column() == 1:
            self._refresh_shortcut_conflicts()

    def _refresh_shortcut_conflicts(self) -> None:
        """Flag rows whose normalised shortcut collides with another row."""
        rows = self._shortcut_table.rowCount()
        by_seq: dict[str, list[int]] = {}
        for r in range(rows):
            cell = self._shortcut_table.item(r, 1)
            raw = cell.text().strip() if cell is not None else ""
            if not raw:
                continue
            normalised = QKeySequence(raw).toString()
            by_seq.setdefault(normalised, []).append(r)

        conflict_brush = QBrush(QColor("#cf6679"))
        for r in range(rows):
            label_item = self._shortcut_table.item(r, 0)
            shortcut_item = self._shortcut_table.item(r, 1)
            label = label_item.text() if label_item is not None else ""
            raw = shortcut_item.text().strip() if shortcut_item is not None else ""
            normalised = QKeySequence(raw).toString() if raw else ""
            peers = by_seq.get(normalised, [])
            is_conflict = len(peers) > 1
            self._shortcut_conflicts[label] = is_conflict
            for col in (0, 1):
                cell = self._shortcut_table.item(r, col)
                if cell is None:
                    continue
                if is_conflict:
                    cell.setForeground(conflict_brush)
                else:
                    cell.setData(Qt.ItemDataRole.ForegroundRole, None)
            if shortcut_item is not None:
                if is_conflict:
                    others = [
                        self._shortcut_table.item(p, 0).text()
                        for p in peers if p != r
                    ]
                    shortcut_item.setToolTip(
                        "Conflicts with: " + ", ".join(others)
                    )
                else:
                    shortcut_item.setToolTip("")

    def _on_shortcut_context_menu(self, pos) -> None:
        index = self._shortcut_table.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        label_item = self._shortcut_table.item(row, 0)
        if label_item is None:
            return
        label = label_item.text()
        menu = QMenu(self._shortcut_table)
        act_clear = menu.addAction("Clear shortcut")
        act_reset = menu.addAction("Reset to default")
        chosen = menu.exec(self._shortcut_table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_clear:
            self._shortcut_table.item(row, 1).setText("")
        elif chosen is act_reset:
            self._shortcut_table.item(row, 1).setText(self._shortcut_defaults.get(label, ""))

    # ---------------------------------------------------------------- load

    def _load(self) -> None:
        # Per-notebook
        if self._nb_root is not None:
            self._chk_versioning.setChecked(
                bool(nb_settings.get(self._nb_root, "versioning_enabled", True))
            )
            self._chk_strict_preserve.setChecked(
                bool(nb_settings.get(self._nb_root, "strict_preserve", True))
            )

        # Global
        autosave_ms = int(self._settings.value("autosave_ms", 30000, type=int))
        self._spin_autosave_secs.setValue(max(1, round(autosave_ms / 1000)))
        self._chk_autosave.setChecked(
            bool(self._settings.value("autosave_enabled", True, type=bool))
        )
        self._chk_spell.setChecked(
            bool(self._settings.value("spell_enabled", False, type=bool))
        )
        self._chk_dark.setChecked(
            bool(self._settings.value("dark_mode", False, type=bool))
        )
        self._chk_session_restore.setChecked(
            bool(self._settings.value("session_restore_enabled", True, type=bool))
        )

    # --------------------------------------------------------------- apply

    def _apply(self) -> None:
        # Per-notebook
        if self._nb_root is not None:
            versioning_enabled = self._chk_versioning.isChecked()
            nb_settings.set_value(
                self._nb_root, "versioning_enabled", versioning_enabled
            )
            nb_settings.set_value(
                self._nb_root, "strict_preserve", self._chk_strict_preserve.isChecked()
            )
            # Mirror to QSettings since other code paths read it there.
            self._settings.setValue("versioning_enabled", versioning_enabled)
            if versioning_enabled:
                try:
                    from . import versioning as _v
                    _v.init_repo(self._nb_root)
                except Exception:
                    pass

        # Global
        autosave_ms = int(self._spin_autosave_secs.value()) * 1000
        autosave_enabled = self._chk_autosave.isChecked()
        self._settings.setValue("autosave_ms", autosave_ms)
        self._settings.setValue("autosave_enabled", autosave_enabled)
        if getattr(self._window, "editor", None) is not None:
            self._window.editor.set_autosave_interval_ms(autosave_ms)
            self._window.editor.set_autosave_enabled(autosave_enabled)

        spell_on = self._chk_spell.isChecked()
        self._settings.setValue("spell_enabled", spell_on)
        if hasattr(self._window, "act_toggle_spell"):
            self._window.act_toggle_spell.setChecked(spell_on)

        dark_on = self._chk_dark.isChecked()
        self._settings.setValue("dark_mode", dark_on)
        if hasattr(self._window, "act_dark"):
            self._window.act_dark.setChecked(dark_on)

        self._settings.setValue(
            "session_restore_enabled", self._chk_session_restore.isChecked()
        )

        # Shortcuts
        for r, (label, _act) in enumerate(self._shortcut_actions):
            cell = self._shortcut_table.item(r, 1)
            key_text = cell.text().strip() if cell is not None else ""
            self._window.set_action_shortcut(label, key_text)

    def _apply_and_close(self) -> None:
        self._apply()
        self.accept()


def _muted_label(text: str):
    from PyQt6.QtWidgets import QLabel
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    # Slightly faded relative to body text, using the placeholder-text role so
    # it adapts to dark/light platform themes instead of hard-coding a colour.
    lbl.setStyleSheet("color: palette(placeholder-text);")
    return lbl
