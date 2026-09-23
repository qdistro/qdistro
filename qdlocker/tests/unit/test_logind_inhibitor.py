"""Unit tests for the logind sleep delay-inhibitor in LogindWatcher.

These tests drive the inhibitor coroutines directly (no real D-Bus /
dbus-next): a fake Manager hands out inhibitor "fds" via call_inhibit,
and os.close is patched to record releases. The goal is to pin the
core invariant the todo asks for:

    on PrepareForSleep(start=True) the locker requests the lock and
    releases the sleep delay inhibitor ONLY AFTER the lock is confirmed
    (or a bounded timeout elapses) — never before.

so the system does not suspend with pre-lock content still on screen.
"""

from __future__ import annotations

import asyncio

import pytest
from qdlocker import logind
from qdlocker.logind import REASON_SUSPEND, LogindWatcher


class FakeManager:
    """Minimal stand-in for the login1.Manager proxy. Hands out a fresh
    integer fd per Inhibit call and records the arguments."""

    def __init__(self) -> None:
        self.inhibit_calls: list[tuple] = []
        self._next_fd = 1000

    async def call_inhibit(self, what, who, why, mode):  # noqa: ANN001
        self.inhibit_calls.append((what, who, why, mode))
        fd = self._next_fd
        self._next_fd += 1
        return fd


@pytest.fixture
def closed_fds(monkeypatch):
    """Record os.close() targets instead of touching real fds (the fake
    'fds' are not real, so a real close would EBADF)."""
    recorded: list[int] = []

    def _fake_close(fd):  # noqa: ANN001
        recorded.append(fd)

    monkeypatch.setattr(logind.os, "close", _fake_close)
    return recorded


async def _eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 1.0)


def _make_watcher(on_lock):
    w = LogindWatcher(on_lock=on_lock)
    return w


def test_inhibitor_acquired_with_delay_mode(closed_fds):
    """Startup acquires a *sleep* inhibitor in *delay* mode (not block):
    delay mode lets logind force the transition after InhibitDelayMaxSec,
    so a stuck locker can never wedge suspend forever."""
    async def run():
        w = _make_watcher(on_lock=lambda r: None)
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        assert w._inhibit_fd == 1000
        assert w._mgr.inhibit_calls == [
            ("sleep", "qdlocker",
             "Lock the screen before the system sleeps", "delay")
        ]

    asyncio.run(run())


def test_release_only_after_confirmation(closed_fds):
    """The inhibitor fd must NOT be released until the lock is confirmed.

    We start the suspend coroutine, assert the fd is still held while
    confirmation is pending, then confirm and assert it is released.
    """
    locks: list[int] = []

    async def run():
        w = _make_watcher(on_lock=lambda r: locks.append(r))
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        held_fd = w._inhibit_fd
        assert held_fd is not None

        task = asyncio.ensure_future(w._on_prepare_for_sleep_async())
        # Let the coroutine run up to its await on confirmation.
        await asyncio.sleep(0)
        # Lock must have been requested with the suspend reason...
        assert locks == [REASON_SUSPEND]
        # ...but the inhibitor is still held (not yet released).
        assert w._inhibit_fd == held_fd
        assert closed_fds == []

        # Now confirm the lock (as the compositor's locked_changed=1 path
        # would via notify_lock_confirmed()).
        w.notify_lock_confirmed()
        await task

        # fd released exactly once, after confirmation.
        assert w._inhibit_fd is None
        assert closed_fds == [held_fd]

    asyncio.run(run())


def test_release_on_timeout_is_failopen(closed_fds, monkeypatch):
    """If confirmation never arrives, the inhibitor is released after the
    bounded timeout so suspend is never wedged. Lock is still requested."""
    monkeypatch.setattr(logind, "_LOCK_CONFIRM_TIMEOUT_S", 0.05)
    locks: list[int] = []

    async def run():
        w = _make_watcher(on_lock=lambda r: locks.append(r))
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        held_fd = w._inhibit_fd

        # Never confirm — let the timeout fire.
        await w._on_prepare_for_sleep_async()

        assert locks == [REASON_SUSPEND]
        assert w._inhibit_fd is None
        assert closed_fds == [held_fd]

    asyncio.run(run())


