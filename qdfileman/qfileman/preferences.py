"""Preferences dialog.

Adapted from the sibling qfileman variant under qdistro-org2. The dialog
reads and writes the same TOML keys the rest of the app already uses
(``general.show_hidden``, ``default_view`` ∈ ``{list, grid}``,
``sort_order`` ∈ ``{asc, desc}``, etc.) so existing config files keep
working.
"""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QSpinBox,
    QVBoxLayout,
)

from qfileman.config import Config

log = logging.getLogger(__name__)


# UI label ↔ on-disk value tables. Keeping these as module constants makes
# the round-trip easy to reason about and trivial to test.
_VIEW_LABEL_TO_KEY = {"List": "list", "Grid": "grid"}
_VIEW_KEY_TO_LABEL = {v: k for k, v in _VIEW_LABEL_TO_KEY.items()}

_SORT_LABEL_TO_KEY = {"Name": "name", "Size": "size", "Date": "date", "Type": "type"}
_SORT_KEY_TO_LABEL = {v: k for k, v in _SORT_LABEL_TO_KEY.items()}

_ORDER_LABEL_TO_KEY = {"Ascending": "asc", "Descending": "desc"}
_ORDER_KEY_TO_LABEL = {v: k for k, v in _ORDER_LABEL_TO_KEY.items()}

_THEME_LABEL_TO_KEY = {"System": "system", "Light": "light", "Dark": "dark"}
_THEME_KEY_TO_LABEL = {v: k for k, v in _THEME_LABEL_TO_KEY.items()}


class PreferencesDialog(QDialog):
    """Settings dialog backed by :class:`qfileman.config.Config`."""

    def __init__(self, config: Config | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.resize(420, 360)
        self.config = config or Config()
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        general = QGroupBox("General", self)
        form = QFormLayout(general)

        self.cb_show_hidden = QCheckBox("Show hidden files")
        form.addRow(self.cb_show_hidden)

        self.cb_confirm_delete = QCheckBox("Confirm before delete")
        form.addRow(self.cb_confirm_delete)

        self.cb_single_click = QCheckBox("Single-click to open")
        form.addRow(self.cb_single_click)

        self.combo_view = QComboBox()
        self.combo_view.addItems(list(_VIEW_LABEL_TO_KEY))
        form.addRow("View mode:", self.combo_view)

        self.combo_sort = QComboBox()
        self.combo_sort.addItems(list(_SORT_LABEL_TO_KEY))
        form.addRow("Sort by:", self.combo_sort)

        self.combo_order = QComboBox()
        self.combo_order.addItems(list(_ORDER_LABEL_TO_KEY))
        form.addRow("Sort order:", self.combo_order)

        self.combo_theme = QComboBox()
        self.combo_theme.addItems(list(_THEME_LABEL_TO_KEY))
        form.addRow("Theme:", self.combo_theme)

        self.spin_icon_size = QSpinBox()
        self.spin_icon_size.setRange(16, 256)
        self.spin_icon_size.setSingleStep(8)
        form.addRow("Icon size:", self.spin_icon_size)

        layout.addWidget(general)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply,
            parent=self,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        layout.addWidget(btns)

    def _load_values(self) -> None:
        g = "general"
        self.cb_show_hidden.setChecked(
            bool(self.config.get(g, "show_hidden", default=False))
        )
        self.cb_confirm_delete.setChecked(
            bool(self.config.get(g, "confirm_delete", default=True))
        )
        self.cb_single_click.setChecked(
            bool(self.config.get(g, "single_click", default=False))
        )
        view = self.config.get(g, "default_view", default="list")
        self.combo_view.setCurrentText(_VIEW_KEY_TO_LABEL.get(view, "List"))
        sort = self.config.get(g, "sort_by", default="name")
        self.combo_sort.setCurrentText(_SORT_KEY_TO_LABEL.get(sort, "Name"))
        order = self.config.get(g, "sort_order", default="asc")
        self.combo_order.setCurrentText(_ORDER_KEY_TO_LABEL.get(order, "Ascending"))
        theme = self.config.get(g, "theme_mode", default="system")
        self.combo_theme.setCurrentText(_THEME_KEY_TO_LABEL.get(theme, "System"))
        self.spin_icon_size.setValue(
            int(self.config.get(g, "icon_size", default=32))
        )

    def _apply(self) -> None:
        """Write the current dialog state back to Config and persist."""
        g = "general"
        self.config.set(g, "show_hidden", self.cb_show_hidden.isChecked())
        self.config.set(g, "confirm_delete", self.cb_confirm_delete.isChecked())
        self.config.set(g, "single_click", self.cb_single_click.isChecked())
        self.config.set(g, "default_view", _VIEW_LABEL_TO_KEY[self.combo_view.currentText()])
        self.config.set(g, "sort_by", _SORT_LABEL_TO_KEY[self.combo_sort.currentText()])
        self.config.set(g, "sort_order", _ORDER_LABEL_TO_KEY[self.combo_order.currentText()])
        self.config.set(g, "theme_mode", _THEME_LABEL_TO_KEY[self.combo_theme.currentText()])
        self.config.set(g, "icon_size", self.spin_icon_size.value())
        self.config.save()

    def accept(self) -> None:
        self._apply()
        super().accept()
