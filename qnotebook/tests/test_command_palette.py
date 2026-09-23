"""Command palette: search + trigger menu actions."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook.command_palette import CommandPalette, collect_actions
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear(); s.sync()
    yield


@pytest.fixture
def win(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    yield w


def test_collect_actions_non_empty(win):
    actions = collect_actions(win.menuBar())
    assert len(actions) > 5
    # Should include Save:
    labels = [a.text().replace("&", "") for a in actions]
    assert any("Save" in label for label in labels)


def test_command_palette_filters_by_query(win, qtbot):
    actions = collect_actions(win.menuBar())
    dlg = CommandPalette(actions, win)
    qtbot.addWidget(dlg)
    dlg.input.setText("Save")
    qtbot.wait(50)
    assert dlg.list.count() >= 1
    # Top entry contains "Save"
    top = dlg.list.item(0).text()
    assert "Save" in top


def test_command_palette_triggers_action(win, qtbot):
    # Use a trivially-safe action: Find.
    actions = collect_actions(win.menuBar())
    dlg = CommandPalette(actions, win)
    qtbot.addWidget(dlg)
    dlg.input.setText("Find")
    dlg._accept_current()
    a = dlg.chosen()
    assert a is not None
    # The action should be one that contains "Find"
    assert "Find" in a.text() or "find" in a.text().lower()
