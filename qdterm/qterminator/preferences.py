"""Preferences dialog for QTerminator."""

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QFontDatabase, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFontDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QStyledItemDelegate,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Action name -> human-readable category. Anything not listed falls into
# "Other". Categories themselves are rendered in this declared order.
SHORTCUT_CATEGORIES: list[tuple[str, list[str]]] = [
    ("File", ["new_tab", "new_window", "close_terminal", "quit"]),
    ("Edit", ["copy", "paste", "search", "reset", "reset_clear", "preferences"]),
    ("View & Zoom", [
        "split_horizontal", "split_vertical", "rotate_splits",
        "maximize_terminal", "full_screen",
        "zoom_in", "zoom_out", "zoom_normal", "toggle_scrollbar",
    ]),
    ("Terminal", [
        "edit_terminal_title", "edit_tab_title", "edit_window_title", "read_only",
    ]),
    ("Tabs", [
        "next_tab", "prev_tab", "cycle_next", "cycle_prev",
        "move_tab_left", "move_tab_right",
        "switch_to_tab_1", "switch_to_tab_2", "switch_to_tab_3",
        "switch_to_tab_4", "switch_to_tab_5", "switch_to_tab_6",
        "switch_to_tab_7", "switch_to_tab_8", "switch_to_tab_9",
    ]),
    ("Splits", [
        "navigate_left", "navigate_right", "navigate_up", "navigate_down",
        "resize_left", "resize_right", "resize_up", "resize_down",
    ]),
    ("Scrollback", ["scroll_page_up", "scroll_page_down"]),
    ("Profiles", ["next_profile", "prev_profile"]),
    ("Window", ["toggle_menubar"]),
]

from QTermWidget import QTermWidget  # noqa: E402

from qterminator.config import Config  # noqa: E402
from qterminator.translation import _ as tr  # noqa: E402


class _KeySequenceDelegate(QStyledItemDelegate):
    """Cell delegate that edits a shortcut via QKeySequenceEdit."""

    def createEditor(self, parent, option, index):  # noqa: N802
        return QKeySequenceEdit(parent)

    def setEditorData(self, editor: QKeySequenceEdit, index: QModelIndex) -> None:  # noqa: N802
        text = index.data(Qt.ItemDataRole.EditRole) or ""
        editor.setKeySequence(QKeySequence(str(text)))

    def setModelData(self, editor: QKeySequenceEdit, model, index: QModelIndex) -> None:  # noqa: N802
        model.setData(index, editor.keySequence().toString(), Qt.ItemDataRole.EditRole)


def _humanize_action_name(name: str) -> str:
    return name.replace("_", " ").title()


class _CategoryAdapter:
    """Read-only adapter exposing the QTabWidget API used by tests.

    The dialog uses a left-pane QListWidget + right-pane QStackedWidget; this
    wrapper lets callers query category labels and pages with tabText/widget/count.
    """

    def __init__(self, list_widget: QListWidget, stack: QStackedWidget):
        self._list = list_widget
        self._stack = stack

    def count(self) -> int:
        return self._stack.count()

    def tabText(self, index: int) -> str:
        item = self._list.item(index)
        return item.text() if item is not None else ""

    def widget(self, index: int) -> QWidget:
        return self._stack.widget(index)

    def setCurrentIndex(self, index: int) -> None:
        self._list.setCurrentRow(index)

    def currentIndex(self) -> int:
        return self._stack.currentIndex()


