"""Tests for the bridge_adapter threading / timeout / PID-reuse fixes.

Covers three previously-reported gaps in
``qdbrowser/plugins/bridge_adapter.py``:

1. Outbound daemon forwards (download / media state) must not block the
   GUI thread — they run on a dedicated worker thread.
2. The polkit gate (which may run an interactive ``pkcheck`` with a 15 s
   cap) is enforced on the receive thread, BEFORE the op is bounced to
   the GUI thread, so an auth prompt can't freeze the UI and a denied
   call never executes the mutating op (``authorize`` raises, ``invoke``
   never runs).
3. ``caller_start_time`` is read for the caller PID and threaded through
   ``authorize`` → polkit so polkit can defeat a PID-reuse race.

These are pure-Python (no real session bus), using fakes/monkeypatch.
"""

from __future__ import annotations

import threading
import time

import pytest
from qdbrowser.plugins import bridge_adapter as ba

# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #


class _RecordingForwarder:
    """Stands in for DaemonForwarder; records the thread each call ran
    on and (optionally) blocks to simulate a slow daemon."""

    def __init__(self, block_event=None):
        self.calls: list = []
        self.threads: list = []
        self._block = block_event

    def notify_download(self, *a, **kw):
        self.threads.append(threading.current_thread())
        if self._block is not None:
            self._block.wait(timeout=5.0)
        self.calls.append(("download", a, kw))
        return {"ok": True}

    def publish_media(self, *a, **kw):
        self.threads.append(threading.current_thread())
        if self._block is not None:
            self._block.wait(timeout=5.0)
        self.calls.append(("media", a, kw))
        return {"ok": True}


# --------------------------------------------------------------------- #
# Fix 1 — outbound forwards run off the GUI thread
# --------------------------------------------------------------------- #


def test_forward_worker_runs_on_separate_thread():
    """A started worker runs submitted forwards on its own thread, not
    the caller's thread."""
    worker = ba._ForwardWorker()
    worker.start()
    try:
        seen = {}
        done = threading.Event()

        def job():
            seen["thread"] = threading.current_thread()
            done.set()

        worker.submit(job)
        assert done.wait(timeout=2.0)
        assert seen["thread"] is not threading.current_thread()
    finally:
        worker.stop()


def test_forward_worker_submit_does_not_block_caller():
    """submit() returns immediately even when the job blocks for a long
    time — proving the GUI thread is not held up by a slow daemon."""
    release = threading.Event()
    worker = ba._ForwardWorker()
    worker.start()
    try:
        def slow():
            release.wait(timeout=5.0)

        t0 = time.monotonic()
        worker.submit(slow)
        elapsed = time.monotonic() - t0
        # submit must return promptly (well under the 5 s the job blocks).
        assert elapsed < 0.5
    finally:
        release.set()
        worker.stop()


def test_forward_worker_inline_when_not_started():
    """Without start(), submit runs inline (back-compat for callers that
    never start the worker, e.g. unit tests using emit_* directly)."""
    worker = ba._ForwardWorker()
    ran = {"v": False}
    worker.submit(lambda: ran.__setitem__("v", True))
    assert ran["v"] is True


def test_forward_worker_drops_oldest_when_full():
    """A wedged worker (queue full) drops the oldest pending forward
    rather than growing without bound or blocking the producer."""
    block = threading.Event()
    worker = ba._ForwardWorker()
    worker.start()
    try:
        # Wedge the worker thread on the first item.
        worker.submit(lambda: block.wait(timeout=5.0))
        # Fill the queue past capacity; the producer must never block.
        t0 = time.monotonic()
        for _ in range(ba._ForwardWorker._MAX_PENDING + 50):
            worker.submit(lambda: None)
        assert time.monotonic() - t0 < 1.0
        # Queue is bounded.
        assert worker._queue.qsize() <= ba._ForwardWorker._MAX_PENDING
    finally:
        block.set()
        worker.stop()


