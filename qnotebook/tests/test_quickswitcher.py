from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook.quickswitcher import QuickSwitcher, fuzzy_score, rank
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()


def test_fuzzy_score_basename_prefix_beats_distant_match():
    pages = ["Other:Foo:Home", "Home"]
    scores = {p: fuzzy_score("hom", p) for p in pages}
    assert scores["Home"] > scores["Other:Foo:Home"]


def test_fuzzy_score_no_match_returns_none():
    assert fuzzy_score("xyzq", "Home") is None


def test_rank_orders_results():
    pages = ["Home", "Sub:Child", "Sub", "Other"]
    res = rank("sub", pages)
    names = [n for _s, n in res]
    assert names[0] in ("Sub", "Sub:Child")
    assert "Other" not in names


def test_quickswitcher_opens_via_window(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    pages = w.index.all_pages()
    dlg = QuickSwitcher(pages, w)
    qtbot.addWidget(dlg)
    dlg.input.setText("Other")
    qapp.processEvents()
    assert dlg.list.count() >= 1
    assert dlg.list.item(0).text() == "Other"


def test_quickswitcher_print_shortcut_rebound(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    # Print moved to Ctrl+Alt+P in wave4 phase 9 (Ctrl+Shift+P -> Command Palette).
    assert w.act_print.shortcut().toString() == "Ctrl+Alt+P"
    assert w.act_quick_switch.shortcut().toString() == "Ctrl+P"
    assert w.act_command_palette.shortcut().toString() == "Ctrl+Shift+P"
