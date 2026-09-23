"""Tests for the shell_integration plugin.

Three layers:
  - Pure OSC parser unit tests (no Qt, no widgets) — fast.
  - Service-level tests against a real ShadowScreenRegistry +
    TerminalWidget (still no real shell — we feed bytes synthetically).
  - Agent-control / MCP surface tests verifying ``cwd_reported``,
    ``last_command``, and ``rpc_command_history`` light up when the
    plugin's service is on the window.
"""

import json
import time

import pytest
import qterminator.config as config_mod
from qterminator.config import Config

pytest.importorskip("pyte")


# ---------------------------------------------------------------------------
# Fixtures (mirrored from test_shadow_screen.py / test_agent_control.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
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


# Convenience for synthesising the standard escape sequences.
def osc(code: str, payload: str = "") -> bytes:
    body = f"{code};{payload}" if payload != "" else code
    return ("\x1b]" + body + "\x1b\\").encode("utf-8")


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------

from qterminator.plugins.shell_integration import (  # noqa: E402
    CommandHistory,
    CommandStartEvent,
    OSCParser,
    ShellIntegrationPlugin,
    ShellIntegrationService,
    _decode_file_uri,
)


def feed_chunks(parser: OSCParser, *chunks: bytes) -> None:
    """Feed each chunk with a monotonically increasing seq number."""
    for i, raw in enumerate(chunks, start=1):
        parser.feed(i, raw)


def test_osc133_full_command_cycle_records_history():
    h = CommandHistory()
    p = OSCParser(h)
    # ;A prompt-start, ;B command-start, ;C output-start, ;D;0 finish.
    feed_chunks(
        p,
        osc("133", "A"),
        osc("133", "B"),
        osc("133", "C"),
        b"hello\r\n",
        osc("133", "D", ),  # placeholder, but our osc() needs payload form
    )
    # Use explicit ;D;0 second pass — exercise the path that records
    # an exit status.
    h2 = CommandHistory()
    p2 = OSCParser(h2)
    feed_chunks(
        p2,
        osc("133", "A"),
        osc("133", "B"),
        osc("133", "C"),
        b"hello\r\n",
        osc("133", "D;0"),
    )
    assert len(h2.history) == 1
    rec = h2.history[0]
    assert rec.exit_status == 0
    assert rec.output_seq_range[0] >= 1
    assert rec.output_seq_range[1] >= rec.output_seq_range[0]


def test_osc133_records_nonzero_exit():
    h = CommandHistory()
    p = OSCParser(h)
    feed_chunks(
        p,
        osc("133", "A") + osc("133", "B") + osc("133", "C"),
        b"oops\r\n",
        osc("133", "D;127"),
    )
    assert h.last.exit_status == 127


def test_osc133_missing_exit_code_is_none():
    h = CommandHistory()
    p = OSCParser(h)
    feed_chunks(
        p,
        osc("133", "A") + osc("133", "B") + osc("133", "C"),
        osc("133", "D"),
    )
    assert h.last is not None
    assert h.last.exit_status is None


def test_osc133_split_across_chunks_still_parses():
    """A ;D sequence split mid-payload must still be recognised on the
    *second* chunk via the carry-over buffer."""
    h = CommandHistory()
    p = OSCParser(h)
    full = osc("133", "A") + osc("133", "B") + osc("133", "C")
    feed_chunks(p, full)
    # Split the ;D;0 sequence across two chunks.
    d_bytes = osc("133", "D;0")
    cut = len(d_bytes) - 3   # before the terminator
    p.feed(2, d_bytes[:cut])
    assert h.last is None, "should not record on partial sequence"
    p.feed(3, d_bytes[cut:])
    assert h.last is not None
    assert h.last.exit_status == 0


def test_osc7_updates_cwd():
    h = CommandHistory()
    p = OSCParser(h)
    feed_chunks(p, osc("7", "file://localhost/home/alice"))
    assert h.cwd == "/home/alice"
    # Subsequent ;A picks up the new cwd onto the pending command.
    feed_chunks(p,
        osc("133", "A") + osc("133", "B"),
        osc("7", "file://localhost/tmp"),
        osc("133", "C"),
        osc("133", "D;0"),
    )
    assert h.cwd == "/tmp"
    assert h.last.cwd == "/tmp"  # retroactively updated to current cwd


