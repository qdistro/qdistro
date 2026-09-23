"""Per-terminal titlebar widget showing title, group, and status indicators."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QWidget

ACTIVE_BG = "#2a6ea8"
INACTIVE_BG = "#3c3c3c"
TITLE_HEIGHT = 20

# Group colors for visual distinction
GROUP_COLORS = [
    "#c0392b", "#27ae60", "#2980b9", "#8e44ad",
    "#d35400", "#16a085", "#2c3e50", "#f39c12",
]


class TerminalTitlebar(QFrame):
    """Small titlebar shown above each terminal in split views."""

    close_clicked = pyqtSignal()
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(TITLE_HEIGHT)
        self.setAutoFillBackground(True)
        self._active = False
        self._extra_widgets = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 2, 0)
        layout.setSpacing(4)
        self._layout = layout

        # Group indicator (colored dot)
        self._group_label = QLabel()
        self._group_label.setFixedSize(12, 12)
        self._group_label.hide()
        layout.addWidget(self._group_label)

        # Read-only indicator
        self._readonly_label = QLabel("[RO]")
        self._readonly_label.setStyleSheet("color: #e74c3c; font-size: 10px; font-weight: bold;")
        self._readonly_label.hide()
        layout.addWidget(self._readonly_label)

        # Activity indicator
        self._activity_label = QLabel("\u25cf")  # ● dot
        self._activity_label.setStyleSheet("color: #f1c40f; font-size: 10px;")
        self._activity_label.setToolTip("Activity detected")
        self._activity_label.hide()
        layout.addWidget(self._activity_label)

        self._left_extra_start = layout.count()
        self._left_extra_count = 0

        # Title
        self._title_label = QLabel("Terminal")
        self._title_label.setStyleSheet("color: #ddd; font-size: 11px;")
        layout.addWidget(self._title_label, 1)

        self._right_extra_start = layout.count()
        self._right_extra_count = 0

        # Close button
        self._close_btn = QPushButton("\u00d7")  # × symbol
        self._close_btn.setFixedSize(16, 16)
        self._close_btn.setFlat(True)
        self._close_btn.setStyleSheet(
            "QPushButton { color: #aaa; font-size: 12px; border: none; }"
            "QPushButton:hover { color: #fff; background: #555; border-radius: 3px; }"
        )
        self._close_btn.clicked.connect(self.close_clicked.emit)
        layout.addWidget(self._close_btn)

        self.set_active(False)

    def set_title(self, title):
        if len(title) > 60:
            title = title[:57] + "..."
        self._title_label.setText(title)

    def set_active(self, active):
        self._active = active
        bg = ACTIVE_BG if active else INACTIVE_BG
        self.setStyleSheet(f"TerminalTitlebar {{ background-color: {bg}; }}")

    def set_group(self, group_name):
        """Show group indicator with a color based on group name."""
        if group_name:
            color_idx = hash(group_name) % len(GROUP_COLORS)
            color = GROUP_COLORS[color_idx]
            self._group_label.setStyleSheet(
                f"background-color: {color}; border-radius: 6px;"
            )
            self._group_label.setToolTip(f"Group: {group_name}")
            self._group_label.show()
        else:
            self._group_label.hide()

    def set_read_only(self, read_only):
        self._readonly_label.setVisible(read_only)

    def set_activity(self, has_activity):
        self._activity_label.setVisible(has_activity)

    def add_titlebar_widget(self, name: str, widget: QWidget, side: str = "right") -> QWidget:
        """Add or replace a named Qt widget in the titlebar extension area.

        side="left" inserts between the built-in indicators and the title.
        side="right" inserts between the title and the close button.
        """
        if not name:
            raise ValueError("titlebar widget name must be non-empty")
        if widget is None:
            raise ValueError("titlebar widget must not be None")
        if side not in {"left", "right"}:
            raise ValueError("side must be 'left' or 'right'")

        self.remove_titlebar_widget(name)
        if widget.parent() is None:
            widget.setParent(self)

        if side == "left":
            index = self._left_extra_start + self._left_extra_count
            self._left_extra_count += 1
            self._right_extra_start += 1
        else:
            index = self._right_extra_start + self._right_extra_count
            self._right_extra_count += 1

        self._layout.insertWidget(index, widget)
        self._extra_widgets[name] = (widget, side)
        return widget

    def add_titlebar_button(
        self,
        name: str,
        text: str,
        tooltip: str = "",
        callback=None,
        side: str = "right",
    ) -> QToolButton:
        """Create and add a named QToolButton in the titlebar extension area."""
        button = QToolButton(self)
        button.setText(text)
        button.setFixedSize(16, 16)
        button.setToolTip(tooltip)
        button.setStyleSheet(
            "QToolButton { color: #aaa; font-size: 11px; border: none; }"
            "QToolButton:hover { color: #fff; background: #555; border-radius: 3px; }"
        )
        if callback is not None:
            button.clicked.connect(callback)
        return self.add_titlebar_widget(name, button, side)

    def remove_titlebar_widget(self, name: str) -> bool:
        """Remove a widget previously installed with add_titlebar_widget."""
        entry = self._extra_widgets.pop(name, None)
        if entry is None:
            return False

        widget, side = entry
        self._layout.removeWidget(widget)
        widget.hide()
        widget.setParent(None)
        if side == "left":
            self._left_extra_count -= 1
            self._right_extra_start -= 1
        else:
            self._right_extra_count -= 1
        return True

    def titlebar_widget(self, name: str) -> QWidget | None:
        entry = self._extra_widgets.get(name)
        return entry[0] if entry else None

    def set_vm_indicator(self, vm_name: str | None):
        """Show or hide a compact VM indicator on the titlebar."""
        if not vm_name:
            self.remove_titlebar_widget("vm-indicator")
            return
        label = QLabel(f"VM: {vm_name}", self)
        label.setToolTip(f"Running in VM: {vm_name}")
        label.setStyleSheet(
            "color: #111; background: #f1c40f; border-radius: 3px;"
            "font-size: 10px; font-weight: bold; padding: 0 4px;"
        )
        self.add_titlebar_widget("vm-indicator", label, side="left")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
