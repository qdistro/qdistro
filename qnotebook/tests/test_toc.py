from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook.toc import TocDock, parse_headings
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings-toc")
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d)
    )
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield


def test_parse_headings_levels_and_lines():
    md = "# Top\n\ntext\n\n## Sub\n\n### Deep\n\n## Sub2\n"
    headings = parse_headings(md)
    assert [h.level for h in headings] == [1, 2, 3, 2]
    assert [h.text for h in headings] == ["Top", "Sub", "Deep", "Sub2"]
    assert headings[0].line == 0
    assert headings[1].line == 4


def test_parse_headings_skips_fenced_code():
    md = "# Real\n\n```\n# not a heading\n```\n\n## Real2\n"
    headings = parse_headings(md)
    assert [h.text for h in headings] == ["Real", "Real2"]


def test_toc_dock_renders_and_activation_fires_callback(qapp, qtbot):
    dock = TocDock()
    qtbot.addWidget(dock)
    calls: list[int] = []
    dock.set_on_activated(calls.append)
    dock.refresh("# A\n\n## B\n\n# C\n")
    # Top-level count
    assert dock.tree.topLevelItemCount() == 2
    # First item has one child (B under A)
    a = dock.tree.topLevelItem(0)
    assert a.childCount() == 1
    # Activate B
    dock._on_item_activated(a.child(0), 0)
    assert calls == [2]  # "## B" was on line 2


def test_toc_in_window_integrates(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    # Force TOC visible so refresh path runs
    w._toggle_toc_dock(True)
    # Home page content has one heading
    w.load_page("Home")
    w._refresh_toc()
    assert w.toc_widget.tree.topLevelItemCount() >= 1