def test_resume_reacquires_inhibitor(closed_fds):
    """After a sleep/resume cycle the inhibitor must be re-acquired so the
    NEXT suspend is also guarded."""
    async def run():
        w = _make_watcher(on_lock=lambda r: None)
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        first_fd = w._inhibit_fd

        # Suspend: confirm immediately so it releases.
        task = asyncio.ensure_future(w._on_prepare_for_sleep_async())
        await asyncio.sleep(0)
        w.notify_lock_confirmed()
        await task
        assert w._inhibit_fd is None

        # Resume re-acquires a fresh fd.
        await w._on_resume_async()
        assert w._inhibit_fd is not None
        assert w._inhibit_fd != first_fd

    asyncio.run(run())


def test_acquire_failure_is_failopen(closed_fds):
    """If Inhibit raises, we keep no fd and suspend proceeds uninhibited;
    the lock is still requested on the suspend trigger."""
    locks: list[int] = []

    class FailingManager:
        async def call_inhibit(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("logind said no")

    async def run():
        w = _make_watcher(on_lock=lambda r: locks.append(r))
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FailingManager()
        await w._acquire_inhibitor()
        assert w._inhibit_fd is None

        # Suspend still requests the lock; no fd to release/close.
        await w._on_prepare_for_sleep_async()
        assert locks == [REASON_SUSPEND]
        assert closed_fds == []

    asyncio.run(run())


def test_no_double_acquire(closed_fds):
    """Acquiring twice in a row must not leak a second fd."""
    async def run():
        w = _make_watcher(on_lock=lambda r: None)
        w._loop = asyncio.get_running_loop()
        w._lock_confirmed = asyncio.Event()
        w._mgr = FakeManager()
        await w._acquire_inhibitor()
        first = w._inhibit_fd
        await w._acquire_inhibitor()  # no-op
        assert w._inhibit_fd == first
        assert len(w._mgr.inhibit_calls) == 1

    asyncio.run(run())


def test_notify_before_loop_is_noop():
    """notify_lock_confirmed() must be safe before the asyncio loop is up
    (e.g. an early compositor locked_changed during startup)."""
    w = LogindWatcher(on_lock=lambda r: None)
    # No loop / event yet — must not raise.
    w.notify_lock_confirmed()


@pytest.mark.parametrize('pid_session,lid_enabled,foreign_uid,env_session', [
    (False, True, False, True), (False, False, False, True),
    (True, True, False, True), (False, True, True, True),
    (False, True, False, False),
])
def test_main_sleep_registration(monkeypatch, closed_fds, pid_session,
                                 lid_enabled, foreign_uid, env_session):
    """Drive _main through NoSessionForPID and lid=ignore, then suspend/resume."""
    import sys
    from types import ModuleType, SimpleNamespace
    locks = []
    session = SimpleNamespace(lock=None)
    manager = FakeManager()
    manager.sleep = None
    resolved_ids = []

    async def by_pid(pid):
        if not pid_session:
            raise RuntimeError('org.freedesktop.login1.NoSessionForPID')
        return '/session/test'

    async def by_id(session_id):
        resolved_ids.append(session_id)
        return '/session/test'

    async def get_user():
        return (logind.os.getuid() + int(foreign_uid), '/user/test')

    manager.call_get_session_by_pid = by_pid
    manager.call_get_session = by_id
    manager.on_prepare_for_sleep = lambda cb: setattr(manager, 'sleep', cb)
    session.get_user = get_user
    session.on_lock = lambda cb: setattr(session, 'lock', cb)
    session.off_lock = lambda cb: setattr(session, 'lock', None)
    manager.off_prepare_for_sleep = lambda cb: setattr(manager, 'sleep', None)
    manager.on_session_new = lambda cb: None
    manager.off_session_new = lambda cb: None
    manager.on_session_removed = lambda cb: None
    manager.off_session_removed = lambda cb: None
    daemon = SimpleNamespace(on_name_owner_changed=lambda cb: None,
                             off_name_owner_changed=lambda cb: None)

    class Bus:
        def __init__(self, **kwargs):
            assert kwargs['negotiate_unix_fd'] is True

        async def connect(self):
            return self

        async def introspect(self, name, path):
            return None

        def get_proxy_object(self, name, path, intro):
            return SimpleNamespace(get_interface=lambda iface:
                daemon if iface == "org.freedesktop.DBus" else
                manager if iface.endswith('.Manager') else session)

        async def wait_for_disconnect(self):
            await asyncio.Future()

        def disconnect(self):
            pass

    dbus = ModuleType('dbus_next')
    dbus.BusType = SimpleNamespace(SYSTEM=1)
    aio = ModuleType('dbus_next.aio')
    aio.MessageBus = Bus
    monkeypatch.setitem(sys.modules, 'dbus_next', dbus)
    monkeypatch.setitem(sys.modules, 'dbus_next.aio', aio)
    if env_session:
        monkeypatch.setenv('XDG_SESSION_ID', 'test-session')
    else:
        monkeypatch.delenv('XDG_SESSION_ID', raising=False)

    async def run():
        watcher = LogindWatcher(on_lock=locks.append, lock_on_lid=lid_enabled)
        task = asyncio.create_task(watcher._main())
        await _eventually(lambda: watcher.automatic_lock_ready or task.done())
        assert not task.done(), 'session discovery must not terminate sleep protection'
        assert manager.sleep is not None
        assert watcher._inhibit_fd == 1000
        if foreign_uid or not env_session:
            assert session.lock is None
        else:
            session.lock()
            assert locks == ([logind.REASON_LID] if lid_enabled else [])
        manager.sleep(True)
        await asyncio.sleep(0)
        assert locks[-1] == REASON_SUSPEND
        assert closed_fds == []
        watcher.notify_lock_confirmed()
        for _ in range(8):
            await asyncio.sleep(0)
        assert closed_fds == [1000]
        manager.sleep(False)
        await _eventually(lambda: watcher._inhibit_fd == 1001)
        assert watcher._inhibit_fd == 1001
        watcher._stop_event.set()
        await task
        assert closed_fds == [1000, 1001]
        assert resolved_ids == ([] if pid_session or not env_session else ['test-session'])
    asyncio.run(run())


def test_app_starts_suspend_watcher_when_lid_ignored():
    """Execute app's watcher setup without importing its Qt/Wayland stack."""
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    source = ast.parse((Path(__file__).parents[2] / 'qdlocker' / 'app.py').read_text())
    func = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    setup = []
    collecting = False
    for node in func.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_logind'
                                               for t in node.targets):
            collecting = True
        if collecting:
            setup.append(node)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and \
                    isinstance(node.value.func, ast.Attribute) and node.value.func.attr == 'start':
                break
    assert setup
    watchers = []
    class Watcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            watchers.append(self)
        def notify_lock_confirmed(self):
            pass
        def start(self):
            self.started = True
    callbacks = []
    bridge = SimpleNamespace(inject_lock_requested=lambda reason: None,
                             set_lock_confirmed_cb=callbacks.append)
    exec(compile(ast.Module(body=setup, type_ignores=[]), '<app watcher setup>', 'exec'),
         {'LogindWatcher': Watcher, 'config': {'lid_action': 'ignore'}, 'bridge': bridge})
    assert len(watchers) == 1 and watchers[0].started
    assert watchers[0].kwargs['lock_on_lid'] is False
    assert callbacks == [watchers[0].notify_lock_confirmed]


