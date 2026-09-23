"""Unit tests for the ShadowScreen / ShadowScreenRegistry primitives.

These exercise the data path with a real ``TerminalWidget`` (because the
shadow attaches to ``terminal.term.receivedData``), but call
``ShadowScreen.feed`` directly to avoid relying on a live shell.
"""

import pytest

pytest.importorskip("pyte")

import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.shadow_screen import (
    ShadowScreen,
    ShadowScreenRegistry,
)


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture
def terminal(qtbot):
    from qterminator.terminal import TerminalWidget
    t = TerminalWidget()
    qtbot.addWidget(t)
    t.resize(800, 400)
    t.show()
    qtbot.waitExposed(t)
    yield t


def test_feed_records_sequence_and_bytes(terminal):
    s = ShadowScreen(terminal)
    s.feed("hello")
    s.feed(" world")
    assert s.latest_seq == 2
    latest, data = s.tail(0)
    assert latest == 2
    assert data == b"hello world"


def test_tail_since_returns_only_newer_chunks(terminal):
    s = ShadowScreen(terminal)
    s.feed("aaa")
    s.feed("bbb")
    s.feed("ccc")
    _, data = s.tail(since=1)
    assert data == b"bbbccc"
    _, data = s.tail(since=3)
    assert data == b""


def test_snapshot_renders_through_pyte(terminal):
    s = ShadowScreen(terminal)
    # pyte must not be constructed before snapshot()
    assert s._screen is None
    s.feed("hello\r\nworld\r\n")
    snap = s.snapshot()
    assert s._screen is not None  # lazy init triggered
    assert any("hello" in line for line in snap["lines"])
    assert any("world" in line for line in snap["lines"])
    assert snap["cursor"]["y"] >= 1


def test_snapshot_replays_buffered_history_on_first_call(terminal):
    """Output that arrived before snapshot() is still in the snapshot.

    The plugin promise is that ``snapshot()`` reflects everything the
    terminal has emitted, not only what comes after subscription.
    """
    s = ShadowScreen(terminal)
    s.feed("prefix-line\r\n")
    s.feed("suffix-line\r\n")
    snap = s.snapshot()
    joined = "\n".join(snap["lines"])
    assert "prefix-line" in joined
    assert "suffix-line" in joined


def test_listeners_called_in_order_with_seq_and_bytes(terminal):
    s = ShadowScreen(terminal)
    events = []
    s.add_listener(lambda seq, raw: events.append((seq, raw)))
    s.feed("one")
    s.feed("two")
    assert events == [(1, b"one"), (2, b"two")]


def test_listener_exception_doesnt_poison_others(terminal):
    s = ShadowScreen(terminal)
    saw = []
    def bad(_seq, _raw): raise RuntimeError("boom")
    s.add_listener(bad)
    s.add_listener(lambda seq, raw: saw.append(seq))
    s.feed("x")
    assert saw == [1]


def test_remove_listener_stops_callbacks(terminal):
    s = ShadowScreen(terminal)
    events = []
    cb = lambda seq, raw: events.append(seq)
    s.add_listener(cb)
    s.feed("a")
    s.remove_listener(cb)
    s.feed("b")
    assert events == [1]


def test_buffer_truncates_at_limit(terminal):
    s = ShadowScreen(terminal)
    s.BUFFER_LIMIT = 10  # tiny, for test
    s.feed("aaaaa")  # 5 bytes
    s.feed("bbbbb")  # 10 total
    s.feed("ccccc")  # would exceed → drop head
    _, data = s.tail(0)
    assert len(data) <= s.BUFFER_LIMIT
    assert data.endswith(b"ccccc")


# --- registry ---

def test_registry_refcount_attaches_once(terminal):
    reg = ShadowScreenRegistry()
    h1 = reg.acquire(terminal)
    assert reg.refcount(terminal) == 1
    h2 = reg.acquire(terminal)
    assert reg.refcount(terminal) == 2
    assert h1.shadow is h2.shadow  # same underlying shadow
    h1.release()
    assert reg.refcount(terminal) == 1
    h2.release()
    assert reg.refcount(terminal) == 0
    assert reg.active_count() == 0


def test_registry_signal_disconnected_on_last_release(qtbot, terminal):
    """Pumping the terminal after the last handle is released must not
    grow the shadow's stream — the signal must have been disconnected."""
    reg = ShadowScreenRegistry()
    h = reg.acquire(terminal)
    captured = []
    h.add_listener(lambda seq, raw: captured.append(raw))

    # Drive the signal directly via the shadow API to keep test off the
    # PTY's wall-clock timing.
    h.shadow.feed("first")
    assert captured == [b"first"]

    h.release()
    # After release, the registry should have detached. Feeding
    # ``terminal.term.receivedData`` would normally enter the shadow,
    # but since it's detached, captured stays as-is. We simulate by
    # emitting the signal directly.
    terminal.term.receivedData.emit("ghost")
    qtbot.wait(20)
    assert captured == [b"first"]  # ghost was NOT recorded