class PreferencesDialog(QDialog):
    """Application preferences dialog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("QTerminator Preferences"))
        self.resize(720, 520)
        self.setMinimumSize(600, 420)
        self._config = Config()

        outer = QVBoxLayout(self)

        body = QHBoxLayout()
        body.setSpacing(0)
        outer.addLayout(body, 1)

        # ---- Left pane: search + category list ----
        left = QWidget(self)
        left.setFixedWidth(200)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        self._search = QLineEdit(left)
        self._search.setPlaceholderText(tr("Search"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_categories)
        left_layout.addWidget(self._search)

        self._category_list = QListWidget(left)
        self._category_list.setFrameShape(QFrame.Shape.NoFrame)
        left_layout.addWidget(self._category_list, 1)

        body.addWidget(left)

        # Subtle vertical separator between panes
        sep = QFrame(self)
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        body.addWidget(sep)

        # ---- Right pane: title header + stacked category pages ----
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)
        right_layout.setSpacing(8)

        self._title = QLabel(right)
        title_font = self._title.font()
        title_font.setPointSize(max(title_font.pointSize() + 4, 14))
        title_font.setBold(True)
        self._title.setFont(title_font)
        right_layout.addWidget(self._title)

        self._stack = QStackedWidget(right)
        right_layout.addWidget(self._stack, 1)
        body.addWidget(right, 1)

        # Compat shim for existing tests / external callers.
        self._tab_widget = _CategoryAdapter(self._category_list, self._stack)

        # Build pages and add to both list & stack
        self._add_category(tr("Appearance"), self._build_appearance_page())
        self._add_category(tr("Behavior"), self._build_behavior_page())
        self._add_category(tr("Shortcuts"), self._build_shortcuts_page())

        self._category_list.currentRowChanged.connect(self._on_category_changed)
        # Restore last-selected category if it still exists.
        last = self._config.get("general", "last_prefs_category", default="")
        initial = 0
        if last:
            for i in range(self._category_list.count()):
                if self._category_list.item(i).text() == last:
                    initial = i
                    break
        self._category_list.setCurrentRow(initial)

        # ---- Footer buttons ----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._apply_and_close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        outer.addWidget(buttons)

        self._load_current_settings()

    # ------------------------------------------------------------------ helpers

    def _add_category(self, label: str, page: QWidget) -> None:
        item = QListWidgetItem(label)
        self._category_list.addItem(item)
        self._stack.addWidget(page)

    def _on_category_changed(self, index: int) -> None:
        if index < 0:
            return
        self._stack.setCurrentIndex(index)
        item = self._category_list.item(index)
        if item is not None:
            self._title.setText(item.text())
            # Remember the selection so the next open lands here. Persist
            # immediately rather than on Apply so cancelling still keeps the
            # navigational state (the underlying setting is purely UI).
            self._config.set("general", "last_prefs_category", item.text())
            self._config.save()

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

    # ------------------------------------------------------------------ pages

    def _build_appearance_page(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Font group
        font_group = QGroupBox(tr("Font"))
        font_layout = QFormLayout(font_group)

        family_row = QHBoxLayout()
        self._font_combo = QFontComboBox()
        self._font_combo.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self._font_combo.currentFontChanged.connect(self._update_font_preview)
        family_row.addWidget(self._font_combo, 1)
        browse_btn = QPushButton(tr("Browse..."))
        browse_btn.clicked.connect(self._browse_font)
        family_row.addWidget(browse_btn)
        font_layout.addRow(tr("Family:"), family_row)

        self._font_size = QSpinBox()
        self._font_size.setRange(6, 72)
        self._font_size.valueChanged.connect(self._update_font_preview)
        font_layout.addRow(tr("Size:"), self._font_size)

        self._font_ligatures = QCheckBox(tr("Enable ligatures (FiraCode, JetBrains Mono, Cascadia Code)"))
        self._font_ligatures.toggled.connect(self._update_font_preview)
        font_layout.addRow(self._font_ligatures)

        from PyQt6.QtWidgets import QGridLayout
        popular_grid = QGridLayout()
        popular_grid.setHorizontalSpacing(4)
        popular_grid.setVerticalSpacing(2)
        installed = set(QFontDatabase.families())
        any_popular = False
        col_count = 3
        row = col = 0
        for name in ["Fira Code", "JetBrains Mono", "Cascadia Code",
                     "Hack", "Source Code Pro", "DejaVu Sans Mono",
                     "Iosevka", "Inconsolata"]:
            if name in installed:
                btn = QPushButton(name)
                btn.setFlat(True)
                btn.clicked.connect(lambda checked=False, n=name: self._set_font_family(n))
                popular_grid.addWidget(btn, row, col)
                col += 1
                if col >= col_count:
                    col = 0
                    row += 1
                any_popular = True
        if any_popular:
            font_layout.addRow(tr("Popular:"), popular_grid)

        self._font_preview = QLabel()
        self._font_preview.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Sunken)
        self._font_preview.setMinimumHeight(80)
        self._font_preview.setStyleSheet("background: #1e1e1e; color: #d3d7cf; padding: 8px;")
        self._font_preview.setText(
            "abcdefghijklmnopqrstuvwxyz 0123456789\n"
            "=> != >= <= -> ::  // /* */\n"
            "$ git commit -m 'fix: bug #42'"
        )
        font_layout.addRow(tr("Preview:"), self._font_preview)

        layout.addWidget(font_group)

        # Colors group
        color_group = QGroupBox(tr("Colors"))
        color_layout = QFormLayout(color_group)

        self._color_scheme = QComboBox()
        available_schemes = sorted(QTermWidget.availableColorSchemes())
        for scheme in available_schemes:
            self._color_scheme.addItem(scheme)
        color_layout.addRow(tr("Color Scheme:"), self._color_scheme)

        self._dark_color_scheme = QComboBox()
        for scheme in available_schemes:
            self._dark_color_scheme.addItem(scheme)
        color_layout.addRow(tr("Dark color scheme:"), self._dark_color_scheme)

        self._light_color_scheme = QComboBox()
        for scheme in available_schemes:
            self._light_color_scheme.addItem(scheme)
        color_layout.addRow(tr("Light color scheme:"), self._light_color_scheme)

        self._opacity = QDoubleSpinBox()
        self._opacity.setRange(0.0, 1.0)
        self._opacity.setSingleStep(0.05)
        self._opacity.setDecimals(2)
        color_layout.addRow(tr("Opacity:"), self._opacity)

        layout.addWidget(color_group)

        # Cursor group
        cursor_group = QGroupBox(tr("Cursor"))
        cursor_layout = QFormLayout(cursor_group)

        self._cursor_shape = QComboBox()
        self._cursor_shape.addItems(["Block", "Underline", "IBeam"])
        cursor_layout.addRow(tr("Shape:"), self._cursor_shape)

        self._cursor_blink = QCheckBox(tr("Blinking cursor"))
        cursor_layout.addRow(self._cursor_blink)

        layout.addWidget(cursor_group)
        layout.addStretch()

        return self._wrap_scroll(widget)

    def _set_font_family(self, name):
        """Set font from a quick-select button."""
        self._font_combo.setCurrentFont(QFont(name))

    def _browse_font(self):
        """Open the full QFontDialog for advanced font selection."""
        current = QFont(
            self._font_combo.currentFont().family(),
            self._font_size.value(),
        )
        font, ok = QFontDialog.getFont(current, self, tr("Select Font"))
        if ok:
            self._font_combo.setCurrentFont(font)
            self._font_size.setValue(font.pointSize())

    def _update_font_preview(self):
        """Update the preview label to reflect current font selection."""
        font = QFont(
            self._font_combo.currentFont().family(),
            self._font_size.value(),
        )
        if self._font_ligatures.isChecked():
            font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
        self._font_preview.setFont(font)

    def _build_behavior_page(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Scrollback
        scroll_group = QGroupBox(tr("Scrollback"))
        scroll_layout = QFormLayout(scroll_group)

        self._scrollback = QSpinBox()
        self._scrollback.setRange(0, 1_000_000)
        self._scrollback.setSingleStep(1000)
        self._scrollback.setSpecialValueText(tr("Disabled"))
        scroll_layout.addRow(tr("Lines:"), self._scrollback)

        layout.addWidget(scroll_group)

        # Tabs
        tab_group = QGroupBox(tr("Tabs"))
        tab_layout = QFormLayout(tab_group)

        self._tab_position = QComboBox()
        self._tab_position.addItems(["Top", "Bottom", "Left", "Right"])
        tab_layout.addRow(tr("Tab position:"), self._tab_position)

        layout.addWidget(tab_group)

        # Window
        win_group = QGroupBox(tr("Window"))
        win_layout = QFormLayout(win_group)

        self._theme_mode = QComboBox()
        self._theme_mode.addItems(["System", "Dark", "Light"])
        win_layout.addRow(tr("Theme mode:"), self._theme_mode)

        self._confirm_close = QCheckBox(tr("Confirm before closing with running processes"))
        win_layout.addRow(self._confirm_close)

        self._show_menubar = QCheckBox(tr("Show menu bar"))
        win_layout.addRow(self._show_menubar)

        layout.addWidget(win_group)
        layout.addStretch()

        return self._wrap_scroll(widget)

    def _build_shortcuts_page(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel(tr(
            "Double-click a shortcut to record a new key combination. "
            "Right-click a row to clear or reset to default. "
            "Duplicates are highlighted in red."
        ))
        info.setWordWrap(True)
        info.setStyleSheet("color: palette(placeholder-text);")
        layout.addWidget(info)

        # Capture defaults from the config DEFAULTS dict so "Reset to default"
        # restores the shipped binding even after the user has saved an override.
        from qterminator.config import DEFAULTS as _CFG_DEFAULTS
        self._shortcut_defaults: dict[str, str] = dict(
            _CFG_DEFAULTS.get("keybindings", {})
        )
        all_names = set(self._config.keybindings.keys())
        # Build the ordered list of (category, [action_names]) and let any
        # action absent from SHORTCUT_CATEGORIES fall into "Other".
        categorized: list[tuple[str, list[str]]] = []
        consumed = set()
        for cat_label, names in SHORTCUT_CATEGORIES:
            present = [n for n in names if n in all_names]
            if present:
                categorized.append((cat_label, present))
                consumed.update(present)
        leftover = sorted(all_names - consumed)
        if leftover:
            categorized.append(("Other", leftover))
        # Flat list preserved for _apply() and tests.
        self._shortcut_actions: list[str] = [
            name for _, names in categorized for name in names
        ]

        self._shortcut_tree = QTreeWidget(widget)
        self._shortcut_tree.setHeaderLabels([tr("Action"), tr("Shortcut")])
        self._shortcut_tree.setColumnCount(2)
        self._shortcut_tree.setRootIsDecorated(True)
        self._shortcut_tree.setAlternatingRowColors(True)
        self._shortcut_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._shortcut_tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._shortcut_tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self._shortcut_tree.setItemDelegateForColumn(
            1, _KeySequenceDelegate(self._shortcut_tree)
        )
        self._shortcut_tree.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._shortcut_tree.customContextMenuRequested.connect(
            self._on_shortcut_context_menu
        )

        # action_name -> leaf QTreeWidgetItem. Lets _apply() / conflict
        # checks find each leaf without walking the tree.
        self._shortcut_leaves: dict[str, QTreeWidgetItem] = {}
        # action_name -> bool, mirrored from _refresh_shortcut_conflicts().
        # Tests and any external callers can query this without sniffing brushes.
        self._shortcut_conflicts: dict[str, bool] = {}

        for cat_label, names in categorized:
            cat_item = QTreeWidgetItem(self._shortcut_tree, [cat_label, ""])
            font = cat_item.font(0)
            font.setBold(True)
            cat_item.setFont(0, font)
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            cat_item.setFirstColumnSpanned(True)
            for name in names:
                leaf = QTreeWidgetItem(cat_item, [
                    _humanize_action_name(name),
                    self._config.keybindings.get(name, ""),
                ])
                leaf.setData(0, Qt.ItemDataRole.UserRole, name)
                # Only the shortcut column is editable.
                leaf.setFlags(
                    leaf.flags() | Qt.ItemFlag.ItemIsEditable
                )
                self._shortcut_leaves[name] = leaf
            cat_item.setExpanded(True)

        self._shortcut_tree.itemChanged.connect(self._on_shortcut_changed)
        layout.addWidget(self._shortcut_tree, 1)

        self._refresh_shortcut_conflicts()

        return widget

    def _on_shortcut_changed(self, item, column):
        if column == 1:
            self._refresh_shortcut_conflicts()

    def _refresh_shortcut_conflicts(self) -> None:
        """Repaint conflict highlighting across every leaf.

        Two non-empty shortcuts with the same QKeySequence (after Qt
        normalisation) flag both leaves in red with a tooltip.
        """
        by_seq: dict[str, list[tuple[str, QTreeWidgetItem]]] = {}
        for name, leaf in self._shortcut_leaves.items():
            raw = leaf.text(1).strip()
            if not raw:
                continue
            normalised = QKeySequence(raw).toString()
            by_seq.setdefault(normalised, []).append((name, leaf))

        conflict_brush = QBrush(QColor("#cf6679"))
        for name, leaf in self._shortcut_leaves.items():
            raw = leaf.text(1).strip()
            normalised = QKeySequence(raw).toString() if raw else ""
            siblings = by_seq.get(normalised, [])
            is_conflict = len(siblings) > 1
            self._shortcut_conflicts[name] = is_conflict
            if is_conflict:
                for col in (0, 1):
                    leaf.setForeground(col, conflict_brush)
                others = [s.text(0) for n, s in siblings if n != name]
                leaf.setToolTip(1, tr("Conflicts with: ") + ", ".join(others))
            else:
                # Clear the foreground role entirely so we fall back to the
                # palette text colour instead of a NoBrush solid that paints
                # as transparent on some styles.
                for col in (0, 1):
                    leaf.setData(col, Qt.ItemDataRole.ForegroundRole, None)
                leaf.setToolTip(1, "")

    def _on_shortcut_context_menu(self, pos):
        item = self._shortcut_tree.itemAt(pos)
        if item is None or item.parent() is None:
            return  # ignore category rows
        name = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self._shortcut_tree)
        act_clear = menu.addAction(tr("Clear shortcut"))
        act_reset = menu.addAction(tr("Reset to default"))
        chosen = menu.exec(self._shortcut_tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_clear:
            item.setText(1, "")
        elif chosen is act_reset:
            item.setText(1, self._shortcut_defaults.get(name, ""))

    def _load_current_settings(self):
        profile = self._config.get_profile("default")

        # Appearance
        self._font_combo.setCurrentFont(QFont(profile["font_family"]))
        self._font_size.setValue(profile["font_size"])
        self._font_ligatures.setChecked(profile.get("font_ligatures", False))
        self._update_font_preview()

        scheme = profile["color_scheme"]
        idx = self._color_scheme.findText(scheme)
        if idx >= 0:
            self._color_scheme.setCurrentIndex(idx)

        self._opacity.setValue(profile.get("background_opacity", 1.0))

        cursor_map = {"block": 0, "underline": 1, "ibeam": 2}
        self._cursor_shape.setCurrentIndex(
            cursor_map.get(profile.get("cursor_shape", "block"), 0)
        )
        self._cursor_blink.setChecked(profile.get("cursor_blink", True))

        # Behavior
        self._scrollback.setValue(profile["scrollback_lines"])

        tab_pos = self._config.get("general", "tab_position", default="top")
        pos_map = {"top": 0, "bottom": 1, "left": 2, "right": 3}
        self._tab_position.setCurrentIndex(pos_map.get(tab_pos, 0))

        theme_mode = self._config.get("general", "theme_mode", default="system")
        mode_map = {"system": 0, "dark": 1, "light": 2}
        self._theme_mode.setCurrentIndex(mode_map.get(theme_mode, 0))

        dark_scheme = self._config.get("general", "dark_color_scheme", default="Linux")
        idx = self._dark_color_scheme.findText(dark_scheme)
        if idx >= 0:
            self._dark_color_scheme.setCurrentIndex(idx)

        light_scheme = self._config.get("general", "light_color_scheme", default="BlackOnLightYellow")
        idx = self._light_color_scheme.findText(light_scheme)
        if idx >= 0:
            self._light_color_scheme.setCurrentIndex(idx)

        self._confirm_close.setChecked(
            self._config.get("general", "confirm_close", default=True)
        )
        self._show_menubar.setChecked(
            self._config.get("general", "show_menubar", default=False)
        )

    def _apply(self):
        """Apply settings to all open terminals and save to config."""
        parent = self.parent()
        if not parent:
            return

        font_family = self._font_combo.currentFont().family()
        font_size = self._font_size.value()
        color_scheme = self._color_scheme.currentText()
        scrollback = self._scrollback.value()
        cursor_shapes = ["block", "underline", "ibeam"]
        cursor_shape = cursor_shapes[self._cursor_shape.currentIndex()]
        cursor_blink = self._cursor_blink.isChecked()
        opacity = self._opacity.value()
        tab_positions = ["top", "bottom", "left", "right"]
        tab_pos = tab_positions[self._tab_position.currentIndex()]
        theme_modes = ["system", "dark", "light"]
        theme_mode = theme_modes[self._theme_mode.currentIndex()]
        dark_color_scheme = self._dark_color_scheme.currentText()
        light_color_scheme = self._light_color_scheme.currentText()

        self._config.set("profiles", "default", "font_family", font_family)
        self._config.set("profiles", "default", "font_size", font_size)
        self._config.set("profiles", "default", "font_ligatures", self._font_ligatures.isChecked())
        self._config.set("profiles", "default", "color_scheme", color_scheme)
        self._config.set("profiles", "default", "scrollback_lines", scrollback)
        self._config.set("profiles", "default", "cursor_shape", cursor_shape)
        self._config.set("profiles", "default", "cursor_blink", cursor_blink)
        self._config.set("profiles", "default", "background_opacity", opacity)
        self._config.set("general", "tab_position", tab_pos)
        self._config.set("general", "confirm_close", self._confirm_close.isChecked())
        self._config.set("general", "theme_mode", theme_mode)
        self._config.set("general", "dark_color_scheme", dark_color_scheme)
        self._config.set("general", "light_color_scheme", light_color_scheme)
        show_menubar = self._show_menubar.isChecked()
        self._config.set("general", "show_menubar", show_menubar)

        # Persist shortcut edits and re-bind on the parent window.
        for name in self._shortcut_actions:
            leaf = self._shortcut_leaves.get(name)
            keys = leaf.text(1).strip() if leaf is not None else ""
            self._config.set("keybindings", name, keys)
        if hasattr(parent, "apply_keybindings"):
            parent.apply_keybindings()

        self._config.save()

        from qterminator.theme import apply_theme
        app = QApplication.instance()
        if app:
            resolved = apply_theme(app, theme_mode)
            if hasattr(parent, '_resolved_theme'):
                parent._resolved_theme = resolved

            if hasattr(parent, 'apply_color_scheme_to_all'):
                if resolved == "light":
                    parent.apply_color_scheme_to_all(light_color_scheme)
                else:
                    parent.apply_color_scheme_to_all(dark_color_scheme)

        if hasattr(parent, '_tabs'):
            for i in range(parent._tabs.count()):
                split = parent._tabs.widget(i)
                for term in split.find_terminals():
                    term.set_font(font_family, font_size)
                    term.set_color_scheme(color_scheme)
                    term.set_scrollback(scrollback)

            tab_pos_map = {
                0: QTabWidget.TabPosition.North,
                1: QTabWidget.TabPosition.South,
                2: QTabWidget.TabPosition.West,
                3: QTabWidget.TabPosition.East,
            }
            parent._tabs.setTabPosition(
                tab_pos_map.get(self._tab_position.currentIndex(),
                                QTabWidget.TabPosition.North)
            )

            parent.menuBar().setVisible(show_menubar)

    def _apply_and_close(self):
        self._apply()
        self.accept()