def test_osc7_ignores_non_file_uri():
    h = CommandHistory()
    p = OSCParser(h)
    feed_chunks(p, osc("7", "http://example.com/"))
    assert h.cwd is None


def test_osc7_unquotes_percent_encoding():
    assert _decode_file_uri("file://host/path/with%20space") == "/path/with space"


def test_osc8_hyperlink_range_captured():
    h = CommandHistory()
    p = OSCParser(h)
    # Open at chunk 1, close at chunk 3.
    p.feed(1, osc("8", ";https://example.com/"))
    p.feed(2, b"clickable text")
    p.feed(3, osc("8", ";"))
    assert len(h.hyperlinks) == 1
    link = h.hyperlinks[0]
    assert link.url == "https://example.com/"
    assert link.start_seq == 1
    assert link.end_seq == 3


def test_osc8_unclosed_link_keeps_open():
    h = CommandHistory()
    p = OSCParser(h)
    p.feed(1, osc("8", ";https://e.com/"))
    assert h.hyperlinks[0].end_seq == 1
    assert h._open_link is not None


def test_subscriber_fires_exactly_once_per_command():
    h = CommandHistory()
    seen = []
    p = OSCParser(h, on_command_finished=lambda r: seen.append(r))
    full = (osc("133", "A") + osc("133", "B") + osc("133", "C")
            + osc("133", "D;0"))
    feed_chunks(p, full)
    assert len(seen) == 1
    assert seen[0].exit_status == 0


def test_started_subscriber_fires_once_at_C():
    """;C fires the start subscriber with a populated CommandStartEvent."""
    h = CommandHistory()
    started = []
    p = OSCParser(h, capture_command_text=True,
                  on_command_started=lambda ev: started.append(ev))
    p.feed(1, osc("133", "A") + osc("133", "B"))
    p.feed(2, b"ls -la")
    p.feed(3, osc("133", "C"))
    assert len(started) == 1
    ev = started[0]
    assert isinstance(ev, CommandStartEvent)
    assert ev.text == "ls -la"
    assert ev.started_at > 0.0
    assert ev.started_at_monotonic > 0.0
    # ;D should still fire its own subscribers downstream.
    finished = []
    p.add_subscriber(lambda r: finished.append(r))
    p.feed(4, osc("133", "D;0"))
    assert len(started) == 1
    assert len(finished) == 1


def test_started_subscriber_without_explicit_B():
    """Shells that omit ;B (only ;A → ;C) still produce a start event."""
    h = CommandHistory()
    started = []
    p = OSCParser(h, on_command_started=lambda ev: started.append(ev))
    p.feed(1, osc("133", "A") + osc("133", "C"))
    assert len(started) == 1


def test_started_subscriber_exception_swallowed():
    h = CommandHistory()
    def bad(_ev): raise RuntimeError("boom")
    p = OSCParser(h, on_command_started=bad)
    # A second well-behaved subscriber must still get the event.
    seen = []
    p.add_started_subscriber(lambda ev: seen.append(ev))
    p.feed(1, osc("133", "A") + osc("133", "B") + osc("133", "C"))
    assert len(seen) == 1


def test_subscriber_exception_swallowed():
    h = CommandHistory()
    def bad(_r): raise RuntimeError("boom")
    p = OSCParser(h, on_command_finished=bad)
    p.add_subscriber(lambda r: None)
    p.feed(1, osc("133", "A") + osc("133", "B") + osc("133", "C") + osc("133", "D;0"))
    assert h.last is not None  # still recorded


@pytest.mark.cheat_aware(
    protects="typed command text is NOT captured into history unless "
    "capture_command_text is explicitly enabled",
    severity="high",
    cheats=[
        "default capture_command_text to True",
        "weaken the assert from `is None` to a truthy/`!=` check",
        "stop feeding the secret command between ;B and ;C",
    ],
    consequence="secrets typed at the prompt (passwords, tokens) would be "
    "recorded in command history that agents/RPC can read",
)
def test_capture_command_text_off_by_default():
    h = CommandHistory()
    p = OSCParser(h, capture_command_text=False)
    p.feed(1, osc("133", "A") + osc("133", "B"))
    p.feed(2, b"my-secret-command")
    p.feed(3, osc("133", "C") + osc("133", "D;0"))
    assert h.last.text is None