def test_release_is_idempotent(terminal):
    reg = ShadowScreenRegistry()
    h = reg.acquire(terminal)
    h.release()
    h.release()  # must not raise; refcount must not go negative
    assert reg.refcount(terminal) == 0


def test_shutdown_drops_all_shadows(terminal):
    reg = ShadowScreenRegistry()
    h1 = reg.acquire(terminal)
    h2 = reg.acquire(terminal)
    reg.shutdown()
    assert reg.active_count() == 0
    # Handles still exist but their underlying shadows are detached;
    # explicit release should be safe.
    h1.release()
    h2.release()


def test_registry_uses_tmux_screen_when_resolver_matches(terminal):
    from qterminator.tmux_screen import TmuxScreen

    reg = ShadowScreenRegistry()
    reg.set_tmux_resolver(lambda _term: "qterm-1")
    handle = reg.acquire(terminal)
    try:
        assert isinstance(handle.shadow, TmuxScreen)
    finally:
        handle.release()


def test_tmux_screen_snapshot_uses_capture_pane(monkeypatch, terminal):
    from qterminator.tmux_screen import TmuxScreen

    class _Proc:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if "capture-pane" in argv:
            return _Proc(stdout="hello\nworld\n")
        return _Proc(stdout="5,1,20,4\n")

    monkeypatch.setattr("qterminator.tmux_screen.subprocess.run", fake_run)
    screen = TmuxScreen(terminal, "qterm-1")
    snap = screen.snapshot()
    assert snap["source"] == "tmux"
    assert snap["tmux_session"] == "qterm-1"
    assert snap["cursor"] == {"x": 5, "y": 1}
    assert snap["cols"] == 20
    assert snap["rows"] == 4
    assert snap["lines"][0].startswith("hello")
    assert any("capture-pane" in call for call in calls)


def test_tmux_screen_falls_back_to_shadow_on_tmux_error(monkeypatch, terminal):
    from qterminator.tmux_screen import TmuxScreen

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "no such pane"

    monkeypatch.setattr(
        "qterminator.tmux_screen.subprocess.run",
        lambda *a, **k: _Proc(),
    )
    screen = TmuxScreen(terminal, "missing")
    screen.feed("fallback visible\r\n")
    snap = screen.snapshot()
    assert snap.get("source") != "tmux"
    assert any("fallback visible" in line for line in snap["lines"])


# ---------------------------------------------------------------------------
# TmuxScreen caching and delegation tests
# ---------------------------------------------------------------------------


def test_tmux_screen_cache_returns_cached_snapshot(monkeypatch, terminal):
    """Same seq + within cache timeout returns the cached snapshot."""
    from qterminator.tmux_screen import TmuxScreen

    class _Proc:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    call_count = [0]

    def fake_run(argv, **_kwargs):
        call_count[0] += 1
        if "capture-pane" in argv:
            return _Proc(stdout="cached-line\n")
        return _Proc(stdout="0,0,80,24\n")

    monkeypatch.setattr("qterminator.tmux_screen.subprocess.run", fake_run)
    screen = TmuxScreen(terminal, "sess-1")
    snap1 = screen.snapshot()
    count_after_first = call_count[0]
    snap2 = screen.snapshot()
    # Second call should reuse cache, so no new subprocess calls
    assert call_count[0] == count_after_first
    assert snap1 is snap2


def test_tmux_screen_cache_invalidated_on_new_data(monkeypatch, terminal):
    """Cache is invalidated when the underlying seq changes."""
    from qterminator.tmux_screen import TmuxScreen

    class _Proc:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    call_count = [0]

    def fake_run(argv, **_kwargs):
        call_count[0] += 1
        if "capture-pane" in argv:
            return _Proc(stdout="line\n")
        return _Proc(stdout="0,0,80,24\n")

    monkeypatch.setattr("qterminator.tmux_screen.subprocess.run", fake_run)
    screen = TmuxScreen(terminal, "sess-2")
    snap1 = screen.snapshot()
    count_after_first = call_count[0]
    # Feed new data to change latest_seq
    screen.feed("new data")
    snap2 = screen.snapshot()
    # New subprocess calls should have occurred
    assert call_count[0] > count_after_first
    assert snap2 is not snap1


def test_tmux_screen_tail_delegates_to_fallback(monkeypatch, terminal):
    """TmuxScreen.tail() delegates to the fallback ShadowScreen."""
    from qterminator.tmux_screen import TmuxScreen

    monkeypatch.setattr(
        "qterminator.tmux_screen.subprocess.run",
        lambda *a, **k: type("P", (), {"stdout": "", "stderr": "", "returncode": 1})(),
    )
    screen = TmuxScreen(terminal, "sess-3")
    screen.feed("hello")
    screen.feed(" world")
    seq, data = screen.tail(0)
    assert data == b"hello world"
    assert seq == 2