def test_plugin_emit_download_runs_on_worker_thread():
    """When the worker is started, emit_download_started returns
    immediately and the blocking forward executes off-thread."""
    block = threading.Event()
    fwd = _RecordingForwarder(block_event=block)
    plugin = ba.BridgeAdapterPlugin()
    plugin.forwarder = fwd
    plugin._forward_worker.start()
    try:
        caller = threading.current_thread()
        t0 = time.monotonic()
        plugin.emit_download_started(7, "big.iso", state=2,
                                     url="https://x/big.iso")
        # The GUI-thread call returns before the (blocked) forward runs.
        assert time.monotonic() - t0 < 0.5
        block.set()
        # Now let the worker drain and assert it ran off-thread.
        deadline = time.monotonic() + 2.0
        while not fwd.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fwd.calls, "forward never executed"
        assert fwd.threads[0] is not caller
    finally:
        block.set()
        plugin._forward_worker.stop()


def test_plugin_emit_media_snapshots_metadata_on_caller_thread():
    """Media metadata is read from the proxy synchronously (on the GUI
    thread) and passed into the off-thread forward, so the worker never
    touches the Qt-owned proxy."""
    fwd = _RecordingForwarder()
    plugin = ba.BridgeAdapterPlugin()
    plugin.forwarder = fwd
    plugin.media_proxy = ba.MediaProxy()
    plugin.media_proxy.update("Track", "Artist", "playing")
    plugin._forward_worker.start()
    try:
        plugin.emit_media_state_changed("playing")
        deadline = time.monotonic() + 2.0
        while not fwd.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fwd.calls
        kind, _a, kw = fwd.calls[0]
        assert kind == "media"
        assert kw["title"] == "Track"
        assert kw["artist"] == "Artist"
    finally:
        plugin._forward_worker.stop()


# --------------------------------------------------------------------- #
# Fix 2 — polkit runs before invoke; denied call never executes the op
# --------------------------------------------------------------------- #


def _handlers(polkit, history=None, bookmarks=None):
    from tests.test_bridge_adapter_handlers import _FakeDownloads, _FakeMedia, _FakePages, _FakeTabs
    return ba.BridgeAdapterHandlers(
        _FakeTabs(), _FakePages(), _FakeDownloads(), _FakeMedia(),
        polkit=polkit, history=history, bookmarks=bookmarks)


def test_authorize_denied_does_not_invoke_op():
    """A denied authorize() raises and the mutating op (TabsClose) is
    never reached — no action-performed-but-error-reported."""
    fake_tabs = None

    def polkit(action, pid):
        return False

    h = _handlers(polkit)
    fake_tabs = h.tabs
    with pytest.raises(PermissionError):
        h.authorize("TabsClose", caller_pid=4242)
    # The op never ran.
    assert fake_tabs.closed == []


def test_authorize_then_invoke_executes_op():
    """authorize() granting + invoke() runs the op exactly once."""
    h = _handlers(lambda action, pid: True)
    h.authorize("TabsOpen", caller_pid=4242)
    body, sig = h.invoke("TabsOpen", ("https://x",))
    assert sig == "u"
    assert h.tabs.opened == ["https://x"]


def test_invoke_does_not_recheck_polkit():
    """invoke() must NOT re-run the polkit gate (it would touch
    pkcheck/Qt on the wrong thread); it trusts a prior authorize()."""
    called = {"n": 0}

    def polkit(action, pid):
        called["n"] += 1
        return False  # would deny if consulted

    h = _handlers(polkit)
    # invoke directly, bypassing authorize: it must still run the op.
    body, _ = h.invoke("TabsList", ())
    assert called["n"] == 0
    assert body[0]  # got the tab list