def test_capture_command_text_when_enabled():
    h = CommandHistory()
    p = OSCParser(h, capture_command_text=True)
    p.feed(1, osc("133", "A") + osc("133", "B"))
    # The typed command between ;B and ;C.
    p.feed(2, b"ls -la")
    p.feed(3, osc("133", "C") + osc("133", "D;0"))
    assert h.last.text == "ls -la"


def test_history_limit_truncates_oldest():
    h = CommandHistory(limit=3)
    p = OSCParser(h)
    for _ in range(5):
        p.feed(1, osc("133", "A") + osc("133", "B") + osc("133", "C") + osc("133", "D;0"))
    assert len(h.history) == 3


@pytest.mark.cheat_aware(
    protects="the OSC parser's carry buffer stays bounded under a flood of "
    "unterminated escape bytes",
    severity="high",
    cheats=[
        "raise or remove MAX_CARRY so the bound is never hit",
        "shrink `big` below MAX_CARRY so the overflow path is untested",
        "loosen `<=` to a comparison that always holds",
    ],
    consequence="malicious terminal output (bare ESC flood) could grow the "
    "parser buffer without limit, an unbounded-memory DoS",
)
def test_carry_buffer_bounded_under_garbage():
    """A stream of bare ESC bytes must not grow the carry buffer
    unboundedly — we should drop the head."""
    h = CommandHistory()
    p = OSCParser(h)
    big = b"\x1b" * (OSCParser.MAX_CARRY * 2)
    p.feed(1, big)
    assert len(p._carry) <= OSCParser.MAX_CARRY


def test_bel_terminator_also_accepted():
    """OSC sequences can terminate with BEL (0x07), not just ESC \\."""
    h = CommandHistory()
    p = OSCParser(h)
    raw = b"\x1b]133;A\x07\x1b]133;B\x07\x1b]133;C\x07\x1b]133;D;0\x07"
    p.feed(1, raw)
    assert h.last is not None
    assert h.last.exit_status == 0


# ---------------------------------------------------------------------------
# Service against a real ShadowScreenRegistry + TerminalWidget
# ---------------------------------------------------------------------------

from qterminator.shadow_screen import ShadowScreenRegistry  # noqa: E402


def test_service_ensure_attached_is_idempotent(terminal):
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg)
    h1 = svc.ensure_attached(terminal)
    h2 = svc.ensure_attached(terminal)
    assert h1 is h2
    assert reg.refcount(terminal) == 1


def test_service_detach_releases_registry_handle(terminal):
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg)
    svc.ensure_attached(terminal)
    assert reg.refcount(terminal) == 1
    svc.detach(terminal)
    assert reg.refcount(terminal) == 0


def test_service_drives_history_via_shadow_screen(terminal):
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg)
    history = svc.ensure_attached(terminal)
    # Feed the shadow as if the PTY had emitted these.
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("\x1b]7;file://h/home/u\x1b\\")
    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\")
    shadow.feed("\x1b]133;C\x1b\\")
    shadow.feed("\x1b]133;D;0\x1b\\")
    assert history.cwd == "/home/u"
    assert history.last.exit_status == 0


def test_service_global_command_finished_subscription(terminal):
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg)
    seen = []
    svc.subscribe_command_finished(lambda t, rec: seen.append((t, rec)))
    svc.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed(
        "\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\\x1b]133;D;42\x1b\\"
    )
    assert len(seen) == 1
    t, rec = seen[0]
    assert t is terminal
    assert rec.exit_status == 42


def test_service_global_command_started_subscription(terminal):
    """A ``subscribe_command_started`` subscriber sees the originating
    terminal and a populated CommandStartEvent on ;C. Unsubscribing
    the same callable stops further events.
    """
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg)
    started = []

    def cb(t, ev):
        started.append((t, ev))

    svc.subscribe_command_started(cb)
    svc.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    assert len(started) == 1
    t, ev = started[0]
    assert t is terminal
    assert isinstance(ev, CommandStartEvent)

    # Unsubscribing the exact callback stops further fires.
    svc.unsubscribe_command_started(cb)
    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    assert len(started) == 1, "callback fired after unsubscribe"

    # Unsubscribing a callback that was never registered is a no-op.
    svc.unsubscribe_command_started(lambda *_: None)


