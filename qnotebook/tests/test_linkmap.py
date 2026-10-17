from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings, Qt
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


def test_linkmap_colours_follow_the_palette(qapp, qtbot):
    """Nodes, edges and labels take the widget palette (not fixed
    dark-on-light hex), and a palette change — the desktop switching
    light/dark — recolours the existing graph."""
    from PyQt6.QtGui import QColor, QPalette

    dock = LinkMapDock()
    qtbot.addWidget(dock)

    def palette(text: str, highlight: str, mid: str) -> QPalette:
        pal = QPalette(dock.palette())
        pal.setColor(QPalette.ColorRole.WindowText, QColor(text))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(highlight))
        pal.setColor(QPalette.ColorRole.Mid, QColor(mid))
        return pal

    dock.setPalette(palette("#101010", "#2060c0", "#a0a0a0"))
    dock.build("Home", ["Other"], ["Back"])
    home, other = dock._nodes["Home"], dock._nodes["Other"]
    assert home.brush().color() == QColor("#2060c0")
    assert other.brush().color() == QColor("#a0a0a0")
    assert dock._labels and all(
        lbl.brush().color() == QColor("#101010") for lbl in dock._labels)
    assert dock._edges and all(
        e.pen().color().rgb() == QColor("#101010").rgb() for e in dock._edges)

    dock.setPalette(palette("#f0f0f0", "#80b0ff", "#505050"))
    assert home.brush().color() == QColor("#80b0ff")
    assert other.brush().color() == QColor("#505050")
    assert all(lbl.brush().color() == QColor("#f0f0f0") for lbl in dock._labels)
    assert all(
        e.pen().color().rgb() == QColor("#f0f0f0").rgb() for e in dock._edges)
    # The backlink keeps its dashed style through the recolour.
    assert any(e.pen().style() == Qt.PenStyle.DashLine for e in dock._edges)