def test_recv_loop_runs_polkit_before_main_thread_dispatch(monkeypatch):
    """In the recv loop, the polkit gate is evaluated on the recv thread
    (via authorize) BEFORE call_on_main_thread is ever invoked. A deny
    means the main-thread dispatch is never scheduled."""
    from unittest.mock import MagicMock

    from jeepney import HeaderFields, MessageType

    plugin = ba.BridgeAdapterPlugin()
    plugin._active = True
    plugin._bus_name = "org.qdistro.QdBrowser.pid1"

    order: list = []

    deny_polkit = lambda action, pid, **kw: order.append("polkit") or False
    plugin._handlers = _handlers(deny_polkit)

    # Spy on call_on_main_thread so we can prove it is never called on a
    # denied request.
    def fake_main(fn, timeout=10.0):
        order.append("main_dispatch")
        return fn()
    plugin._dispatch_helper.call_on_main_thread = fake_main

    plugin._stop = threading.Event()

    fake_msg = MagicMock()
    fake_msg.header.message_type = MessageType.method_call
    fake_msg.header.serial = 5
    fake_msg.header.fields = {
        HeaderFields.interface: ba.QDBROWSER_IFACE,
        HeaderFields.path: ba.QDBROWSER_PATH,
        HeaderFields.member: "TabsOpen",
        HeaderFields.sender: ":1.42",
    }
    fake_msg.body = ("https://x",)

    sent = []

    class FakeConn:
        sock = MagicMock()
        _n = 0

        def receive(self, timeout=None):
            self._n += 1
            if self._n == 1:
                return fake_msg
            plugin._stop.set()
            raise TimeoutError

        def send(self, msg):
            sent.append(msg)

    class FakePidConn:
        def send_and_get_reply(self, msg, timeout=None):
            r = MagicMock()
            r.body = (1234,)
            return r

    plugin._conn = FakeConn()
    plugin._pid_conn = FakePidConn()
    monkeypatch.setattr(ba.select, "select",
                        lambda r, w, x, t: (r, w, x))
    # Return a real start time so the fail-closed path is NOT taken and
    # the polkit hook is actually consulted (we want to prove the deny
    # comes from polkit, before any main-thread dispatch).
    monkeypatch.setattr(
        ba.BridgeAdapterPlugin, "_resolve_sender_start_time",
        staticmethod(lambda pid: 12345))

    plugin._recv_loop()

    # polkit was consulted; main-thread dispatch was NEVER scheduled.
    assert "polkit" in order
    assert "main_dispatch" not in order
    # And the reply is an AccessDenied error.
    assert len(sent) == 1
    assert sent[0].header.message_type == MessageType.error
    from jeepney import HeaderFields as HF
    assert "AccessDenied" in sent[0].header.fields.get(HF.error_name, "")


# --------------------------------------------------------------------- #
# Fix 3 — caller_start_time threaded through to polkit (PID-reuse guard)
# --------------------------------------------------------------------- #


def test_authorize_passes_start_time_to_polkit():
    """authorize() forwards caller_start_time to a polkit hook that
    accepts it."""
    seen = {}

    def polkit(action, pid, caller_start_time=None):
        seen["pid"] = pid
        seen["start"] = caller_start_time
        return True

    h = _handlers(polkit)
    h.authorize("TabsOpen", caller_pid=4242, caller_start_time=987654)
    assert seen["pid"] == 4242
    assert seen["start"] == 987654


def test_authorize_tolerates_legacy_two_arg_polkit():
    """A polkit hook that accepts only (action, pid) still works when a
    start time is supplied (the hardening just doesn't apply to it)."""
    def legacy(action, pid):
        return True

    h = _handlers(legacy)
    # Must not raise TypeError despite passing a start time.
    h.authorize("TabsOpen", caller_pid=4242, caller_start_time=987654)


def test_polkit_check_passes_pid_start_time_to_pkcheck(monkeypatch):
    """polkit_check, given a start time, calls pkcheck with the
    composite ``pid,start_time`` --process argument so polkit refuses a
    recycled PID."""
    captured = {}

    class FakeResult:
        returncode = 0

    def fake_run(args, capture_output=True, timeout=None):
        captured["args"] = args
        return FakeResult()

    monkeypatch.setattr(ba.subprocess, "run", fake_run)
    ok = ba.polkit_check("org.qdistro.qdbrowser.tabs.open",
                         4242, caller_start_time=55555)
    assert ok is True
    assert "4242,55555" in captured["args"]


def test_resolve_sender_start_time_reads_own_proc():
    """_resolve_sender_start_time returns a positive int for a live PID
    (this process) and None for a bogus one."""
    import os as _os
    st = ba.BridgeAdapterPlugin._resolve_sender_start_time(_os.getpid())
    assert isinstance(st, int)
    assert st > 0
    # Implausible PID → None (best-effort, no raise).
    assert ba.BridgeAdapterPlugin._resolve_sender_start_time(
        2 ** 31 - 1) is None
    # None pid → None.
    assert ba.BridgeAdapterPlugin._resolve_sender_start_time(None) is None


