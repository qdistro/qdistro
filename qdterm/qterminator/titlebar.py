"""Per-terminal titlebar widget showing title, group, and status indicators."""

from __future__ import annotations

import hashlib
import re

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QWidget,
)

TITLE_HEIGHT = 20

# Group colors identify a named group. They are not appearance palette roles.
GROUP_COLORS = [
    "#c0392b", "#27ae60", "#2980b9", "#8e44ad",
    "#d35400", "#16a085", "#2c3e50", "#f39c12",
]

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_FALLBACK_ERROR = "#e74c3c"
_FALLBACK_ACTIVITY = "#f1c40f"
_FALLBACK_VM_FG = "#111111"
_FALLBACK_SECONDARY = "#a9aefe"
_NEAR_WHITE = {"#ffffff", "#f3edf7", "#dddddd", "#d4d4d4"}
_NEAR_BLACK = {"#000000", "#0e0e43", "#1e1e1e", "#111111"}
CHROME_ROLES = frozenset({"activity", "error", "primary", "secondary", "dim"})


def group_color_for_name(name: str) -> str:
    """Map a group name to a cosmetic identity color.

    Uses SHA-256 of the UTF-8 name so the same group keeps the same
    palette entry across process restarts and PYTHONHASHSEED values.
    These colors identify a named group; they are not snapshot roles.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:8], "big") % len(GROUP_COLORS)
    return GROUP_COLORS[idx]


def _css_hex(value: object, fallback: str) -> str:
    if isinstance(value, str) and _HEX.fullmatch(value.strip()):
        return value.strip().lower()
    if isinstance(fallback, str) and _HEX.fullmatch(fallback.strip()):
        return fallback.strip().lower()
    return "#000000"


def _qcolor_hex(color: QColor, fallback: str) -> str:
    if color.isValid():
        return _css_hex(
            f"#{color.red():02x}{color.green():02x}{color.blue():02x}",
            fallback,
        )
    return _css_hex(fallback, "#000000")


def titlebar_roles(widget: QWidget) -> dict[str, str]:
    """Chrome colors from the shared snapshot, else the widget palette."""
    colors = None
    try:
        from qterminator.theme import current_controller

        ctrl = current_controller()
        state = getattr(ctrl, "state", None) if ctrl is not None else None
        snap = getattr(state, "snapshot", None) if state is not None else None
        if getattr(state, "using_shared_palette", False) and snap is not None:
            colors = snap.colors
    except Exception:  # noqa: BLE001
        colors = None
    if colors is not None:
        return {
            "active_bg": _css_hex(colors.mPrimary, "#2a6ea8"),
            "active_fg": _css_hex(colors.mOnPrimary, "#ffffff"),
            "inactive_bg": _css_hex(colors.mSurfaceVariant, "#3c3c3c"),
            "inactive_fg": _css_hex(colors.mOnSurface, "#dddddd"),
            "dim": _css_hex(colors.mOnSurfaceVariant, "#aaaaaa"),
            "error": _css_hex(colors.mError, _FALLBACK_ERROR),
            "activity": _css_hex(colors.mTertiary, _FALLBACK_ACTIVITY),
            "primary": _css_hex(colors.mPrimary, "#2a6ea8"),
            "secondary": _css_hex(colors.mSecondary, _FALLBACK_SECONDARY),
            "hover_bg": _css_hex(colors.mHover, colors.mPrimary),
            "hover_fg": _css_hex(colors.mOnHover, colors.mOnPrimary),
            "vm_bg": _css_hex(colors.mTertiary, _FALLBACK_ACTIVITY),
            "vm_fg": _css_hex(colors.mOnTertiary, _FALLBACK_VM_FG),
        }
    pal = widget.palette()
    highlight = _qcolor_hex(pal.color(QPalette.ColorRole.Highlight), "#2a6ea8")
    highlighted = _qcolor_hex(
        pal.color(QPalette.ColorRole.HighlightedText), "#ffffff"
    )
    alt = _qcolor_hex(pal.color(QPalette.ColorRole.AlternateBase), "#3c3c3c")
    fg = _qcolor_hex(pal.color(QPalette.ColorRole.WindowText), "#dddddd")
    dim = _qcolor_hex(pal.color(QPalette.ColorRole.PlaceholderText), "#aaaaaa")
    bright = _qcolor_hex(pal.color(QPalette.ColorRole.BrightText), _FALLBACK_ERROR)
    error = bright
    if bright.lower() in {fg.lower(), highlighted.lower()} | _NEAR_WHITE | _NEAR_BLACK:
        error = _FALLBACK_ERROR
    link = _qcolor_hex(pal.color(QPalette.ColorRole.LinkVisited), _FALLBACK_ACTIVITY)
    activity = link
    if link.lower() in {error.lower(), fg.lower(), highlight.lower()}:
        activity = _FALLBACK_ACTIVITY
    secondary = _qcolor_hex(
        pal.color(QPalette.ColorRole.Link), _FALLBACK_SECONDARY
    )
    return {
        "active_bg": highlight,
        "active_fg": highlighted,
        "inactive_bg": alt,
        "inactive_fg": fg,
        "dim": dim,
        "error": error,
        "activity": activity,
        "primary": highlight,
        "secondary": secondary,
        "hover_bg": highlight,
        "hover_fg": highlighted,
        "vm_bg": activity,
        "vm_fg": _FALLBACK_VM_FG,
    }


def _ui_font(*, relative: float = 1.0, bold: bool = False) -> QFont:
    app = QApplication.instance()
    font = QFont(app.font()) if app is not None else QFont()
    size = font.pointSizeF()
    if size <= 0:
        size = float(font.pointSize() or 11)
    font.setPointSizeF(max(6.0, size * relative))
    font.setBold(bold)
    return font


class TerminalTitlebar(QFrame):
    """Small titlebar shown above each terminal in split views."""

    close_clicked = pyqtSignal()
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self._active = False
        self._group_name = None
        self._vm_name = None
        self._activity_role = "activity"
        self._extra_widgets = {}
        self._extra_roles = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 2, 0)
        layout.setSpacing(4)
        self._layout = layout

        self._group_label = QLabel()
        self._group_label.setFixedSize(12, 12)
        self._group_label.hide()
        layout.addWidget(self._group_label)

        self._readonly_label = QLabel("[RO]")
        self._readonly_label.hide()
        layout.addWidget(self._readonly_label)

        self._activity_label = QLabel("\u25cf")
        self._activity_label.setToolTip("Activity detected")
        self._activity_label.hide()
        layout.addWidget(self._activity_label)

        self._left_extra_start = layout.count()
        self._left_extra_count = 0

        self._title_label = QLabel("Terminal")
        layout.addWidget(self._title_label, 1)

        self._right_extra_start = layout.count()
        self._right_extra_count = 0

        self._close_btn = QPushButton("\u00d7")
        self._close_btn.setFixedSize(16, 16)
        self._close_btn.setFlat(True)
        self._close_btn.clicked.connect(self.close_clicked.emit)
        layout.addWidget(self._close_btn)

        self._apply_chrome()

    def apply_presentation_update(self) -> None:
        self._apply_chrome()

    def _apply_chrome(self) -> None:
        roles = titlebar_roles(self)
        title_font = _ui_font()
        small_font = _ui_font(relative=0.9)
        height = max(TITLE_HEIGHT, QFontMetrics(title_font).height() + 6)
        self.setFixedHeight(height)
        bg = roles["active_bg"] if self._active else roles["inactive_bg"]
        fg = roles["active_fg"] if self._active else roles["inactive_fg"]
        self.setStyleSheet(f"TerminalTitlebar {{ background-color: {bg}; }}")

        self._title_label.setFont(title_font)
        self._title_label.setStyleSheet(f"color: {fg};")

        self._readonly_label.setFont(_ui_font(relative=0.9, bold=True))
        self._readonly_label.setStyleSheet(f"color: {roles['error']};")

        activity_role = self._activity_role if self._activity_role in CHROME_ROLES else "activity"
        activity_color = roles.get(activity_role, roles["activity"])
        if activity_role == "primary" and self._active:
            # primary fills the active bar; use the on-primary pair so the
            # progress dot stays visible on that background.
            activity_color = roles["active_fg"]
        self._activity_label.setFont(small_font)
        self._activity_label.setStyleSheet(f"color: {activity_color};")

        btn = max(16, height - 4)
        self._close_btn.setFixedSize(btn, btn)
        self._close_btn.setFont(title_font)
        self._close_btn.setStyleSheet(
            f"QPushButton {{ color: {roles['dim']}; border: none; }}"
            f"QPushButton:hover {{ color: {roles['hover_fg']}; "
            f"background: {roles['hover_bg']}; border-radius: 3px; }}"
        )

        if self._group_name:
            color = group_color_for_name(self._group_name)
            self._group_label.setStyleSheet(
                f"background-color: {color}; border-radius: 6px;"
            )

        vm = self.titlebar_widget("vm-indicator")
        if isinstance(vm, QLabel) and self._vm_name:
            vm.setFont(_ui_font(relative=0.9, bold=True))
            vm.setStyleSheet(
                f"color: {roles['vm_fg']}; background: {roles['vm_bg']}; "
                "border-radius: 3px; padding: 0 4px;"
            )

        for name, (widget, _side) in self._extra_widgets.items():
            if isinstance(widget, QToolButton):
                widget.setFont(small_font)
                widget.setStyleSheet(
                    f"QToolButton {{ color: {roles['dim']}; border: none; }}"
                    f"QToolButton:hover {{ color: {roles['hover_fg']}; "
                    f"background: {roles['hover_bg']}; border-radius: 3px; }}"
                )
            elif isinstance(widget, QLabel) and name != "vm-indicator":
                extra_role = self._extra_roles.get(name, "dim")
                if extra_role not in CHROME_ROLES:
                    extra_role = "dim"
                extra_color = roles.get(extra_role, roles["dim"])
                widget.setFont(_ui_font(relative=0.9, bold=True))
                widget.setStyleSheet(f"color: {extra_color};")

    def set_title(self, title):
        if len(title) > 60:
            title = title[:57] + "..."
        self._title_label.setText(title)

    def set_active(self, active):
        self._active = active
        self._apply_chrome()

    def set_group(self, group_name):
        """Show group indicator with a color based on group name."""
        self._group_name = group_name or None
        if self._group_name:
            self._group_label.setToolTip(f"Group: {self._group_name}")
            self._group_label.show()
            self._apply_chrome()
        else:
            self._group_label.hide()

    def set_read_only(self, read_only):
        self._readonly_label.setVisible(read_only)

    def set_activity(self, has_activity):
        if not has_activity:
            self._activity_role = "activity"
        self._activity_label.setVisible(has_activity)

    def set_activity_style(self, role: str) -> None:
        """Paint the activity indicator from a semantic chrome role."""
        self._activity_role = role if role in CHROME_ROLES else "activity"
        if not self._activity_label.isHidden():
            self._apply_chrome()

    def add_titlebar_widget(
        self,
        name: str,
        widget: QWidget,
        side: str = "right",
        role: str | None = None,
    ) -> QWidget:
        """Add or replace a named Qt widget in the titlebar extension area.

        side="left" inserts between the built-in indicators and the title.
        side="right" inserts between the title and the close button.
        role names a semantic chrome color for extra QLabel widgets.
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
        if role is not None:
            self._extra_roles[name] = role if role in CHROME_ROLES else "dim"
        self._apply_chrome()
        return widget

    def set_titlebar_widget_role(self, name: str, role: str) -> None:
        if name not in self._extra_widgets:
            return
        self._extra_roles[name] = role if role in CHROME_ROLES else "dim"
        self._apply_chrome()

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
        if callback is not None:
            button.clicked.connect(callback)
        return self.add_titlebar_widget(name, button, side)

    def remove_titlebar_widget(self, name: str) -> bool:
        """Remove a widget previously installed with add_titlebar_widget."""
        entry = self._extra_widgets.pop(name, None)
        if entry is None:
            return False

        widget, side = entry
        self._extra_roles.pop(name, None)
        self._layout.removeWidget(widget)
        widget.hide()
        widget.setParent(None)
        if side == "left":
            self._left_extra_count -= 1
            self._right_extra_start -= 1
        else:
            self._right_extra_count -= 1
        if name == "vm-indicator":
            self._vm_name = None
        return True

    def titlebar_widget(self, name: str) -> QWidget | None:
        entry = self._extra_widgets.get(name)
        return entry[0] if entry else None

    def set_vm_indicator(self, vm_name: str | None):
        """Show or hide a compact VM indicator on the titlebar."""
        if not vm_name:
            self.remove_titlebar_widget("vm-indicator")
            return
        self._vm_name = vm_name
        existing = self.titlebar_widget("vm-indicator")
        if isinstance(existing, QLabel):
            existing.setText(f"VM: {vm_name}")
            existing.setToolTip(f"Running in VM: {vm_name}")
            self._apply_chrome()
            return
        label = QLabel(f"VM: {vm_name}", self)
        label.setToolTip(f"Running in VM: {vm_name}")
        self.add_titlebar_widget("vm-indicator", label, side="left")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
