"""D-Bus responsiveness tests for asynchronous silo teardown."""

from __future__ import annotations

import json
import queue
import threading
import time

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
    mgr._offload_work_q = queue.Queue(maxsize=sm._OFFLOAD_QUEUE_MAX)
    for _w in range(2):
        threading.Thread(target=mgr._offload_worker_loop,
                         daemon=True).start()
    mgr._offload_pending = 0
    mgr._offload_pending_lock = threading.Lock()
    mgr._offload_active = 0
    mgr._offload_delivering = 0
    mgr._offload_drained = False
    mgr._offload_active_cv = threading.Condition(
        mgr._offload_pending_lock)
    mgr._offload_answers = {}
    mgr._stopping = False
    mgr._refusal_audit_q = queue.Queue(maxsize=64)
    threading.Thread(target=mgr._refusal_audit_writer,
                     daemon=True).start()
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
    mgr._offload_work_q = queue.Queue(maxsize=sm._OFFLOAD_QUEUE_MAX)
    for _w in range(2):
        threading.Thread(target=mgr._offload_worker_loop,
                         daemon=True).start()
    mgr._offload_pending = 0
    mgr._offload_pending_lock = threading.Lock()
    mgr._offload_active = 0
    mgr._offload_delivering = 0
    mgr._offload_drained = False
    mgr._offload_active_cv = threading.Condition(
        mgr._offload_pending_lock)
    mgr._offload_answers = {}
    mgr._stopping = False
    mgr._refusal_audit_q = queue.Queue(maxsize=64)
    threading.Thread(target=mgr._refusal_audit_writer,
                     daemon=True).start()
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
    mgr._offload_work_q = queue.Queue(maxsize=sm._OFFLOAD_QUEUE_MAX)
    for _w in range(2):
        threading.Thread(target=mgr._offload_worker_loop,
                         daemon=True).start()
    mgr._offload_pending = 0
    mgr._offload_pending_reads = 0
    mgr._offload_pending_lock = threading.Lock()
    mgr._offload_active = 0
    mgr._offload_delivering = 0
    mgr._offload_drained = False
    mgr._offload_active_cv = threading.Condition(
        mgr._offload_pending_lock)
    mgr._offload_answers = {}
    mgr._stopping = False
    mgr._refusal_audit_q = queue.Queue(maxsize=64)
    threading.Thread(target=mgr._refusal_audit_writer,
                     daemon=True).start()
    mgr._peer_caller = lambda _sender, _conn: {
        "uid": 1000, "pid": 42, "exe": "/bin/test"}
    mgr._require_admin = lambda _sender, _conn: None
    mgr._peer_uid = lambda _sender, _conn: sm.ADMIN_UID
    mgr.audit = None
    monkeypatch.setattr(sm.GLib, "idle_add", lambda callback: callback())
    # No GLib main loop runs in tests; dispatch the queue deadline on a
    # daemon Timer with the same one-shot, honour-the-interval semantics.
    def _timeout_add(ms, fn, *args):
        t = threading.Timer(ms / 1000.0, lambda: fn(*args))
        t.daemon = True
        t.start()
        return 0
    monkeypatch.setattr(sm.GLib, "timeout_add", _timeout_add)
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
        # Replay the bug: A claims, clears early, a woken waiter B
        # re-claims — then A's finally backstop runs with the stale
        # token and must leave B's claim alone.
        old = store._claim_stop_inflight("work")
        store._clear_stop_inflight("work", old)
        new = store._claim_stop_inflight("work")
        store._clear_stop_inflight("work", old)
        assert store._stopping_inflight.get("work") is new
        store._clear_stop_inflight("work", new)
        assert "work" not in store._stopping_inflight


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_completed_start_invalidates_a_probe_taken_mid_launch(tmp_path):
    """A probe that snapshotted generation N while the launch body ran
    lock-free reads the pre-start world ("stopped"). If its result lands
    after the launcher reports success, the commit must be dropped:
    _run_start_body bumps operation_generation under _lock before
    releasing the claim, so the late verdict is stale by generation."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)

    start_entered = threading.Event()
    start_release = threading.Event()
    orig_start = store._ops.systemctl_start

    def blocked_start(unit):
        start_entered.set()
        assert start_release.wait(5), "test did not release the start"
        orig_start(unit)

    store._ops.systemctl_start = blocked_start
    worker = threading.Thread(
        target=store.start, args=("work",), daemon=True)
    worker.start()
    assert start_entered.wait(2), "launch never reached systemctl_start"
    assert "work" in store._stopping_inflight

    # The observer snapshots the post-claim generation and probes while
    # the launcher is still parked — capture that stale verdict, then
    # hold its return until the launch has fully completed.
    probe_captured = threading.Event()
    probe_release = threading.Event()
    orig_probe = store._ops.observe_silo

    def mid_launch_probe(name, uid, kind):
        verdict = orig_probe(name, uid, kind)
        probe_captured.set()
        assert probe_release.wait(5), "test did not release the probe"
        return verdict

    store._ops.observe_silo = mid_launch_probe
    observer = threading.Thread(
        target=store.observe_runtime_once, daemon=True)
    observer.start()
    assert probe_captured.wait(2), "observer never probed"

    start_release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert "work" not in store._stopping_inflight

    probe_release.set()
    observer.join(5)
    assert not observer.is_alive()

    silo = store.get("work")
    assert silo.observed_status == "unknown", (
        "a probe taken mid-launch committed its stale 'stopped' verdict "
        "after the launch had already succeeded")
    store.observe_runtime_once()
    assert store.get("work").observed_status == "launcher-running"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_admission_refuses_beyond_queue_max(monkeypatch, tmp_path):
    """Once _OFFLOAD_QUEUE_MAX ops are outstanding, the dispatch thread
    answers SiloBusy immediately — the mutation is never submitted and
    the store is never touched."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    mgr._offload_pending = sm._OFFLOAD_QUEUE_MAX
    store_touched = threading.Event()

    def create_spy(*a, **kw):
        store_touched.set()

    mgr.store.create = create_spy
    replied = threading.Event()
    errored = threading.Event()
    errors: list = []

    def on_error(exc):
        errors.append(exc)
        errored.set()

    mgr.CreateSilo("work", 2000, replied.set, on_error,
                   sender=":1.2", conn=object())

    assert errored.wait(1), "saturated pool never answered Busy"
    assert not replied.is_set()
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    assert not store_touched.wait(0.2), "refused op still reached the store"
    # The refused call released its slot — the counter is unchanged.
    assert mgr._offload_pending == sm._OFFLOAD_QUEUE_MAX


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_stale_queued_op_answers_busy_instead_of_running(
        monkeypatch, tmp_path):
    """The queue deadline fires whether or not a worker is free: an op
    stuck behind occupied workers gets its SiloBusy at the deadline —
    before the caller's D-Bus timeout — and the worker that eventually
    picks it up runs nothing."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    monkeypatch.setattr(sm, "_OFFLOAD_QUEUE_STALE_S", 0.2)

    # Occupy both pool workers so the lifecycle op stays queued.
    occupied = [threading.Event(), threading.Event()]
    release = threading.Event()

    def park(ev):
        def work():
            ev.set()
            assert release.wait(5), "test did not release the workers"
            return ()
        return work

    for ev in occupied:
        mgr._run_offloaded("blocker", park(ev),
                           threading.Event().set, lambda e: None)
    assert all(ev.wait(2) for ev in occupied), "workers never started"

    store_touched = threading.Event()
    mgr.store.start = lambda *a, **kw: store_touched.set()
    replied = threading.Event()
    errored = threading.Event()
    errors: list = []

    def on_error(exc):
        errors.append(exc)
        errored.set()

    mgr.StartSilo("work", replied.set, on_error,
                  sender=":1.2", conn=object())

    # The Busy answer arrives at the queue deadline, while every worker
    # is still parked — not at the D-Bus timeout and not when a worker
    # eventually frees.
    assert errored.wait(2), "queued op got no answer at its deadline"
    assert not replied.is_set()
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"

    release.set()
    # The expired task is still dequeued (clearing its admission slot)
    # but must not run the mutation the caller already timed out on.
    for _ in range(100):
        with mgr._offload_pending_lock:
            if mgr._offload_pending == 0:
                break
        threading.Event().wait(0.02)
    assert mgr._offload_pending == 0
    assert not store_touched.is_set(), "expired op still ran the mutation"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_enqueue_failure_never_runs_the_mutation(
        monkeypatch, tmp_path):
    """A queue.Full on enqueue still answers exactly one Busy; if a
    worker somehow runs the captured item later, the mutation never
    runs and the slot is released only once."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    enqueued = []

    def full_enqueue(item):
        enqueued.append(item)
        raise queue.Full()

    monkeypatch.setattr(mgr, "_offload_enqueue", full_enqueue)
    store_touched = threading.Event()
    mgr.store.create = lambda *a, **kw: store_touched.set()
    replied = threading.Event()
    errored = threading.Event()
    errors: list = []

    mgr.CreateSilo("work", 2000, replied.set,
                   lambda e: (errors.append(e), errored.set()),
                   sender=":1.2", conn=object())

    assert errored.wait(1), "submit failure never answered Busy"
    assert not replied.is_set()
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    # The guarded half of the captured pair must no-op when a worker
    # eventually runs it — and must not decrement the slot a 2nd time.
    assert len(enqueued) == 1
    enqueued[0][0]()
    assert not store_touched.is_set(), "refused op still ran the mutation"
    assert mgr._offload_pending == 0


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_admission_refusal_is_audited(monkeypatch, tmp_path):
    """A dispatch-thread refusal never reaches the worker's peer lookup,
    so it owes the audit trail a deny row with caller=None."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    records = []

    class Audit:
        def record(self, action, name, **kw):
            records.append((action, name, kw))

    mgr.audit = Audit()
    mgr._offload_pending = sm._OFFLOAD_QUEUE_MAX
    errored = threading.Event()
    errors: list = []

    mgr.CreateSilo("work", 2000, threading.Event().set,
                   lambda e: (errors.append(e), errored.set()),
                   sender=":1.2", conn=object())

    assert errored.wait(1)
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    # The row lands asynchronously on the refusal-audit writer thread.
    for _ in range(100):
        if records:
            break
        threading.Event().wait(0.02)
    assert records and records[0][0] == "create"
    assert records[0][1] == "work"
    assert records[0][2]["decision"] == "deny"
    assert records[0][2]["caller"] is None


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_shutdown_settles_queued_ops(monkeypatch, tmp_path):
    """Queued ops cancelled at shutdown get a direct Busy answer and an
    audit row, release their admission slot via the done callback, and
    never run the mutation."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    records = []

    class Audit:
        def record(self, action, name, **kw):
            records.append((action, name, kw))

    mgr.audit = Audit()

    # Park both workers so the create stays queued.
    occupied = [threading.Event(), threading.Event()]
    release = threading.Event()

    def park(ev):
        def work():
            ev.set()
            assert release.wait(5), "test did not release the workers"
            return ()
        return work

    for ev in occupied:
        mgr._run_offloaded("blocker", park(ev),
                           threading.Event().set, lambda e: None)
    assert all(ev.wait(2) for ev in occupied), "workers never started"

    store_touched = threading.Event()
    mgr.store.create = lambda *a, **kw: store_touched.set()
    replied = threading.Event()
    errors: list = []
    mgr.CreateSilo("work", 2000, replied.set, errors.append,
                   sender=":1.2", conn=object())

    # main()'s shutdown sequence: stop admissions, then drain the work
    # queue and settle each never-started item (running blockers were
    # already dequeued by workers, so only the create remains queued).
    with mgr._offload_pending_lock:
        mgr._stopping = True
    settled = 0
    while True:
        try:
            _g, settle = mgr._offload_work_q.get_nowait()
        except queue.Empty:
            break
        settle()
        settled += 1
    release.set()

    assert settled == 1, "only the queued create should have settled"
    for _ in range(100):
        with mgr._offload_pending_lock:
            if mgr._offload_pending == 0:
                break
        threading.Event().wait(0.02)
    assert mgr._offload_pending == 0
    assert not replied.is_set()
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    assert not store_touched.is_set(), "settled op still ran the mutation"
    for _ in range(100):
        if records:
            break
        threading.Event().wait(0.02)
    assert any(r[0] == "create" and r[2]["decision"] == "deny"
               for r in records), "shutdown settle left no audit row"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_submit_raises_after_worker_already_started(
        monkeypatch, tmp_path):
    """submit() can enqueue the callable, let a running worker consume
    it, and THEN raise RuntimeError on thread creation. The started op
    owns the reply: no Busy next to the mutation, one answer total."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    done = threading.Event()
    store_touched = threading.Event()
    mgr.store.create = lambda *a, **kw: store_touched.set()

    def racing_enqueue(item):
        guarded, _settle = item
        t = threading.Thread(target=lambda: (guarded(), done.set()),
                             daemon=True)
        t.start()
        # Wait until the stray callable has claimed `started` and
        # entered work() so the enqueue-failure handler deterministically
        # observes a started op.
        assert store_touched.wait(2), "stray task never reached work()"
        raise queue.Full()

    monkeypatch.setattr(mgr, "_offload_enqueue", racing_enqueue)
    replied = threading.Event()
    errors: list = []

    mgr.CreateSilo("work", 2000, replied.set, errors.append,
                   sender=":1.2", conn=object())

    # The "leaked" task runs the mutation and replies; the submit-failure
    # path must NOT add a Busy on top of it.
    assert done.wait(2), "leaked task never ran"
    assert store_touched.is_set()
    assert replied.wait(1), "started op never replied"
    assert errors == [], "a Busy was answered next to the started op"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_worker_dequeued_after_stopping_answers_itself(
        monkeypatch, tmp_path):
    """The shutdown-cancel race: a queued task whose Future went running
    before main could cancel it must answer its own refusal — direct,
    audited — instead of silently returning."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    records = []

    class Audit:
        def record(self, action, name, **kw):
            records.append((action, name, kw))

    mgr.audit = Audit()

    occupied = [threading.Event(), threading.Event()]
    release = threading.Event()

    def park(ev):
        def work():
            ev.set()
            assert release.wait(5), "test did not release the workers"
            return ()
        return work

    for ev in occupied:
        mgr._run_offloaded("blocker", park(ev),
                           threading.Event().set, lambda e: None)
    assert all(ev.wait(2) for ev in occupied), "workers never started"

    store_touched = threading.Event()
    mgr.store.create = lambda *a, **kw: store_touched.set()
    replied = threading.Event()
    errors: list = []
    mgr.CreateSilo("work", 2000, replied.set, errors.append,
                   sender=":1.2", conn=object())

    # Shutdown begins while the create is still queued, but nothing
    # cancels it — the worker that dequeues it must answer instead.
    mgr._stopping = True
    release.set()

    deadline = threading.Event()
    for _ in range(100):
        if errors:
            break
        deadline.wait(0.02)
    assert len(errors) == 1, "stopping worker never answered"
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    assert not replied.is_set()
    assert not store_touched.is_set(), "stopping op still ran the mutation"
    for _ in range(100):
        if records:
            break
        deadline.wait(0.02)
    assert any(r[0] == "create" and r[2]["decision"] == "deny"
               for r in records), "stopping refusal left no audit row"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_expired_busy_answer_is_drained_when_loop_quits(
        monkeypatch, tmp_path):
    """A queue-deadline Busy scheduled via idle_add that never dispatches
    (the loop quit first) must still reach the caller: the shutdown drain
    delivers the pending answer exactly once."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    # Loop "dies": idle callbacks are captured but never dispatched.
    pending_idle: list = []
    monkeypatch.setattr(sm.GLib, "idle_add",
                        lambda cb: pending_idle.append(cb))
    deadlines: list = []
    monkeypatch.setattr(sm.GLib, "timeout_add",
                        lambda ms, fn, *a: deadlines.append(fn))

    occupied = [threading.Event(), threading.Event()]
    release = threading.Event()

    def park(ev):
        def work():
            ev.set()
            assert release.wait(5), "test did not release the workers"
            return ()
        return work

    for ev in occupied:
        mgr._run_offloaded("blocker", park(ev),
                           threading.Event().set, lambda e: None)
    assert all(ev.wait(2) for ev in occupied), "workers never started"

    store_touched = threading.Event()
    mgr.store.create = lambda *a, **kw: store_touched.set()
    replied = threading.Event()
    errors: list = []
    mgr.CreateSilo("work", 2000, replied.set, errors.append,
                   sender=":1.2", conn=object())

    # The queued create crosses its deadline: the Busy decision is made
    # and its delivery is scheduled — but the loop dies before dispatch.
    assert deadlines, "queue deadline was never armed"
    deadlines[-1]()
    assert pending_idle, "deadline refusal never scheduled an answer"
    assert errors == [] and not replied.is_set()

    # main()'s teardown: drain queued work items (settle each), then
    # drain answers that were scheduled but never dispatched.
    with mgr._offload_pending_lock:
        mgr._stopping = True
    while True:
        try:
            _g, settle = mgr._offload_work_q.get_nowait()
        except queue.Empty:
            break
        settle()
    with mgr._offload_pending_lock:
        drains = list(mgr._offload_answers.values())
    for drain in drains:
        drain()
    release.set()

    assert len(errors) == 1, "scheduled Busy was lost when the loop quit"
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    assert not replied.is_set()
    assert not store_touched.is_set()
    # The parked blockers unregister once their post-release replies land.
    for _ in range(100):
        with mgr._offload_pending_lock:
            if not mgr._offload_answers:
                break
        threading.Event().wait(0.02)
    with mgr._offload_pending_lock:
        assert mgr._offload_answers == {}, "answered op left registered"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_completed_reply_is_drained_when_loop_quits(
        monkeypatch, tmp_path):
    """A worker that finishes its mutation and schedules the reply just
    before the loop quits: shutdown must deliver that reply, not drop
    it — the mutation already landed, so silence is not an option."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    pending_idle: list = []
    monkeypatch.setattr(sm.GLib, "idle_add",
                        lambda cb: pending_idle.append(cb))

    replies: list = []
    errors: list = []
    mgr.CreateSilo("work", 2000, lambda *a: replies.append(a),
                   errors.append, sender=":1.2", conn=object())

    # Wait until the worker finished the mutation and queued the reply.
    for _ in range(100):
        if pending_idle:
            break
        threading.Event().wait(0.02)
    assert pending_idle, "worker never scheduled its reply"
    assert store.get("work") is not None, "mutation did not run"
    assert replies == [], "reply dispatched before the loop could run it"

    # The loop quits now; the drain must deliver the pending reply.
    with mgr._offload_pending_lock:
        mgr._stopping = True
    while True:
        try:
            _g, settle = mgr._offload_work_q.get_nowait()
        except queue.Empty:
            break
        settle()
    with mgr._offload_pending_lock:
        drains = list(mgr._offload_answers.values())
    for drain in drains:
        drain()

    assert len(replies) == 1, "completed reply was dropped at shutdown"
    assert errors == []
    with mgr._offload_pending_lock:
        assert mgr._offload_answers == {}, "answered op left registered"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_answer_registry_stays_bounded_under_overload(
        monkeypatch, tmp_path):
    """Retained answers share the admission bound: refused calls are
    answered inline (no registry entry, no idle source), and admitted
    ops keep their slot until the answer is delivered — so a starved
    loop cannot accumulate undelivered answers past the queue cap."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    # The loop never dispatches: every delivered-via-idle answer is
    # retained instead.
    pending_idle: list = []
    monkeypatch.setattr(sm.GLib, "idle_add",
                        lambda cb: pending_idle.append(cb))

    replies: list = []
    errors: list = []
    for i in range(sm._OFFLOAD_QUEUE_MAX):
        mgr._run_offloaded(f"op-{i}", lambda: (),
                           lambda *a: replies.append(a), errors.append)
    with mgr._offload_pending_lock:
        assert mgr._offload_pending == sm._OFFLOAD_QUEUE_MAX
        assert len(mgr._offload_answers) == sm._OFFLOAD_QUEUE_MAX

    # Every additional call is refused inline — immediately, without
    # retaining an answer-registration or an idle source.
    for _ in range(200):
        mgr._run_offloaded("refused", lambda: (),
                           threading.Event().set, errors.append)
    assert len(errors) == 200
    assert not pending_idle or all(True for _ in pending_idle)
    with mgr._offload_pending_lock:
        assert len(mgr._offload_answers) == sm._OFFLOAD_QUEUE_MAX, \
            "refusals grew the answer registry"
        assert mgr._offload_pending == sm._OFFLOAD_QUEUE_MAX

    # The workers finish and schedule the admitted ops' replies.
    for _ in range(100):
        if len(pending_idle) == sm._OFFLOAD_QUEUE_MAX:
            break
        threading.Event().wait(0.02)
    assert len(pending_idle) == sm._OFFLOAD_QUEUE_MAX

    # Dispatching the retained answers releases every slot — delivery,
    # not task completion, frees the admission budget.
    for cb in pending_idle:
        cb()
    assert len(replies) == sm._OFFLOAD_QUEUE_MAX
    with mgr._offload_pending_lock:
        assert mgr._offload_answers == {}
        assert mgr._offload_pending == 0


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_repeated_submit_failures_stay_bounded(
        monkeypatch, tmp_path):
    """A submit() RuntimeError before a worker starts must keep the
    admission slot until its Busy is delivered — during a thread-
    creation outage, repeated failures cannot grow the retained-answer
    backlog past _OFFLOAD_QUEUE_MAX."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    pending_idle: list = []
    monkeypatch.setattr(sm.GLib, "idle_add",
                        lambda cb: pending_idle.append(cb))

    def no_capacity(item):
        raise queue.Full()

    monkeypatch.setattr(mgr, "_offload_enqueue", no_capacity)
    replied = threading.Event()
    errors: list = []
    total = 200
    for i in range(total):
        mgr._run_offloaded(f"op-{i}", lambda: (), replied.set,
                           errors.append)

    # The first _OFFLOAD_QUEUE_MAX calls were admitted and now retain
    # undelivered Busies; every later call was refused inline.
    assert len(errors) == total - sm._OFFLOAD_QUEUE_MAX
    with mgr._offload_pending_lock:
        assert mgr._offload_pending == sm._OFFLOAD_QUEUE_MAX
        assert len(mgr._offload_answers) == sm._OFFLOAD_QUEUE_MAX, \
            "submit failures grew the answer registry"
    assert len(pending_idle) == sm._OFFLOAD_QUEUE_MAX

    # Dispatching the retained Busies releases every slot.
    for cb in list(pending_idle):
        cb()
    assert len(errors) == total
    assert not replied.is_set()
    with mgr._offload_pending_lock:
        assert mgr._offload_pending == 0
        assert mgr._offload_answers == {}
    assert all(e.get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
               for e in errors)


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_expired_ops_do_not_grow_work_backlog(
        monkeypatch, tmp_path):
    """A delivered deadline answer frees the answer slot but not the
    queued work item — the bounded queue caps retained items while the
    workers stay parked, so repeat expire-and-deliver rounds cannot
    grow the executor backlog the way an unbounded pool queue did."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    deadlines: list = []
    monkeypatch.setattr(sm.GLib, "timeout_add",
                        lambda ms, fn, *a: deadlines.append(fn))

    occupied = [threading.Event(), threading.Event()]
    release = threading.Event()

    def park(ev):
        def work():
            ev.set()
            assert release.wait(5), "test did not release the workers"
            return ()
        return work

    for ev in occupied:
        mgr._run_offloaded("blocker", park(ev),
                           threading.Event().set, lambda e: None)
    assert all(ev.wait(2) for ev in occupied), "workers never started"

    errors: list = []
    for i in range(200):
        mgr._run_offloaded(f"op-{i}", lambda: (),
                           threading.Event().set, errors.append)
        # Fire every armed deadline immediately — each expired op is
        # answered Busy and its answer slot released while its work
        # item stays queued behind the parked workers.
        while deadlines:
            deadlines.pop(0)()

    # Retained work items are capped by the bounded queue itself; every
    # request still got exactly one answer.
    assert mgr._offload_work_q.qsize() <= sm._OFFLOAD_QUEUE_MAX
    assert len(errors) == 200
    assert all(e.get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
               for e in errors)

    # Once the workers free, the queued expired items retire without
    # mutating and without touching the pending counter.
    release.set()
    deadline = threading.Event()
    for _ in range(100):
        if mgr._offload_work_q.qsize() == 0:
            break
        deadline.wait(0.02)
    assert mgr._offload_work_q.qsize() == 0
    with mgr._offload_pending_lock:
        assert mgr._offload_pending == 0


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_shutdown_grace_waits_for_running_mutation(
        monkeypatch, tmp_path):
    """A mutation mid-flight when shutdown begins must be allowed to
    finish before teardown proceeds — daemon workers cannot hold
    interpreter exit, so the bounded grace on _offload_active is the
    only thing between a committed transaction and a torn one."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    entered = threading.Event()
    finish = threading.Event()
    store_touched = threading.Event()

    def slow_create(*a, **kw):
        entered.set()
        assert finish.wait(5), "test did not release the mutation"
        store_touched.set()

    mgr.store.create = slow_create
    replies: list = []
    errors: list = []
    mgr.CreateSilo("work", 2000, lambda *a: replies.append(a),
                   errors.append, sender=":1.2", conn=object())
    assert entered.wait(2), "mutation never started"

    # Shutdown begins mid-mutation: stop admissions, drain the (empty)
    # queue, then run the grace wait on a separate thread — it must
    # block until the running op retires.
    with mgr._offload_pending_lock:
        mgr._stopping = True
    while True:
        try:
            _g, settle = mgr._offload_work_q.get_nowait()
        except queue.Empty:
            break
        settle()

    grace_done = threading.Event()

    def grace():
        with mgr._offload_active_cv:
            mgr._offload_active_cv.wait_for(
                lambda: mgr._offload_active == 0, timeout=10)
        grace_done.set()

    threading.Thread(target=grace, daemon=True).start()
    assert not grace_done.wait(0.5), \
        "shutdown proceeded while a mutation was in flight"

    finish.set()
    assert grace_done.wait(2), "running work never retired"
    # Its answer was delivered directly (the loop is down), exactly once.
    assert len(replies) == 1
    assert errors == []
    assert store_touched.is_set()


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_shutdown_grace_is_bounded(monkeypatch, tmp_path):
    """A wedged op must not stall shutdown past the grace bound."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)
    monkeypatch.setattr(sm, "_OFFLOAD_SHUTDOWN_GRACE_S", 0.2)

    wedged = threading.Event()
    mgr.store.create = lambda *a, **kw: wedged.wait(30)
    mgr.CreateSilo("work", 2000, threading.Event().set,
                   lambda e: None, sender=":1.2", conn=object())
    for _ in range(100):
        with mgr._offload_active_cv:
            if mgr._offload_active == 1:
                break
        threading.Event().wait(0.02)

    started_at = time.monotonic()
    with mgr._offload_active_cv:
        drained = mgr._offload_active_cv.wait_for(
            lambda: mgr._offload_active == 0,
            timeout=sm._OFFLOAD_SHUTDOWN_GRACE_S)
    assert not drained, "wedged op falsely reported drained"
    assert time.monotonic() - started_at < 2, \
        "grace wait did not honour its bound"
    wedged.set()


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_dequeued_before_stopping_read_is_refused_not_mutated(
        monkeypatch, tmp_path):
    """A worker that dequeues an item but has not yet read _stopping
    must still be refused: the read and the active-count claim share
    _offload_active_cv with teardown's write, so a shutdown that slips
    in first can never be followed by a mutation."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    parked = threading.Event()
    release_worker = threading.Event()
    real_enqueue = mgr._offload_enqueue

    def parking_enqueue(item):
        guarded, settle = item

        def parked_guarded():
            # Worker holds the dequeued item but is parked before its
            # stopping-read + active-claim section.
            parked.set()
            assert release_worker.wait(5), \
                "test did not release the dequeued worker"
            guarded()

        real_enqueue((parked_guarded, settle))

    monkeypatch.setattr(mgr, "_offload_enqueue", parking_enqueue)
    store_touched = threading.Event()
    mgr.store.create = lambda *a, **kw: store_touched.set()
    replied = threading.Event()
    errors: list = []
    mgr.CreateSilo("work", 2000, replied.set, errors.append,
                   sender=":1.2", conn=object())
    assert parked.wait(2), "worker never dequeued the op"

    # Teardown slips in first: _stopping lands and the grace wait sees
    # an unclaimed counter — the item is consumed, not queued.
    with mgr._offload_pending_lock:
        mgr._stopping = True
    while True:
        try:
            _g, settle = mgr._offload_work_q.get_nowait()
        except queue.Empty:
            break
        settle()
    with mgr._offload_active_cv:
        assert mgr._offload_active_cv.wait_for(
            lambda: mgr._offload_active == 0, timeout=5)

    release_worker.set()
    deadline = threading.Event()
    for _ in range(100):
        if errors:
            break
        deadline.wait(0.02)
    assert len(errors) == 1, "worker dequeued after stopping never answered"
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    assert not replied.is_set()
    assert not store_touched.is_set(), \
        "op mutated after shutdown claimed it"


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_shutdown_settle_does_not_wait_on_audit_sink(
        monkeypatch, tmp_path):
    """A wedged audit sink must not park the shutdown thread: the
    settle path queues its deny row asynchronously, so teardown answers
    and returns while the writer is still blocked."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    audit_blocked = threading.Event()
    audit_release = threading.Event()

    class Audit:
        def record(self, *a, **kw):
            audit_blocked.set()
            audit_release.wait(30)

    mgr.audit = Audit()

    occupied = [threading.Event(), threading.Event()]
    release = threading.Event()

    def park(ev):
        def work():
            ev.set()
            assert release.wait(5), "test did not release the workers"
            return ()
        return work

    for ev in occupied:
        mgr._run_offloaded("blocker", park(ev),
                           threading.Event().set, lambda e: None)
    assert all(ev.wait(2) for ev in occupied)

    errors: list = []
    mgr.CreateSilo("work", 2000, threading.Event().set, errors.append,
                   sender=":1.2", conn=object())

    with mgr._offload_pending_lock:
        mgr._stopping = True
    settled = threading.Event()

    def teardown():
        while True:
            try:
                _g, settle = mgr._offload_work_q.get_nowait()
            except queue.Empty:
                break
            settle()
        settled.set()

    threading.Thread(target=teardown, daemon=True).start()
    assert settled.wait(2), \
        "settle blocked on a wedged audit sink — shutdown would stall"
    assert len(errors) == 1
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    # The deny row still reached the writer — it is queued on the
    # blocked record, not dropped.
    assert audit_blocked.wait(2)
    release.set()
    audit_release.set()


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_dequeued_unclaimed_op_is_answered_by_drain(
        monkeypatch, tmp_path):
    """A worker can consume an item and stall before claiming it: the
    queue drain cannot see it, the grace sees no running count — the
    answer registry must still settle the caller exactly once."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    parked = threading.Event()
    release_worker = threading.Event()
    worker_done = threading.Event()
    real_enqueue = mgr._offload_enqueue

    def parking_enqueue(item):
        guarded, settle = item

        def parked_guarded():
            parked.set()
            assert release_worker.wait(5), \
                "test did not release the dequeued worker"
            try:
                guarded()
            finally:
                worker_done.set()

        real_enqueue((parked_guarded, settle))

    monkeypatch.setattr(mgr, "_offload_enqueue", parking_enqueue)
    store_touched = threading.Event()
    mgr.store.create = lambda *a, **kw: store_touched.set()
    replied = threading.Event()
    errors: list = []
    mgr.CreateSilo("work", 2000, replied.set, errors.append,
                   sender=":1.2", conn=object())
    assert parked.wait(2), "worker never dequeued the op"

    # Teardown with the worker still descheduled: empty queue, no
    # active count — the drain must answer the unclaimed request itself.
    with mgr._offload_pending_lock:
        mgr._stopping = True
    while True:
        try:
            _g, settle = mgr._offload_work_q.get_nowait()
        except queue.Empty:
            break
        settle()
    with mgr._offload_active_cv:
        assert mgr._offload_active_cv.wait_for(
            lambda: mgr._offload_active == 0, timeout=5)
    with mgr._offload_pending_lock:
        drains = list(mgr._offload_answers.values())
    for drain in drains:
        drain()

    assert len(errors) == 1, "dequeued-unclaimed op exited unanswered"
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    assert not replied.is_set()
    assert not store_touched.is_set()

    # When the worker finally runs, its own refusal must not double up.
    release_worker.set()
    assert worker_done.wait(5), "dequeued worker never finished"
    assert len(errors) == 1, "worker answered a second time"
    assert not store_touched.is_set(), \
        "drain-answered op mutated after resume"
    assert mgr._offload_pending == 0
    with mgr._offload_pending_lock:
        assert not mgr._offload_answers


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_claimed_refusal_holds_shutdown_until_delivered(
        monkeypatch, tmp_path):
    """A worker that claimed its shutdown refusal but paused before
    _invoke is neither active nor pending-answer — teardown's wait must
    outlast the in-flight delivery or the caller exits unanswered."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    parked = threading.Event()
    release_worker = threading.Event()
    audit_parked = threading.Event()
    audit_release = threading.Event()
    real_enqueue = mgr._offload_enqueue
    real_async = mgr._audit_refusal_async

    def parking_enqueue(item):
        guarded, settle = item

        def parked_guarded():
            parked.set()
            assert release_worker.wait(5), \
                "test did not release the dequeued worker"
            guarded()

        real_enqueue((parked_guarded, settle))

    def parked_async(*args):
        audit_parked.set()
        assert audit_release.wait(5), "test did not release the audit"
        return real_async(*args)

    monkeypatch.setattr(mgr, "_offload_enqueue", parking_enqueue)
    monkeypatch.setattr(mgr, "_audit_refusal_async", parked_async)
    mgr.store.create = lambda *a, **kw: pytest.fail(
        "stopping op must never run the mutation")
    errors: list = []
    mgr.CreateSilo("work", 2000, threading.Event().set, errors.append,
                   sender=":1.2", conn=object())
    assert parked.wait(2), "worker never dequeued the op"

    with mgr._offload_pending_lock:
        mgr._stopping = True
    release_worker.set()
    # The worker has now claimed `answered` and _offload_delivering and
    # is parked between the claim and _invoke — astra r5's window.
    assert audit_parked.wait(2), "worker never reached the refusal"

    teardown_done = threading.Event()

    def teardown():
        while True:
            try:
                _g, settle = mgr._offload_work_q.get_nowait()
            except queue.Empty:
                break
            settle()
        with mgr._offload_active_cv:
            mgr._offload_active_cv.wait_for(
                lambda: (mgr._offload_active == 0
                         and mgr._offload_delivering == 0),
                timeout=10)
        with mgr._offload_pending_lock:
            drains = list(mgr._offload_answers.values())
        for drain in drains:
            drain()
        with mgr._offload_active_cv:
            mgr._offload_drained = True
            mgr._offload_active_cv.wait_for(
                lambda: mgr._offload_delivering == 0, timeout=10)
        teardown_done.set()

    threading.Thread(target=teardown, daemon=True).start()
    assert mgr._offload_delivering == 1, \
        "parked worker did not hold an in-flight delivery claim"
    assert not teardown_done.wait(0.5), \
        "teardown finished while a claimed refusal was undelivered"
    assert not errors, "answer arrived before the claim finished"

    audit_release.set()
    assert teardown_done.wait(5), "teardown never released the wait"
    assert len(errors) == 1, "claimed refusal never delivered"
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    assert mgr._offload_pending == 0
    assert mgr._offload_delivering == 0


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_claim_after_wait_is_caught_by_drain_fence(
        monkeypatch, tmp_path):
    """A worker dequeued before teardown may claim its refusal in the
    window between the successful zero-counter wait and the registry
    drain. The post-drain fence + delivering wait must outlast it."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    parked = threading.Event()
    release_worker = threading.Event()
    audit_parked = threading.Event()
    audit_release = threading.Event()
    real_enqueue = mgr._offload_enqueue
    real_async = mgr._audit_refusal_async

    def parking_enqueue(item):
        guarded, settle = item

        def parked_guarded():
            parked.set()
            assert release_worker.wait(5), \
                "test did not release the dequeued worker"
            guarded()

        real_enqueue((parked_guarded, settle))

    def parked_async(*args):
        audit_parked.set()
        assert audit_release.wait(5), "test did not release the audit"
        return real_async(*args)

    monkeypatch.setattr(mgr, "_offload_enqueue", parking_enqueue)
    monkeypatch.setattr(mgr, "_audit_refusal_async", parked_async)
    mgr.store.create = lambda *a, **kw: pytest.fail(
        "stopping op must never run the mutation")
    errors: list = []
    mgr.CreateSilo("work", 2000, threading.Event().set, errors.append,
                   sender=":1.2", conn=object())
    assert parked.wait(2), "worker never dequeued the op"

    # Teardown up to the successful zero-counter wait, with the worker
    # still descheduled before its claim.
    with mgr._offload_pending_lock:
        mgr._stopping = True
    while True:
        try:
            _g, settle = mgr._offload_work_q.get_nowait()
        except queue.Empty:
            break
        settle()
    with mgr._offload_active_cv:
        assert mgr._offload_active_cv.wait_for(
            lambda: (mgr._offload_active == 0
                     and mgr._offload_delivering == 0),
            timeout=5), "first wait should pass — worker not yet claimed"

    # The worker now claims the refusal in the wait→drain window and
    # parks before _invoke — sol r14's interleaving.
    release_worker.set()
    assert audit_parked.wait(2), "worker never reached the refusal"

    with mgr._offload_pending_lock:
        drains = list(mgr._offload_answers.values())
    for drain in drains:
        drain()
    assert not errors, "drain must not deliver a claimed refusal twice"

    tail_done = threading.Event()

    def teardown_tail():
        with mgr._offload_active_cv:
            mgr._offload_drained = True
            mgr._offload_active_cv.wait_for(
                lambda: mgr._offload_delivering == 0, timeout=10)
        tail_done.set()

    threading.Thread(target=teardown_tail, daemon=True).start()
    assert mgr._offload_delivering == 1, \
        "parked worker did not hold an in-flight delivery claim"
    assert not tail_done.wait(0.5), \
        "teardown finished while a claimed refusal was undelivered"

    audit_release.set()
    assert tail_done.wait(5), "teardown never released the final wait"
    assert len(errors) == 1, "claimed refusal never delivered"
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    assert mgr._offload_pending == 0
    assert mgr._offload_delivering == 0


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_offload_worker_dequeued_after_drain_fence_answers_nothing(
        monkeypatch, tmp_path):
    """A worker resuming after _offload_drained must produce nothing:
    its op was already answered by the drain fallback, and the fence
    forbids it from claiming a delivery or running the mutation."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    parked = threading.Event()
    release_worker = threading.Event()
    worker_done = threading.Event()
    real_enqueue = mgr._offload_enqueue

    def parking_enqueue(item):
        guarded, settle = item

        def parked_guarded():
            parked.set()
            assert release_worker.wait(5), \
                "test did not release the dequeued worker"
            try:
                guarded()
            finally:
                worker_done.set()

        real_enqueue((parked_guarded, settle))

    monkeypatch.setattr(mgr, "_offload_enqueue", parking_enqueue)
    store_touched = threading.Event()
    mgr.store.create = lambda *a, **kw: store_touched.set()
    errors: list = []
    mgr.CreateSilo("work", 2000, threading.Event().set, errors.append,
                   sender=":1.2", conn=object())
    assert parked.wait(2), "worker never dequeued the op"

    # Full teardown while the worker is still descheduled: settle the
    # (already-consumed) queue, wait, drain, then fence.
    with mgr._offload_pending_lock:
        mgr._stopping = True
    while True:
        try:
            _g, settle = mgr._offload_work_q.get_nowait()
        except queue.Empty:
            break
        settle()
    with mgr._offload_active_cv:
        assert mgr._offload_active_cv.wait_for(
            lambda: (mgr._offload_active == 0
                     and mgr._offload_delivering == 0),
            timeout=5)
    with mgr._offload_pending_lock:
        drains = list(mgr._offload_answers.values())
    for drain in drains:
        drain()
    assert len(errors) == 1, "drain did not answer the unclaimed op"
    assert errors[0].get_dbus_name() == f"{sm.BUS_NAME}.SiloBusy"
    with mgr._offload_active_cv:
        mgr._offload_drained = True
        assert mgr._offload_active_cv.wait_for(
            lambda: mgr._offload_delivering == 0, timeout=5)

    # The worker resumes post-fence: the drained path must return
    # without answering, mutating, or touching the counters.
    release_worker.set()
    assert worker_done.wait(5), "drained worker never finished"
    assert len(errors) == 1, "drained worker answered a second time"
    assert not store_touched.is_set(), "drained op still mutated"
    assert mgr._offload_pending == 0
    assert mgr._offload_delivering == 0
    assert mgr._offload_active == 0


def _list_silos(mgr, uid_or_exc):
    """Call ListSilos as a peer with uid (or whose uid lookup raises);
    return ("reply", rows) or ("error", dbus error name)."""
    def peer_uid(_sender, _conn):
        if isinstance(uid_or_exc, BaseException):
            raise uid_or_exc
        return uid_or_exc
    mgr._peer_uid = peer_uid
    out: list = []
    done = threading.Event()

    def on_reply(raw):
        out.append(("reply", json.loads(raw)))
        done.set()

    def on_error(exc):
        out.append(("error", exc.get_dbus_name()))
        done.set()

    mgr.ListSilos(on_reply, on_error, sender=":1.9", conn=object())
    assert done.wait(2), "ListSilos never answered"
    return out[0]


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_list_silos_shows_other_uids_only_their_own_silo(
        monkeypatch, tmp_path):
    """ensures: a non-admin uid (which the bus policy lets call ListSilos)
    cannot read other silos' launch config and egress; admin and root
    tooling still see every row (devin 14 F4)."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    store.create("work", 2000)
    store.create("bank", 2001)
    mgr = _mgr(store, monkeypatch)

    for uid in (sm.ADMIN_UID, 0):
        kind, rows = _list_silos(mgr, uid)
        assert kind == "reply"
        assert sorted(r["name"] for r in rows) == ["bank", "work"], uid
    kind, rows = _list_silos(mgr, 2000)
    assert kind == "reply"
    assert [r["name"] for r in rows] == ["work"]
    kind, rows = _list_silos(mgr, 4242)
    assert (kind, rows) == ("reply", [])
    # An unresolvable caller is refused, not shown everything.
    kind, name = _list_silos(mgr, sm.NotAuthorized("no uid"))
    assert (kind, name) == ("error", f"{sm.BUS_NAME}.NotAuthorized")
    assert mgr._offload_pending_reads == 0
    assert mgr._offload_pending == 0