def test_method_needs_pkcheck_classification():
    """Mutating / privacy-sensitive methods need pkcheck; read-only
    inventory methods (open actions) do not."""
    needs = ba.BridgeAdapterHandlers.method_needs_pkcheck
    assert needs("TabsOpen") is True
    assert needs("TabsClose") is True
    assert needs("HistorySearch") is True
    assert needs("BookmarksSearch") is True
    assert needs("PageExtract") is True
    # Read-only inventory — pkcheck is skipped via _OPEN_ACTIONS.
    assert needs("TabsList") is False
    assert needs("MediaStatus") is False
    assert needs("DownloadsList") is False
    # Unknown method: no action, so not pkcheck-gated.
    assert needs("NoSuchMethod") is False


def test_call_polkit_does_not_downgrade_on_internal_typeerror():
    """A start-time-aware hook that raises TypeError from its OWN body
    must propagate, NOT be silently retried without the start time
    (which would drop the PID-reuse protection)."""
    def hook(action, pid, caller_start_time=None):
        raise TypeError("boom from inside the hook")

    h = _handlers(hook)
    with pytest.raises(TypeError):
        h._call_polkit("org.qdistro.qdbrowser.tabs.open", 42, 999)


def test_recv_loop_fails_closed_when_start_time_unresolvable(monkeypatch):
    """A pkcheck-gated method from an external caller whose start time
    cannot be read must be DENIED (fail closed) — never authorized on a
    bare PID that could have been recycled."""
    from unittest.mock import MagicMock

    from jeepney import HeaderFields, MessageType

    plugin = ba.BridgeAdapterPlugin()
    plugin._active = True
    plugin._bus_name = "org.qdistro.QdBrowser.pid1"

    polkit_calls = {"n": 0}

    def polkit(action, pid, **kw):
        polkit_calls["n"] += 1
        return True  # would GRANT if reached

    plugin._handlers = _handlers(polkit)
    main_called = {"v": False}
    plugin._dispatch_helper.call_on_main_thread = (
        lambda fn, timeout=10.0: main_called.__setitem__("v", True) or fn())
    plugin._stop = threading.Event()

    fake_msg = MagicMock()
    fake_msg.header.message_type = MessageType.method_call
    fake_msg.header.serial = 8
    fake_msg.header.fields = {
        HeaderFields.interface: ba.QDBROWSER_IFACE,
        HeaderFields.path: ba.QDBROWSER_PATH,
        HeaderFields.member: "TabsOpen",
        HeaderFields.sender: ":1.77",
    }
    fake_msg.body = ("https://x",)

    sent = []

    class FakeConn:
        sock = MagicMock()
        _n = 0

        def receive(self, timeout=None):
            self._n += 1
            if self._n == 1:
                return fake_msg
            plugin._stop.set()
            raise TimeoutError

        def send(self, msg):
            sent.append(msg)

    class FakePidConn:
        def send_and_get_reply(self, msg, timeout=None):
            r = MagicMock()
            r.body = (1234,)
            return r

    plugin._conn = FakeConn()
    plugin._pid_conn = FakePidConn()
    monkeypatch.setattr(ba.select, "select",
                        lambda r, w, x, t: (r, w, x))
    # Start time unresolvable.
    monkeypatch.setattr(
        ba.BridgeAdapterPlugin, "_resolve_sender_start_time",
        staticmethod(lambda pid: None))

    plugin._recv_loop()

    # Denied before polkit was ever consulted and before any op ran.
    assert polkit_calls["n"] == 0
    assert main_called["v"] is False
    assert len(sent) == 1
    assert sent[0].header.message_type == MessageType.error
    from jeepney import HeaderFields as HF
    assert "AccessDenied" in sent[0].header.fields.get(HF.error_name, "")


