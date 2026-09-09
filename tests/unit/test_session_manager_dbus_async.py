"""D-Bus responsiveness tests for asynchronous silo teardown."""

from __future__ import annotations

import threading

import pytest

sm = pytest.importorskip("qdistro_session_manager")
from test_session_manager import _FakeOps  # noqa: E402


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_stop_silo_returns_dispatch_thread_while_teardown_runs(monkeypatch):
    """ListSilos must remain dispatchable during the Stopping grace window."""
    entered = threading.Event()
    release = threading.Event()
    replied = threading.Event()
    errors: list[BaseException] = []

    class Store:
        def stop(self, name, grace, caller=None):
            assert (name, grace, caller) == (
                "work", 30, {"uid": 1000, "pid": 42, "exe": "/bin/test"})
            entered.set()
            assert release.wait(2), "test did not release teardown worker"

    mgr = object.__new__(sm.SessionManager)
    mgr.store = Store()
    mgr._peer_caller = lambda _sender, _conn: {
        "uid": 1000, "pid": 42, "exe": "/bin/test"}
    mgr._require_admin = lambda _sender, _conn: None
    monkeypatch.setattr(sm.GLib, "idle_add", lambda callback: callback())

    mgr.StopSilo("work", 30, replied.set, errors.append,
                 sender=":1.2", conn=object())

    assert entered.wait(1), "teardown worker never started"
    assert not replied.is_set(), "reply arrived before teardown completed"
    assert errors == []
    # The decorated method has returned here while Store.stop remains blocked;
    # the GLib D-Bus thread is therefore free to dispatch ListSilos.
    release.set()
    assert replied.wait(1), "successful teardown did not reply"
    assert errors == []


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_worker_store_signal_is_marshaled_to_glib_thread(monkeypatch):
    queued = []
    emitted = []
    mgr = object.__new__(sm.SessionManager)
    mgr.SiloChanged = lambda name, state: emitted.append((name, state))
    monkeypatch.setattr(sm.GLib, "idle_add", lambda callback: queued.append(callback))

    worker = threading.Thread(
        target=mgr._emit_changed, args=("work", "Stopping"))
    worker.start()
    worker.join(1)

    assert emitted == [], "worker emitted on the D-Bus connection directly"
    assert len(queued) == 1
    assert queued[0]() is False
    assert emitted == [("work", "Stopping")]


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_unexpected_store_oserror_replies_with_generic_dbus_error(
        monkeypatch, tmp_path):
    """A silos.yaml write failure must not strand the async caller."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    store.start("work")

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", fail_save)
    monkeypatch.setattr(sm.GLib, "idle_add", lambda callback: callback())

    mgr = object.__new__(sm.SessionManager)
    mgr.store = store
    mgr._peer_caller = lambda _sender, _conn: {
        "uid": 1000, "pid": 42, "exe": "/bin/test"}
    mgr._require_admin = lambda _sender, _conn: None
    replied = threading.Event()
    error_called = threading.Event()
    errors = []

    def on_error(exc):
        errors.append(exc)
        error_called.set()

    mgr.StopSilo("work", 30, replied.set, on_error,
                 sender=":1.2", conn=object())

    assert error_called.wait(1), "unexpected worker failure never replied"
    assert not replied.is_set()
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.Failed"
    assert "disk full" in str(errors[0])
    assert store.get("work").state == sm.State.ACTIVE