def test_tmux_screen_chunks_delegates_to_fallback(monkeypatch, terminal):
    """TmuxScreen.chunks() delegates to the fallback ShadowScreen."""
    from qterminator.tmux_screen import TmuxScreen

    monkeypatch.setattr(
        "qterminator.tmux_screen.subprocess.run",
        lambda *a, **k: type("P", (), {"stdout": "", "stderr": "", "returncode": 1})(),
    )
    screen = TmuxScreen(terminal, "sess-4")
    screen.feed("chunk1")
    screen.feed("chunk2")
    chunks = screen.chunks()
    assert len(chunks) == 2
    assert chunks[0][1] == b"chunk1"
    assert chunks[1][1] == b"chunk2"


def test_tmux_screen_add_remove_listener_delegates(monkeypatch, terminal):
    """TmuxScreen.add_listener/remove_listener delegate to fallback."""
    from qterminator.tmux_screen import TmuxScreen

    monkeypatch.setattr(
        "qterminator.tmux_screen.subprocess.run",
        lambda *a, **k: type("P", (), {"stdout": "", "stderr": "", "returncode": 1})(),
    )
    screen = TmuxScreen(terminal, "sess-5")
    events = []
    cb = lambda seq, raw: events.append((seq, raw))
    screen.add_listener(cb)
    screen.feed("data")
    assert len(events) == 1
    assert events[0] == (1, b"data")
    screen.remove_listener(cb)
    screen.feed("more")
    assert len(events) == 1  # callback was removed


def test_tmux_screen_feed_delegates_to_fallback(monkeypatch, terminal):
    """TmuxScreen.feed() delegates to the fallback ShadowScreen."""
    from qterminator.tmux_screen import TmuxScreen

    monkeypatch.setattr(
        "qterminator.tmux_screen.subprocess.run",
        lambda *a, **k: type("P", (), {"stdout": "", "stderr": "", "returncode": 1})(),
    )
    screen = TmuxScreen(terminal, "sess-6")
    screen.feed("abc")
    assert screen.latest_seq == 1
    _, data = screen.tail(0)
    assert data == b"abc"


def test_registry_falls_back_when_resolver_returns_none(terminal):
    """Registry uses ShadowScreen when resolver returns None."""
    reg = ShadowScreenRegistry()
    reg.set_tmux_resolver(lambda _term: None)
    handle = reg.acquire(terminal)
    try:
        assert isinstance(handle.shadow, ShadowScreen)
        assert not hasattr(handle.shadow, "_session")  # not a TmuxScreen
    finally:
        handle.release()


def test_registry_falls_back_when_resolver_raises(terminal):
    """Registry uses ShadowScreen when resolver raises an exception."""
    reg = ShadowScreenRegistry()

    def bad_resolver(_term):
        raise RuntimeError("resolver broke")

    reg.set_tmux_resolver(bad_resolver)
    handle = reg.acquire(terminal)
    try:
        assert isinstance(handle.shadow, ShadowScreen)
    finally:
        handle.release()


def test_tmux_screen_snapshot_empty_pane_output(monkeypatch, terminal):
    """TmuxScreen snapshot with empty pane output still returns valid dict."""
    from qterminator.tmux_screen import TmuxScreen

    class _Proc:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_run(argv, **_kwargs):
        if "capture-pane" in argv:
            return _Proc(stdout="")  # empty pane
        return _Proc(stdout="0,0,80,24\n")

    monkeypatch.setattr("qterminator.tmux_screen.subprocess.run", fake_run)
    screen = TmuxScreen(terminal, "empty-pane")
    snap = screen.snapshot()
    assert snap["source"] == "tmux"
    assert snap["cols"] == 80
    assert snap["rows"] == 24
    assert len(snap["lines"]) == 24
    # All lines should be blank (padded to cols width)
    for line in snap["lines"]:
        assert line.strip() == ""


def test_tmux_screen_snapshot_malformed_display_message(monkeypatch, terminal):
    """TmuxScreen snapshot with partial/malformed display-message output."""
    from qterminator.tmux_screen import TmuxScreen

    class _Proc:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_run(argv, **_kwargs):
        if "capture-pane" in argv:
            return _Proc(stdout="content\n")
        # Malformed: only two comma-separated values instead of four
        return _Proc(stdout="3,2\n")

    monkeypatch.setattr("qterminator.tmux_screen.subprocess.run", fake_run)
    screen = TmuxScreen(terminal, "malformed-meta")
    snap = screen.snapshot()
    assert snap["source"] == "tmux"
    assert snap["cursor"]["x"] == 3
    assert snap["cursor"]["y"] == 2
    # cols and rows should fall back to defaults (80 and 24) for empty values
    assert snap["cols"] == 80
    assert snap["rows"] == 24


def test_registry_resolver_reuses_tmux_screen_for_same_terminal(terminal):
    """Second acquire for same terminal reuses the existing TmuxScreen."""
    from qterminator.tmux_screen import TmuxScreen

    reg = ShadowScreenRegistry()
    reg.set_tmux_resolver(lambda _term: "shared-sess")
    h1 = reg.acquire(terminal)
    h2 = reg.acquire(terminal)
    try:
        assert isinstance(h1.shadow, TmuxScreen)
        assert h1.shadow is h2.shadow  # same instance
        assert reg.refcount(terminal) == 2
    finally:
        h1.release()
        h2.release()