def test_recv_loop_allows_readonly_when_start_time_unresolvable(monkeypatch):
    """A read-only inventory method (TabsList) is NOT denied just because
    the caller start time is unresolvable — it never used pkcheck."""
    from unittest.mock import MagicMock

    from jeepney import HeaderFields, MessageType

    plugin = ba.BridgeAdapterPlugin()
    plugin._active = True
    plugin._bus_name = "org.qdistro.QdBrowser.pid1"
    plugin._handlers = _handlers(lambda a, p, **kw: True)
    plugin._stop = threading.Event()

    fake_msg = MagicMock()
    fake_msg.header.message_type = MessageType.method_call
    fake_msg.header.serial = 9
    fake_msg.header.fields = {
        HeaderFields.interface: ba.QDBROWSER_IFACE,
        HeaderFields.path: ba.QDBROWSER_PATH,
        HeaderFields.member: "TabsList",
        HeaderFields.sender: ":1.88",
    }
    fake_msg.body = ()

    sent = []

    class FakeConn:
        sock = MagicMock()
        _n = 0

        def receive(self, timeout=None):
            self._n += 1
            if self._n == 1:
                return fake_msg
            plugin._stop.set()
            raise TimeoutError

        def send(self, msg):
            sent.append(msg)

    class FakePidConn:
        def send_and_get_reply(self, msg, timeout=None):
            r = MagicMock()
            r.body = (1234,)
            return r

    plugin._conn = FakeConn()
    plugin._pid_conn = FakePidConn()
    monkeypatch.setattr(ba.select, "select",
                        lambda r, w, x, t: (r, w, x))
    monkeypatch.setattr(
        ba.BridgeAdapterPlugin, "_resolve_sender_start_time",
        staticmethod(lambda pid: None))

    plugin._recv_loop()

    assert len(sent) == 1
    # A method_return (success), not an error.
    assert sent[0].header.message_type == MessageType.method_return


def test_activate_failure_stops_forward_worker(monkeypatch):
    """If _claim_bus_name fails after the worker started, activate() must
    stop the worker so the thread doesn't leak."""
    plugin = ba.BridgeAdapterPlugin()

    monkeypatch.setattr(ba, "_enabled_config_override", lambda: True)

    class _FakePlugins:
        _instances = {}

        def get_by_capability(self, cap):
            return []

    app = type("App", (), {})()
    app.plugins = _FakePlugins()
    # Signals the activate path connects to.
    app.webview_added = type("S", (), {"connect": lambda *a: None})()
    app.webview_removed = type("S", (), {"connect": lambda *a: None})()

    monkeypatch.setattr(
        ba.BridgeAdapterPlugin, "_claim_bus_name", lambda self: False)

    plugin.activate(app)
    assert plugin._active is False
    # Worker thread must not be left running.
    assert (plugin._forward_worker._thread is None
            or not plugin._forward_worker._thread.is_alive())


def test_resolve_sender_start_time_handles_spaces_in_comm(monkeypatch):
    """Field-22 parse survives a process whose comm contains ')' and
    spaces (the rfind(')') split must align the trailing fields)."""
    # Synthesize /proc stat with a nasty comm: "(weird ) name)".
    fake_stat = (b"4242 (weird ) name) S 1 4242 4242 0 -1 0 0 0 0 0 0 0 "
                 b"0 0 20 0 1 0 123456 0 0")

    class _FakeFile:
        def __init__(self, data):
            self._data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._data

    monkeypatch.setattr("builtins.open",
                        lambda *a, **kw: _FakeFile(fake_stat))
    st = ba.BridgeAdapterPlugin._resolve_sender_start_time(4242)
    assert st == 123456


def test_call_on_main_thread_from_plain_thread_runs_on_gui_thread(qapp, qtbot):
    """The recv thread is a plain threading.Thread with no Qt event loop.
    A QTimer.singleShot(0, fn) posted from it lives in THAT thread and never
    fires, so every D-Bus call timed out ("main-thread dispatch timed out")
    in the VM smoke bats. The wake-up must be delivered to the GUI thread."""
    helper = ba._DispatchHelper()
    gui_thread = threading.current_thread()
    out: dict = {}

    def worker():
        try:
            out["value"] = helper.call_on_main_thread(
                lambda: threading.current_thread(), timeout=3.0)
        except Exception as exc:  # pragma: no cover - asserted below
            out["exc"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    qtbot.waitUntil(lambda: not t.is_alive(), timeout=5000)
    assert "exc" not in out, out.get("exc")
    assert out["value"] is gui_thread


def test_call_on_main_thread_from_gui_thread_does_not_deadlock(qapp):
    helper = ba._DispatchHelper()
    assert helper.call_on_main_thread(lambda: 42, timeout=2.0) == 42