@pytest.fixture
def reconnect_bus(monkeypatch):
    """Inject only the D-Bus transport/proxies; _main owns real asyncio tasks."""
    import sys
    from types import ModuleType, SimpleNamespace

    class Signals:
        def __init__(self):
            self.callbacks = {}

        def __getattr__(self, name):
            if name.startswith('on_'):
                return lambda cb: self.callbacks.setdefault(name[3:], []).append(cb)
            if name.startswith('off_'):
                return lambda cb: self.callbacks[name[4:]].remove(cb)
            raise AttributeError(name)

        def emit(self, name, *args):
            for cb in self.callbacks.get(name, [])[:]:
                cb(*args)

    class Manager(Signals, FakeManager):
        def __init__(self, index):
            Signals.__init__(self)
            FakeManager.__init__(self)
            self._next_fd += index * 100
            self.path = '/session/current'

        async def call_get_session_by_pid(self, pid):
            return self.path

    class Session(Signals):
        async def get_user(self):
            return (logind.os.getuid(), '/user/current')

    state = SimpleNamespace(instances=[], fail_connect=0, hang_connect=False)

    class Bus:
        def __init__(self, **kwargs):
            assert kwargs == {'bus_type': 1, 'negotiate_unix_fd': True}
            self.manager = Manager(len(state.instances))
            self.daemon = Signals()
            self.session = Session()
            self.disconnected = asyncio.Event()
            self.connect_cancelled = False
            state.instances.append(self)

        async def connect(self):
            if state.fail_connect:
                state.fail_connect -= 1
                raise ConnectionError('system bus unavailable')
            if state.hang_connect:
                try:
                    await asyncio.Future()
                finally:
                    self.connect_cancelled = True
            return self

        async def introspect(self, name, path):
            return None

        def get_proxy_object(self, name, path, intro):
            return SimpleNamespace(get_interface=lambda iface:
                self.daemon if iface == 'org.freedesktop.DBus' else
                self.manager if iface.endswith('.Manager') else self.session)

        async def wait_for_disconnect(self):
            await self.disconnected.wait()

        def disconnect(self):
            self.disconnected.set()

    dbus = ModuleType('dbus_next')
    dbus.BusType = SimpleNamespace(SYSTEM=1)
    aio = ModuleType('dbus_next.aio')
    aio.MessageBus = Bus
    monkeypatch.setitem(sys.modules, 'dbus_next', dbus)
    monkeypatch.setitem(sys.modules, 'dbus_next.aio', aio)
    monkeypatch.setattr(logind, '_RECONNECT_MIN_S', 0.001)
    monkeypatch.setattr(logind, '_RECONNECT_MAX_S', 0.01)
    return state


