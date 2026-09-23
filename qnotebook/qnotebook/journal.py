"""Journal/Calendar dock: click a date → open/create Journal:YYYY:MM:DD."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from PyQt6.QtCore import QDate
from PyQt6.QtGui import QFont, QTextCharFormat
from PyQt6.QtWidgets import QCalendarWidget, QVBoxLayout, QWidget


def journal_page_for_date(d: date) -> str:
    return f"Journal:{d.year:04d}:{d.month:02d}:{d.day:02d}"


class CalendarDock(QWidget):
    """Calendar widget with bold-dates for journal entries."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        self.calendar = QCalendarWidget(self)
        self.calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        self.calendar.setGridVisible(True)
        self.calendar.activated.connect(self._on_activated)
        self.calendar.clicked.connect(self._on_activated)
        self.calendar.currentPageChanged.connect(self._on_page_changed)
        v.addWidget(self.calendar, 1)

        self._on_date_activated: Callable[[date], None] = lambda _d: None
        self._page_exists: Callable[[str], bool] = lambda _p: False
        self._highlighted: set[QDate] = set()

    def set_on_date_activated(self, cb: Callable[[date], None]) -> None:
        self._on_date_activated = cb

    def set_page_exists(self, cb: Callable[[str], bool]) -> None:
        self._page_exists = cb

    def refresh_highlights(self) -> None:
        """Repaint bold cells for dates with existing journal pages."""
        # Clear prior highlights
        clear_fmt = QTextCharFormat()
        for d in list(self._highlighted):
            self.calendar.setDateTextFormat(d, clear_fmt)
        self._highlighted.clear()

        bold_fmt = QTextCharFormat()
        bold_fmt.setFontWeight(QFont.Weight.Bold)
        # Only scan the currently-displayed month's days for speed.
        year = self.calendar.yearShown()
        month = self.calendar.monthShown()
        # Paint for every day in that month that has a page.
        import calendar as _cal
        _, ndays = _cal.monthrange(year, month)
        for day in range(1, ndays + 1):
            d_py = date(year, month, day)
            page = journal_page_for_date(d_py)
            if self._page_exists(page):
                qd = QDate(year, month, day)
                self.calendar.setDateTextFormat(qd, bold_fmt)
                self._highlighted.add(qd)

    def _on_activated(self, qd: QDate) -> None:
        d_py = date(qd.year(), qd.month(), qd.day())
        self._on_date_activated(d_py)

    def _on_page_changed(self, _year: int, _month: int) -> None:
        self.refresh_highlights()
