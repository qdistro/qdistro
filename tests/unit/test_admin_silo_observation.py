"""Real SilosTab timer refreshes evidence without changing lifecycle authority."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QObject, QEventLoop, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

import qdistro_session_manager as sm
from qdistro_admin_app import SilosTab
from test_session_manager import _FakeOps


class StoreBridge(QObject):
    siloChanged = pyqtSignal(str, str)

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.calls = 0
        self.reverse = False
        self.stopped = []
        store._on_change = self.siloChanged.emit

    def list_silos(self):
        self.calls += 1
        rows = [silo.to_dict() for silo in self.store.list_silos()]
        return list(reversed(rows)) if self.reverse else rows

    def stop(self, name, timeout):
        self.stopped.append((name, timeout))


@pytest.fixture
def tab_with_store(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    clock = {"mono": 100.0, "wall": 1000.0}
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(sm.time, "time", lambda: clock["wall"])
    ops = _FakeOps()
    store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    store.create("other", 2001)
    store.start("work")
    ops.observe_silo = lambda *args: ("launcher-running", "unit active")
    store.observe_runtime_once()
    bridge = StoreBridge(store)
    tab = SilosTab(bridge)
    tab.refresh()
    yield tab, bridge, store, clock
    tab._observation_timer.stop()
    tab._refresh_timer.stop()
    tab.close()
    tab.deleteLater()
    app.processEvents()


def timer_refresh(tab, bridge):
    """Use a real Qt timer event; the deadline is only a failing-path guard."""
    assert tab._observation_timer.isActive()
    assert tab._observation_timer.interval() <= 5000
    before = bridge.calls
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    tab._observation_timer.timeout.connect(loop.quit)
    tab._observation_timer.setInterval(1)
    deadline.start(1000)
    loop.exec()
    tab._observation_timer.stop()
    deadline.stop()
    tab._observation_timer.timeout.disconnect(loop.quit)
    assert bridge.calls == before + 1, "Production observation timer did not refresh the tab"


def row_index(tab, name):
    return next(i for i in range(tab.model.rowCount()) if tab.model.item(i, 0).text() == name)


def test_timer_expires_evidence_without_status_event(tab_with_store):
    tab, bridge, store, clock = tab_with_store
    events = []
    bridge.siloChanged.connect(lambda *args: events.append(args))
    idx = row_index(tab, "work")
    tab.table.selectRow(idx)
    assert tab.model.item(idx, 4).text() == "launcher-running"
    clock["mono"] = 131.0
    bridge.reverse = True
    timer_refresh(tab, bridge)
    idx = row_index(tab, "work")
    assert events == []
    assert tab.model.item(idx, 4).text() == "unknown"
    assert "observation stale or unavailable" in tab.model.item(idx, 4).toolTip()
    assert tab.model.item(idx, 2).text() == "Active"
    assert tab._selected_row()["name"] == "work"
    with pytest.raises(sm.SiloBusy):
        store.delete("work")
    tab._do("stop")
    assert bridge.stopped == [("work", 5)]


def test_same_status_new_evidence_advances_tooltip(tab_with_store):
    tab, bridge, store, clock = tab_with_store
    events = []
    bridge.siloChanged.connect(lambda *args: events.append(args))
    idx = row_index(tab, "work")
    tab.table.selectRow(idx)
    assert "Observed at: 1000.0;" in tab.model.item(idx, 4).toolTip()
    clock.update(mono=105.0, wall=1005.0)
    store.observe_runtime_once()
    assert events == []
    timer_refresh(tab, bridge)
    idx = row_index(tab, "work")
    assert tab.model.item(idx, 4).text() == "launcher-running"
    assert "Observed at: 1005.0;" in tab.model.item(idx, 4).toolTip()
    assert tab._selected_row()["name"] == "work"
    assert store.get("work").state == sm.State.ACTIVE
    with pytest.raises(sm.SiloBusy):
        store.delete("work")


def test_removed_selected_silo_cannot_target_replacement(tab_with_store):
    tab, bridge, store, clock = tab_with_store
    tab.table.selectRow(row_index(tab, "other"))
    store.delete("other")
    timer_refresh(tab, bridge)
    assert tab._selected_row() is None
    tab._do("stop")
    assert bridge.stopped == []


def test_background_failure_invalidates_evidence_without_modal(tab_with_store, monkeypatch):
    import dbus
    from PyQt6.QtWidgets import QMessageBox

    tab, bridge, store, clock = tab_with_store
    tab.table.selectRow(row_index(tab, "work"))
    def unavailable():
        bridge.calls += 1
        raise dbus.DBusException("backend unavailable")
    monkeypatch.setattr(bridge, "list_silos", unavailable)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    timer_refresh(tab, bridge)
    idx = row_index(tab, "work")
    assert tab.model.item(idx, 4).text() == "unknown"
    assert tab.model.item(idx, 4).toolTip() == "Runtime observation unavailable: refresh failed"
    assert warnings == []
    assert tab._selected_row()["name"] == "work"
    assert tab.model.item(idx, 2).text() == "Active"
    with pytest.raises(sm.SiloBusy):
        store.delete("work")
