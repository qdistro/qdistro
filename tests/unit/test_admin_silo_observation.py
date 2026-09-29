"""Real SilosTab timer refreshes evidence without changing lifecycle authority."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QObject, QEventLoop, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

import qdistro_session_manager as sm
from qdistro_admin_app import SessionManagerBridge, SilosTab
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

    def list_silos_async(self, reply, error):
        try:
            rows = self.list_silos()
        except Exception as exc:
            error(exc)
            return None
        reply(rows)
        return None


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
    tab._expiry_timer.stop()
    tab._read_timeout.stop()
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


class PendingRead:
    cancelled = False

    def cancel(self):
        self.cancelled = True


class DelayedProxy:
    def __init__(self, store):
        self.store = store
        self.requests = []

    def ListSilos(self, **kwargs):
        import json

        rows = [s.to_dict() for s in self.store.list_silos()]
        if "reply_handler" not in kwargs:
            return json.dumps(rows)
        assert kwargs["timeout"] == 3.0
        pending = PendingRead()
        self.requests.append((kwargs, json.dumps(rows), pending))
        return pending


@pytest.fixture
def real_async_tab(tab_with_store, monkeypatch):
    import dbus

    old_tab, _, store, clock = tab_with_store
    old_tab._observation_timer.stop()
    old_tab._expiry_timer.stop()
    proxy = DelayedProxy(store)
    options = []
    class Bus:
        def get_object(self, *args, **kwargs):
            options.append(kwargs)
            return proxy
        def add_signal_receiver(self, *args, **kwargs):
            pass
    monkeypatch.setattr(dbus, "SystemBus", Bus)
    bridge = SessionManagerBridge()
    assert options == [{}, {"introspect": False, "follow_name_owner_changes": True}]
    tab = SilosTab(bridge)
    tab.refresh()
    yield tab, bridge, proxy, store, clock
    for timer in (tab._observation_timer, tab._expiry_timer, tab._refresh_timer, tab._read_timeout):
        timer.stop()
    tab.close()
    tab.deleteLater()
    QApplication.instance().processEvents()


@pytest.mark.slow
def test_real_bridge_production_timer_remains_responsive_and_expires_pending(real_async_tab):
    tab, bridge, proxy, store, clock = real_async_tab
    assert tab._observation_timer.interval() == 5000
    beats = []
    heartbeat = QTimer()
    heartbeat.setInterval(20)
    heartbeat.timeout.connect(lambda: beats.append(True))
    heartbeat.start()
    clock["mono"] = 129.0
    loop = QEventLoop()
    advance = QTimer()
    advance.setSingleShot(True)
    advance.timeout.connect(lambda: clock.update(mono=131.0))
    advance.start(5500)
    end = QTimer()
    end.setSingleShot(True)
    end.timeout.connect(loop.quit)
    end.start(6200)
    loop.exec()
    heartbeat.stop()
    advance.stop()
    end.stop()
    assert len(beats) > 200, "Qt heartbeat stalled while the async ListSilos reply was delayed"
    assert len(proxy.requests) == 1
    assert tab._pending_generation is not None
    assert tab.model.item(row_index(tab, "work"), 4).text() == "unknown"
    # A late response's TTL includes its full transit time, so it cannot
    # turn this expired sample back into affirmative evidence.
    callbacks, raw, pending = proxy.requests[0]
    callbacks["reply_handler"](raw)
    assert tab.model.item(row_index(tab, "work"), 4).text() == "unknown"
    assert store.get("work").state == sm.State.ACTIVE


def test_watchdog_stale_reply_and_newer_evidence_keep_named_target(real_async_tab):
    import json

    tab, bridge, proxy, store, clock = real_async_tab
    tab.table.selectRow(row_index(tab, "work"))
    tab._refresh_observations()
    tab._refresh_observations()
    assert len(proxy.requests) == 1
    callbacks, raw, pending = proxy.requests[0]
    # Exercise the production watchdog signal without a timing-dependent wait.
    tab._read_timeout.timeout.emit()
    assert pending.cancelled
    assert tab._pending_generation is None
    assert tab.model.item(row_index(tab, "work"), 4).text() == "unknown"
    clock.update(mono=105.0, wall=1005.0)
    store.observe_runtime_once()
    tab._refresh_observations()
    fresh, fresh_raw, _ = proxy.requests[1]
    fresh["reply_handler"](json.dumps(list(reversed(json.loads(fresh_raw)))))
    assert "Observed at: 1005.0;" in tab.model.item(row_index(tab, "work"), 4).toolTip()
    callbacks["reply_handler"](raw)
    assert "Observed at: 1005.0;" in tab.model.item(row_index(tab, "work"), 4).toolTip()
    assert tab._selected_row()["name"] == "work"
    with pytest.raises(sm.SiloBusy):
        store.delete("work")
    tab._refresh_observations()
    outdated, outdated_raw, _ = proxy.requests[2]
    outdated_rows = json.loads(outdated_raw)
    for row in outdated_rows:
        if row["name"] == "work":
            row["operation_generation"] -= 1
    outdated["reply_handler"](json.dumps(outdated_rows))
    assert tab.model.item(row_index(tab, "work"), 4).text() == "unknown"
    assert tab._selected_row()["name"] == "work"
    assert tab.model.item(row_index(tab, "work"), 2).text() == "Active"


def test_lifecycle_event_invalidates_reply_and_malformed_or_owner_loss_is_unknown(real_async_tab):
    import dbus

    tab, bridge, proxy, store, clock = real_async_tab
    tab.table.selectRow(row_index(tab, "work"))
    tab._refresh_observations()
    callbacks, raw, pending = proxy.requests[0]
    store.freeze("work")
    bridge._on_changed("work", "Frozen")
    tab.refresh()
    callbacks["reply_handler"](raw)
    assert tab.model.item(row_index(tab, "work"), 2).text() == "Frozen"
    assert pending.cancelled
    tab._refresh_observations()
    proxy.requests[1][0]["reply_handler"]('[{"name":"work", "observed_ttl_seconds":"bad"}]')
    assert tab.model.item(row_index(tab, "work"), 4).text() == "unknown"
    tab._refresh_observations()
    proxy.requests[2][0]["error_handler"](dbus.DBusException("owner lost"))
    assert tab.model.item(row_index(tab, "work"), 4).text() == "unknown"
    # The same non-introspecting, well-known-name proxy can read the new owner.
    clock.update(mono=105.0, wall=1005.0)
    store.observe_runtime_once()
    tab._refresh_observations()
    fresh, fresh_raw, _ = proxy.requests[3]
    fresh["reply_handler"](fresh_raw)
    assert "Observed at: 1005.0;" in tab.model.item(row_index(tab, "work"), 4).toolTip()
    assert tab._selected_row()["name"] == "work"