def test_service_serialize_last_command(terminal):
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg)
    svc.ensure_attached(terminal)
    assert svc.serialize_last_command(terminal) is None
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed(
        "\x1b]7;file://h/tmp\x1b\\"
        "\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\\x1b]133;D;3\x1b\\"
    )
    d = svc.serialize_last_command(terminal)
    assert d["exit_status"] == 3
    assert d["cwd"] == "/tmp"
    assert "started_at" in d and "finished_at" in d


def test_service_serialize_history_respects_limit(terminal):
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg)
    svc.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]
    for i in range(5):
        shadow.feed(
            f"\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\\x1b]133;D;{i}\x1b\\"
        )
    out = svc.serialize_history(terminal, limit=3)
    assert len(out) == 3
    assert [r["exit_status"] for r in out] == [2, 3, 4]


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def test_plugin_activate_installs_service_and_default_enabled(qtbot, terminal):
    """activate() with default config installs app_controller.shell_integration."""
    class FakeWindow:
        def __init__(self):
            self.shadow_screens = ShadowScreenRegistry()
    w = FakeWindow()
    p = ShellIntegrationPlugin()
    p.activate(w)
    try:
        assert hasattr(w, "shell_integration")
        assert isinstance(w.shell_integration, ShellIntegrationService)
        # capture_command_text default is False
        assert w.shell_integration.capture_command_text is False
    finally:
        p.deactivate()
        assert not hasattr(w, "shell_integration")


def test_plugin_disabled_by_config_does_not_install_service(monkeypatch):
    class FakeWindow:
        shadow_screens = ShadowScreenRegistry()
    cfg = Config()
    cfg.set("plugins", "shell_integration", "enabled", False)
    w = FakeWindow()
    p = ShellIntegrationPlugin()
    p.activate(w)
    try:
        assert not hasattr(w, "shell_integration")
    finally:
        p.deactivate()


def test_plugin_honors_capture_text_config(monkeypatch):
    cfg = Config()
    cfg.set("plugins", "shell_integration", "capture_command_text", True)
    class FakeWindow:
        shadow_screens = ShadowScreenRegistry()
    w = FakeWindow()
    p = ShellIntegrationPlugin()
    p.activate(w)
    try:
        assert w.shell_integration.capture_command_text is True
    finally:
        p.deactivate()


def test_plugin_refuses_window_without_shadow_registry():
    class BareWindow:
        pass
    p = ShellIntegrationPlugin()
    with pytest.raises(RuntimeError, match="shadow_screens"):
        p.activate(BareWindow())


# ---------------------------------------------------------------------------
# Integration with agent_control / MCP surface
# ---------------------------------------------------------------------------