@pytest.mark.skipif(sm.dbus is None, reason="dbus-python unavailable")
def test_list_silos_burst_never_takes_a_lifecycle_slot(
        monkeypatch, tmp_path):
    """ensures: reads from any uid cannot answer the admin's lifecycle
    calls SiloBusy — a saturated read budget refuses only the read, and
    a saturated lifecycle budget does not refuse the read."""
    store = sm._SiloStore(
        _FakeOps(), config_path=tmp_path / "silos.yaml")
    mgr = _mgr(store, monkeypatch)

    mgr._offload_pending_reads = sm._OFFLOAD_READ_MAX
    kind, name = _list_silos(mgr, sm.ADMIN_UID)
    assert (kind, name) == ("error", f"{sm.BUS_NAME}.SiloBusy")
    assert mgr._offload_pending_reads == sm._OFFLOAD_READ_MAX

    replied = threading.Event()
    errors: list = []
    mgr.CreateSilo("work", 2000, replied.set, errors.append,
                   sender=":1.2", conn=object())
    assert replied.wait(2), f"lifecycle call refused: {errors}"
    assert errors == []
    assert store.get("work").uid == 2000

    mgr._offload_pending_reads = 0
    mgr._offload_pending = sm._OFFLOAD_QUEUE_MAX
    kind, rows = _list_silos(mgr, sm.ADMIN_UID)
    assert kind == "reply"
    assert [r["name"] for r in rows] == ["work"]
    assert mgr._offload_pending == sm._OFFLOAD_QUEUE_MAX
    assert mgr._offload_pending_reads == 0