@pytest.mark.parametrize('trigger', ['owner', 'disconnect', 'session'])
def test_main_reconnects_and_rejects_stale_callbacks(reconnect_bus, closed_fds, trigger):
    """A logind/bus/session replacement restores pre-suspend locking and its fd."""
    async def run():
        locks = []
        watcher = LogindWatcher(on_lock=locks.append)
        task = asyncio.create_task(watcher._main())
        await _eventually(lambda: watcher.automatic_lock_ready)
        old = reconnect_bus.instances[0]
        stale_sleep = old.manager.callbacks['prepare_for_sleep'][0]
        stale_lock = old.session.callbacks['lock'][0]
        old.manager.emit('prepare_for_sleep', True)
        await _eventually(lambda: locks == [REASON_SUSPEND])
        assert closed_fds == [], 'restart test must begin with lock confirmation pending'
        if trigger == 'owner':
            old.daemon.emit('name_owner_changed', 'org.freedesktop.login1', ':1.1', ':1.2')
        elif trigger == 'disconnect':
            old.disconnected.set()
        else:
            old.manager.emit('session_removed', 'current', '/session/current')
        await _eventually(lambda: len(reconnect_bus.instances) == 2 and watcher.automatic_lock_ready)
        assert closed_fds == [1000], 'old confirmation task must close only its own inhibitor'
        assert watcher._inhibit_fd == 1100
        assert all(not handlers for handlers in old.manager.callbacks.values())
        assert all(not handlers for handlers in old.session.callbacks.values())
        stale_sleep(False)
        stale_lock()
        await asyncio.sleep(0)
        assert locks == [REASON_SUSPEND], 'retired connection callbacks must be inert'
        current = reconnect_bus.instances[1]
        current.manager.emit('prepare_for_sleep', True)
        await _eventually(lambda: locks == [REASON_SUSPEND, REASON_SUSPEND])
        watcher.notify_lock_confirmed()
        await _eventually(lambda: watcher._inhibit_fd is None)
        assert closed_fds == [1000, 1100]
        current.manager.emit('prepare_for_sleep', False)
        await _eventually(lambda: watcher._inhibit_fd == 1101)
        watcher._stop_event.set()
        await asyncio.wait_for(task, 1)
        assert closed_fds == [1000, 1100, 1101]
        assert not watcher.automatic_lock_ready
        assert watcher._mgr is None
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()], \
            'stop must await every watcher-owned connection, transition and retry task'
    asyncio.run(run())


def test_main_retries_initial_bus_failure(reconnect_bus, closed_fds):
    """A boot ordering race must not permanently disable automatic locking."""
    reconnect_bus.fail_connect = 1
    async def run():
        watcher = LogindWatcher(on_lock=lambda reason: None)
        task = asyncio.create_task(watcher._main())
        await _eventually(lambda: watcher.automatic_lock_ready)
        assert len(reconnect_bus.instances) == 2
        assert watcher._inhibit_fd == 1100
        watcher._stop_event.set()
        await asyncio.wait_for(task, 1)
        assert closed_fds == [1100]
    asyncio.run(run())


