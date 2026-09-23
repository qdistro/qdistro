"""Side panel dock that hosts panels contributed by SidePanelProvider plugins.

Looks Vivaldi-ish: a thin icon strip on the left to switch panels, the
selected panel fills the rest of the dock.
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDockWidget,
    QHBoxLayout,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class SidePanel(QDockWidget):
    panel_switched = pyqtSignal(str)  # panel_id

    def __init__(self, parent=None):
        super().__init__("Side panel", parent)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._strip = QWidget()
        self._strip.setFixedWidth(36)
        strip_layout = QVBoxLayout(self._strip)
        strip_layout.setContentsMargins(2, 2, 2, 2)
        strip_layout.setSpacing(2)
        strip_layout.addStretch(1)
        self._strip_layout = strip_layout

        self._stack = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Expanding)

        layout.addWidget(self._strip)
        layout.addWidget(self._stack, 1)
        self.setWidget(container)

        self._buttons = QButtonGroup(self)
        self._buttons.setExclusive(True)
        self._panels: dict = {}      # panel_id -> (button, widget)
        self._order: list = []

    def add_panel(self, panel_id: str, label: str, icon_text: str, widget):
        if panel_id in self._panels:
            return
        btn = QToolButton()
        btn.setText(icon_text or label[:2])
        btn.setToolTip(label)
        btn.setCheckable(True)
        btn.setFixedSize(32, 32)
        btn.clicked.connect(lambda _=False, pid=panel_id: self.show_panel(pid))
        # Insert above the stretch.
        self._strip_layout.insertWidget(
            self._strip_layout.count() - 1, btn,
            alignment=Qt.AlignmentFlag.AlignTop,
        )
        self._stack.addWidget(widget)
        self._buttons.addButton(btn)
        self._panels[panel_id] = (btn, widget)
        self._order.append(panel_id)
        if len(self._panels) == 1:
            btn.setChecked(True)

    def show_panel(self, panel_id: str):
        if panel_id not in self._panels:
            return
        btn, widget = self._panels[panel_id]
        btn.setChecked(True)
        self._stack.setCurrentWidget(widget)
        self.panel_switched.emit(panel_id)
        if not self.isVisible():
            self.show()

    def get_panel(self, panel_id: str):
        item = self._panels.get(panel_id)
        return item[1] if item else None

    def panel_ids(self):
        return list(self._order)
