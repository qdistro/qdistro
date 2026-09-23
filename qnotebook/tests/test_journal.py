from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook.journal import journal_page_for_date
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings-journal")
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d)
    )
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield


def test_journal_page_for_date_formats_correctly():
    assert journal_page_for_date(date(2026, 4, 15)) == "Journal:2026:04:15"
    assert journal_page_for_date(date(2001, 1, 3)) == "Journal:2001:01:03"


def test_calendar_dock_activate_creates_page(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    today = date.today()
    w._on_calendar_date_activated(today)
    expected = journal_page_for_date(today)
    assert w.notebook.exists(expected)
    assert w._current_page == expected
    body = w.notebook.get_page(expected)
    # Should have been rendered from the Daily Journal template, substituting date
    assert today.isoformat() in body


def test_calendar_dock_activate_existing_just_opens(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    target = journal_page_for_date(date(2026, 1, 2))
    w.notebook.create_page(target, "# prewritten\n\npre-existing content\n")
    w._on_calendar_date_activated(date(2026, 1, 2))
    assert w._current_page == target
    assert "pre-existing" in w.notebook.get_page(target)


def test_calendar_dock_toggle_visibility(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    assert not w.calendar_dock.isVisible()
    w._toggle_calendar_dock(True)
    assert w.calendar_widget is not None
    # Check highlights refresh runs without exception
    w.calendar_widget.refresh_highlights()


def test_calendar_highlights_existing_journal_pages(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    today = date.today()
    w.notebook.create_page(journal_page_for_date(today), "# today\n")
    # Drive the calendar to today's month/year
    from PyQt6.QtCore import QDate
    w.calendar_widget.calendar.setCurrentPage(today.year, today.month)
    w.calendar_widget.refresh_highlights()
    qd = QDate(today.year, today.month, today.day)
    assert qd in w.calendar_widget._highlighted
