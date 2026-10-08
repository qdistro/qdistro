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


def _mgr(store, monkeypatch):
    """A SessionManager on a fake store with inline idle_add and a
    rubber-stamp admin check — the same shape as the StopSilo tests."""
    mgr = object.__new__(sm.SessionManager)
    mgr.store = store
    mgr._peer_caller = lambda _sender, _conn: {
        "uid": 1000, "pid": 42, "exe": "/bin/test"}
    mgr._require_admin = lambda _sender, _conn: None
    mgr.audit = None
    monkeypatch.setattr(sm.GLib, "idle_add", lambda callback: callback())
    return mgr


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_create_silo_runs_account_work_on_a_worker(monkeypatch, tmp_path):
    """The D-Bus dispatch call must return while useradd is still running."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    entered = threading.Event()
    release = threading.Event()
    replied = threading.Event()
    errors: list[BaseException] = []

    orig_useradd = store._ops.useradd

    def blocked_useradd(name, uid):
        entered.set()
        assert release.wait(5), "test did not release the useradd worker"
        orig_useradd(name, uid)

    store._ops.useradd = blocked_useradd
    mgr = _mgr(store, monkeypatch)

    mgr.CreateSilo("work", 2000, replied.set, errors.append,
                   sender=":1.2", conn=object())

    assert entered.wait(2), "account worker never started"
    assert not replied.is_set(), "reply arrived before useradd completed"
    assert errors == []
    # The decorated method has returned while useradd is blocked — the
    # dispatch thread is free, and the store lock is NOT held either:
    assert store.list_silos() == []
    release.set()
    assert replied.wait(2), "successful create did not reply"
    assert errors == []
    assert store.get("work").state == sm.State.CREATED


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_delete_silo_runs_teardown_on_a_worker(monkeypatch, tmp_path):
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    entered = threading.Event()
    release = threading.Event()
    replied = threading.Event()
    errors: list[BaseException] = []

    orig_userdel = store._ops.userdel

    def blocked_userdel(name):
        entered.set()
        assert release.wait(5), "test did not release the userdel worker"
        orig_userdel(name)

    store._ops.userdel = blocked_userdel
    mgr = _mgr(store, monkeypatch)

    mgr.DeleteSilo("work", replied.set, errors.append,
                   sender=":1.2", conn=object())

    assert entered.wait(2), "teardown worker never started"
    assert not replied.is_set(), "reply arrived before userdel completed"
    assert errors == []
    # The row is mid-teardown but listing is not parked behind userdel.
    assert [s.name for s in store.list_silos()] == ["work"]
    release.set()
    assert replied.wait(2), "successful delete did not reply"
    assert errors == []
    with pytest.raises(sm.UnknownSilo):
        store.get("work")


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_create_silo_session_error_reaches_typed_dbus_error(
        monkeypatch, tmp_path):
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    mgr = _mgr(store, monkeypatch)
    replied = threading.Event()
    errored = threading.Event()
    errors: list[BaseException] = []

    def on_error(exc):
        errors.append(exc)
        errored.set()

    mgr.CreateSilo("work", 2001, replied.set, on_error,
                   sender=":1.2", conn=object())

    assert errored.wait(2), "SiloExists never reached the error callback"
    assert not replied.is_set()
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == (
        f"{sm.BUS_NAME}.SiloExists")


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_create_silo_unexpected_error_maps_to_generic(
        monkeypatch, tmp_path):
    """store.create() deliberately re-raises the original class (OSError
    included); the bus boundary must translate it to Generic, not strand
    the async caller."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")

    def fail_useradd(name, uid):
        raise OSError("disk full")

    store._ops.useradd = fail_useradd
    mgr = _mgr(store, monkeypatch)
    replied = threading.Event()
    errored = threading.Event()
    errors: list[BaseException] = []

    def on_error(exc):
        errors.append(exc)
        errored.set()

    mgr.CreateSilo("work", 2000, replied.set, on_error,
                   sender=":1.2", conn=object())

    assert errored.wait(2), "worker failure never reached the error callback"
    assert not replied.is_set()
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.Generic"
    assert "disk full" in str(errors[0])
    # The in-flight reservation is released on every failure path.
    assert store._creating_inflight == {}


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_create_silo_auth_refusal_is_audited_and_never_reaches_store(
        monkeypatch, tmp_path):
    """A non-admin caller must get the NotAuthorized error and leave an
    audit row — and the refusal happens on the worker (peer lookup is a
    D-Bus round trip that must not park the dispatch thread), so the
    store is never touched."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    records = []

    class Audit:
        def record(self, action, name, **kw):
            records.append((action, name, kw))

    mgr = _mgr(store, monkeypatch)
    mgr.audit = Audit()
    store_created = threading.Event()

    def create_spy(*a, **kw):
        store_created.set()

    mgr.store.create = create_spy

    def refuse(_sender, _conn):
        raise sm.NotAuthorized("caller uid 4242 is not ADMIN_UID=1000")

    mgr._require_admin = refuse
    replied = threading.Event()
    errored = threading.Event()
    errors: list[BaseException] = []

    def on_error(exc):
        errors.append(exc)
        errored.set()

    mgr.CreateSilo("work", 2000, replied.set, on_error,
                   sender=":1.9", conn=object())

    assert errored.wait(2), "refusal never reached the error callback"
    assert not replied.is_set()
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.NotAuthorized"
    assert not store_created.wait(0.2), "store.create ran despite refusal"
    assert records and records[0][0] == "create"
    assert records[0][2]["decision"] == "deny"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_duplicate_create_fails_fast_while_first_is_inflight(
        monkeypatch, tmp_path):
    """A second create of the same name must be refused with SiloExists
    immediately — not park on _accounts_lock behind the first useradd."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    entered = threading.Event()
    release = threading.Event()
    orig_useradd = store._ops.useradd

    def blocked_useradd(name, uid):
        entered.set()
        assert release.wait(5), "test did not release the useradd worker"
        orig_useradd(name, uid)

    store._ops.useradd = blocked_useradd
    mgr = _mgr(store, monkeypatch)

    def reply_err(pair):
        event, errors = threading.Event(), []

        def on_error(exc):
            errors.append(exc)
            event.set()
        return event, errors, on_error

    first_reply, first_errors = threading.Event(), []
    mgr.CreateSilo("work", 2000, first_reply.set, first_errors.append,
                   sender=":1.2", conn=object())
    assert entered.wait(2), "first create never reached useradd"

    second_err, second_errors, on_error2 = reply_err(None)
    second_reply = threading.Event()
    mgr.CreateSilo("work", 2001, second_reply.set, on_error2,
                   sender=":1.2", conn=object())
    assert second_err.wait(2), "duplicate create never errored"
    assert not second_reply.is_set()
    assert len(second_errors) == 1
    assert second_errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloExists"

    # A delete racing the pending create is busy, not "unknown silo".
    del_err, del_errors, on_error3 = reply_err(None)
    del_reply = threading.Event()
    mgr.DeleteSilo("work", del_reply.set, on_error3,
                   sender=":1.2", conn=object())
    assert del_err.wait(2), "racing delete never errored"
    assert not del_reply.is_set()
    assert len(del_errors) == 1
    assert del_errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"

    release.set()
    assert first_reply.wait(2)
    assert first_errors == []
    assert store._creating_inflight == {}


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_creating_inflight_cleared_when_account_setup_raises(
        monkeypatch, tmp_path):
    """Every failure path out of the account transaction must drop the
    in-flight reservation — a stuck entry would wedge the name forever."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")

    def fail_relay(name, uid, reload=True):
        raise OSError("policy dir readonly")

    store._ops.write_relay_policy = fail_relay
    mgr = _mgr(store, monkeypatch)
    replied = threading.Event()
    errored = threading.Event()
    errors: list[BaseException] = []

    def on_error(exc):
        errors.append(exc)
        errored.set()

    mgr.CreateSilo("work", 2000, replied.set, on_error,
                   sender=":1.2", conn=object())

    assert errored.wait(2), "relay failure never reached the error callback"
    assert not replied.is_set()
    assert len(errors) == 1
    assert store._creating_inflight == {}
    assert "work" not in store._silos


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_start_body_does_not_hold_store_lock(monkeypatch, tmp_path):
    """A launch parked inside systemctl_start must not wedge _lock: a
    concurrent ListSilos (which needs _lock) has to dispatch while the
    launch body runs on its worker."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    entered = threading.Event()
    release = threading.Event()

    orig_start = store._ops.systemctl_start

    def blocked_start(unit):
        entered.set()
        assert release.wait(5), "test did not release the start worker"
        orig_start(unit)

    store._ops.systemctl_start = blocked_start
    mgr = _mgr(store, monkeypatch)
    replied = threading.Event()
    errored = threading.Event()
    errors: list[BaseException] = []

    def on_error(exc):
        errors.append(exc)
        errored.set()

    mgr.StartSilo("work", replied.set, on_error,
                  sender=":1.2", conn=object())

    assert entered.wait(2), "launch worker never reached systemctl_start"
    # _lock is free: a listing from another dispatch answers while the
    # start is still parked.
    assert [s.name for s in store.list_silos()] == ["work"]
    assert not replied.is_set()
    release.set()
    assert replied.wait(2), "start never replied"
    assert errors == []
    assert store.get("work").state == sm.State.ACTIVE


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_start_failure_releases_inflight_and_rolls_back(
        monkeypatch, tmp_path):
    """A failed launch must drop the in-flight claim and leave the row
    STOPPED — a stuck claim would wedge every later lifecycle op."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)

    def fail_start(unit):
        raise OSError("unit failed")

    store._ops.systemctl_start = fail_start
    mgr = _mgr(store, monkeypatch)
    errored = threading.Event()
    errors: list[BaseException] = []

    def on_error(exc):
        errors.append(exc)
        errored.set()

    mgr.StartSilo("work", threading.Event().set, on_error,
                  sender=":1.2", conn=object())

    assert errored.wait(2), "failed start never errored"
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.Generic"
    assert store.get("work").state == sm.State.STOPPED
    assert "work" not in store._stopping_inflight


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_peer_lookup_runs_on_the_worker_not_the_dispatch_thread(
        monkeypatch, tmp_path):
    """_peer_caller/_require_admin are synchronous D-Bus round trips —
    they must run inside the worker, or a slow bus stalls dispatch."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    entered = threading.Event()
    release = threading.Event()
    replied = threading.Event()
    errors: list[BaseException] = []

    mgr = _mgr(store, monkeypatch)

    def blocked_peer(_sender, _conn):
        entered.set()
        assert release.wait(5), "test did not release the peer lookup"
        return {"uid": 1000, "pid": 42, "exe": "/bin/test"}

    mgr._peer_caller = blocked_peer

    mgr.StartSilo("work", replied.set, errors.append,
                  sender=":1.2", conn=object())

    # The decorated method already returned; the worker is parked in the
    # peer lookup and the dispatch thread is free.
    assert entered.wait(2), "worker never reached the peer lookup"
    assert not replied.is_set()
    release.set()
    assert replied.wait(2), "start never replied"
    assert errors == []


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_observer_does_not_publish_during_an_inflight_start(
        monkeypatch, tmp_path):
    """A runtime probe taken while a launch body runs lock-free reads the
    pre-launch world (unit not started → "stopped"); the commit must treat
    the in-flight claim as a stale verdict and publish nothing."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    entered = threading.Event()
    release = threading.Event()
    orig_start = store._ops.systemctl_start

    def blocked_start(unit):
        entered.set()
        assert release.wait(5), "test did not release the start worker"
        orig_start(unit)

    store._ops.systemctl_start = blocked_start
    worker = threading.Thread(
        target=store.start, args=("work",), daemon=True)
    worker.start()

    assert entered.wait(2), "launch never reached systemctl_start"
    assert "work" in store._stopping_inflight
    store.observe_runtime_once()
    silo = store.get("work")
    assert silo.state == sm.State.ACTIVE
    assert silo.observed_status == "unknown", (
        "observer published a verdict over a launch still in flight")

    release.set()
    worker.join(5)
    assert not worker.is_alive()
    # After the claim clears, the next pass publishes the real verdict.
    store.observe_runtime_once()
    assert store.get("work").observed_status == "launcher-running"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_inflight_clear_only_releases_its_own_claim(tmp_path):
    """A clear that cannot prove ownership must be a no-op: between an
    early clear and a finally backstop a woken waiter can re-claim the
    slot, and the backstop must not drop THAT claim."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    with store._lock:
        mine = store._claim_stop_inflight("work")
        foreign = object()
        store._clear_stop_inflight("work", foreign)
        assert "work" in store._stopping_inflight
        store._clear_stop_inflight("work", mine)
        assert "work" not in store._stopping_inflight