@pytest.fixture
def window_with_agent_control(qtbot, tmp_path, monkeypatch):
    """A real MainWindow with agent_control enabled. The shell_integration
    plugin is enabled by default for this fixture too."""
    monkeypatch.setenv("QTERMINATOR_AGENT_CONTROL", "1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(900, 600)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(150)
    yield win


def _rpc(qtbot, win, method, **params):
    """Call agent_control over its real Unix socket."""
    import socket as _s
    import threading
    plugin = win._plugin_manager._instances.get("agent_control")
    sock = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    sock.connect(plugin.socket_path)
    rid = 1
    req = json.dumps({"jsonrpc": "2.0", "id": rid,
                      "method": method, "params": params}) + "\n"
    sock.sendall(req.encode("utf-8"))
    result = [None]
    done = threading.Event()
    def reader():
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            line, _, _rest = buf.partition(b"\n")
            result[0] = json.loads(line)
        finally:
            done.set()
    threading.Thread(target=reader, daemon=True).start()
    end = time.monotonic() + 5.0
    while not done.is_set() and time.monotonic() < end:
        qtbot.wait(15)
    sock.close()
    if result[0] is None:
        raise TimeoutError(method)
    return result[0]


def test_agent_control_list_tabs_has_shell_integration_fields(qtbot, window_with_agent_control):
    resp = _rpc(qtbot, window_with_agent_control, "list_tabs")
    tab = resp["result"][0]
    assert "cwd_reported" in tab
    assert "last_command" in tab
    # No shell output has produced OSC sequences yet.
    assert tab["cwd_reported"] is None
    assert tab["last_command"] is None


def test_agent_control_list_tabs_reports_last_command(qtbot, window_with_agent_control):
    """Drive an OSC 133/7 sequence through the real shadow and confirm
    that list_tabs surfaces last_command + cwd_reported."""
    win = window_with_agent_control
    svc = win.shell_integration
    # Locate the live terminal and ensure attachment.
    plugin = win._plugin_manager._instances.get("agent_control")
    term_widget = plugin._enumerate_terminals()[0]
    svc.ensure_attached(term_widget)
    shadow = win.shadow_screens._shadows[id(term_widget)][0]
    shadow.feed(
        "\x1b]7;file://h/var/log\x1b\\"
        "\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\\x1b]133;D;0\x1b\\"
    )
    resp = _rpc(qtbot, win, "list_tabs")
    tab = resp["result"][0]
    assert tab["cwd_reported"] == "/var/log"
    assert tab["last_command"]["exit_status"] == 0


def test_rpc_command_history_returns_records(qtbot, window_with_agent_control):
    win = window_with_agent_control
    plugin = win._plugin_manager._instances.get("agent_control")
    term_widget = plugin._enumerate_terminals()[0]
    win.shell_integration.ensure_attached(term_widget)
    shadow = win.shadow_screens._shadows[id(term_widget)][0]
    shadow.feed(
        "\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\\x1b]133;D;1\x1b\\"
    )
    shadow.feed(
        "\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\\x1b]133;D;2\x1b\\"
    )
    resp = _rpc(qtbot, win, "command_history",
                tab_id=id(term_widget), limit=10)
    records = resp["result"]["records"]
    assert [r["exit_status"] for r in records] == [1, 2]


def test_rpc_command_history_when_plugin_disabled(qtbot, window_with_agent_control):
    """Removing the service from the window must surface as a clear
    RPC error rather than a silent zero-record response."""
    win = window_with_agent_control
    saved = win.shell_integration
    del win.shell_integration
    try:
        plugin = win._plugin_manager._instances.get("agent_control")
        term_widget = plugin._enumerate_terminals()[0]
        resp = _rpc(qtbot, win, "command_history", tab_id=id(term_widget))
        assert "error" in resp
        assert resp["error"]["code"] == -32007
    finally:
        win.shell_integration = saved


# ---------------------------------------------------------------------------
# Install-helper CLI
# ---------------------------------------------------------------------------

from qterminator.shell_integration_cli import (  # noqa: E402
    main as cli_main,
)


def test_cli_install_writes_hook_and_appends_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    bashrc = tmp_path / "home" / ".bashrc"
    bashrc.write_text("# user content\n")
    rc = cli_main(["install", "bash"])
    assert rc == 0
    hook_path = tmp_path / "data" / "qterminator" / "shell-integration.bash"
    assert hook_path.exists()
    content = bashrc.read_text()
    assert "qterminator/shell-integration.bash" in content


def test_cli_install_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".bashrc").write_text("")
    cli_main(["install", "bash"])
    cli_main(["install", "bash"])
    content = (tmp_path / "home" / ".bashrc").read_text()
    assert content.count("qterminator/shell-integration.bash") == 1


def test_cli_install_zsh_appends_to_zshrc(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    rc = cli_main(["install", "zsh"])
    assert rc == 0
    zshrc = tmp_path / "home" / ".zshrc"
    assert zshrc.exists()
    assert "qterminator/shell-integration.zsh" in zshrc.read_text()


def test_cli_install_fish_drops_into_confd(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = cli_main(["install", "fish"])
    assert rc == 0
    confd = tmp_path / "home" / ".config" / "fish" / "conf.d" / "qterminator.fish"
    assert confd.exists()


def test_cli_print_outputs_hook(capsys):
    rc = cli_main(["print", "bash"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OSC 133" in out
    assert "__qterm_precmd" in out


def test_cli_install_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = cli_main(["install", "bash", "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "data" / "qterminator").exists()
    assert "would write" in capsys.readouterr().out
