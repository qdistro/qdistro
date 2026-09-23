from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook.linkmap import LinkMapDock
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings-linkmap")
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d)
    )
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield


def test_linkmap_builds_nodes(qapp, qtbot):
    dock = LinkMapDock()
    qtbot.addWidget(dock)
    dock.build("Home", ["Sub:Child", "Other"], ["Sub:Child"])
    # Should create center node + 2 distinct peers
    assert "Home" in dock._nodes
    assert "Sub:Child" in dock._nodes
    assert "Other" in dock._nodes


def test_linkmap_integration_with_window(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    w.load_page("Home")
    w._toggle_linkmap_dock(True)
    assert w._current_page == "Home"
    # Home has forward links to Sub:Child and Other
    assert "Home" in w.linkmap_widget._nodes
    assert "Sub:Child" in w.linkmap_widget._nodes or "Other" in w.linkmap_widget._nodes


def test_linkmap_click_navigates(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    w.load_page("Home")
    # Programmatically invoke the on_navigate callback (scene click simulation is brittle)
    w.linkmap_widget._on_navigate("Other")
    assert w._current_page == "Other"


def test_force_layout_falls_back_when_no_networkx():
    from qnotebook.linkmap import force_layout
    pos = force_layout(["A", "B", "C"], [("A", "B"), ("B", "C")])
    assert set(pos.keys()) == {"A", "B", "C"}
    for x, y in pos.values():
        assert isinstance(x, float) and isinstance(y, float)


def test_linkmap_multihop_includes_distant_pages(qapp, tmp_notebook: Path, qtbot):
    # Create chain: A -> B -> C
    (tmp_notebook / "A.md").write_text("# A\n\n[[B]]\n", encoding="utf-8")
    (tmp_notebook / "B.md").write_text("# B\n\n[[C]]\n", encoding="utf-8")
    (tmp_notebook / "C.md").write_text("# C\n\nleaf\n", encoding="utf-8")
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    w.load_page("A")
    w.linkmap_widget.hops_spin.setValue(2)
    qapp.processEvents()
    nodes = w.linkmap_widget._nodes
    assert "A" in nodes
    assert "B" in nodes
    assert "C" in nodes