def test_stop_cancels_connection_setup(reconnect_bus, closed_fds):
    """Shutdown interrupts an unresponsive D-Bus connect without leaking tasks."""
    reconnect_bus.hang_connect = True
    async def run():
        watcher = LogindWatcher(on_lock=lambda reason: None)
        task = asyncio.create_task(watcher._main())
        await _eventually(lambda: bool(reconnect_bus.instances))
        await asyncio.sleep(0)
        watcher._stop_event.set()
        await asyncio.wait_for(task, 1)
        assert reconnect_bus.instances[0].connect_cancelled
        assert reconnect_bus.instances[0].disconnected.is_set()
        assert closed_fds == []
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    asyncio.run(run())


def test_resume_cancels_old_confirmation_before_reacquiring(reconnect_bus, closed_fds):
    """A late lock confirmation must not release the next sleep cycle's fd."""
    async def run():
        locks = []
        watcher = LogindWatcher(on_lock=locks.append)
        task = asyncio.create_task(watcher._main())
        await _eventually(lambda: watcher.automatic_lock_ready)
        manager = reconnect_bus.instances[0].manager
        manager.emit('prepare_for_sleep', True)
        await _eventually(lambda: locks == [REASON_SUSPEND])
        manager.emit('prepare_for_sleep', False)
        await _eventually(lambda: watcher._inhibit_fd == 1001)
        watcher.notify_lock_confirmed()
        await asyncio.sleep(0)
        assert closed_fds == [1000]
        assert watcher._inhibit_fd == 1001
        watcher._stop_event.set()
        await asyncio.wait_for(task, 1)
        assert closed_fds == [1000, 1001]
    asyncio.run(run())


def test_concurrent_inhibitor_acquires_own_one_fd(closed_fds):
    """A retry overlapping resume must not lose the first acquired fd."""
    class SlowManager(FakeManager):
        async def call_inhibit(self, *args):
            await asyncio.sleep(0)
            return await super().call_inhibit(*args)

    async def run():
        watcher = LogindWatcher(on_lock=lambda reason: None)
        watcher._mgr = SlowManager()
        await asyncio.gather(watcher._acquire_inhibitor(), watcher._acquire_inhibitor())
        assert len(watcher._mgr.inhibit_calls) == 1
        assert watcher._inhibit_fd == 1000
        watcher._release_inhibitor()
        assert closed_fds == [1000]
    asyncio.run(run())


@pytest.mark.parametrize('candidate_uid,active,expected', [
    (0, True, '/session/current'), (1, True, None), (0, False, None),
])
def test_session_replacement_resolves_stale_environment(monkeypatch, reconnect_bus,
                                                       closed_fds, candidate_uid,
                                                       active, expected):
    """A seatless user's stale XDG_SESSION_ID resolves only to an owned active seat."""
    monkeypatch.setenv('XDG_SESSION_ID', 'old-session')
    async def run():
        # Instantiate transport through the injected module, without bypassing
        # the production session lookup/ownership verification.
        from dbus_next.aio import MessageBus
        bus = MessageBus(bus_type=1, negotiate_unix_fd=True)
        manager = bus.manager
        async def missing(*args):
            raise RuntimeError('session no longer exists')
        async def sessions():
            return [('current', logind.os.getuid(), 'admin', 'seat0', '/session/current')]
        async def user():
            return (logind.os.getuid() + candidate_uid, '/user/current')
        async def is_active():
            return active
        async def session_type():
            return 'wayland'
        async def remote():
            return False
        manager.call_get_session_by_pid = missing
        manager.call_get_session = missing
        manager.call_list_sessions = sessions
        bus.session.get_user = user
        bus.session.get_active = is_active
        bus.session.get_type = session_type
        bus.session.get_remote = remote
        watcher = LogindWatcher(on_lock=lambda reason: None)
        actual = await watcher._subscribe_session(bus, manager)
        assert actual == expected
        assert bool(bus.session.callbacks.get('lock')) == (expected is not None)
    asyncio.run(run())
