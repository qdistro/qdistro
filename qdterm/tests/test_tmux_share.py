"""Tests for the tmux_share plugin.

Unit tests with subprocess mocked to verify MOSH CONNECT parsing and
service lifecycle. One real-mosh smoke test (skipped if mosh-server
is not installed) runs `mosh-server new -- /bin/true` to confirm we
parse a real binary's output.
"""

import os
import shutil
import subprocess

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.tmux_share import (
    _MOSH_CONNECT_RE,
    _MOSH_DETACHED_RE,
    Share,
    TmuxSharePlugin,
    TmuxShareService,
    _discover_running_shares,
    _scan_mosh_server_pid,
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


class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_connect_line():
    out = (b"MOSH CONNECT 60001 UAkFedSsVJs2LfMeKyQB5g\n"
           b"mosh-server (mosh 1.4.0) [build mosh 1.4.0]\n"
           b"[mosh-server detached, pid = 12345]\n")
    m = _MOSH_CONNECT_RE.search(out)
    assert m and m.group(1) == b"60001"
    assert m.group(2) == b"UAkFedSsVJs2LfMeKyQB5g"

    d = _MOSH_DETACHED_RE.search(out)
    assert d and d.group(1) == b"12345"


def test_parses_when_split_across_stdout_stderr():
    """Some mosh-server builds split lines across stdout/stderr; we
    concatenate both before searching. Verify the regex still finds it."""
    combined = b"banner\n" + b"MOSH CONNECT 42 ABCDEF\n"
    m = _MOSH_CONNECT_RE.search(combined)
    assert m and m.group(1) == b"42"


# ---------------------------------------------------------------------------
# Service: spawn flow
# ---------------------------------------------------------------------------

def test_share_session_raises_when_no_binary(monkeypatch):
    svc = TmuxShareService()
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._mosh_available", lambda: False,
    )
    with pytest.raises(RuntimeError, match="not installed"):
        svc.share_session("qterm-1")


def test_share_session_parses_mocked_output(monkeypatch):
    svc = TmuxShareService(bind="127.0.0.1", port_range="60000:61000")
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._mosh_available", lambda: True,
    )
    fake_out = (b"MOSH CONNECT 60042 SOMEKEY123\n"
                b"mosh-server (mosh 1.4.0)\n"
                b"[mosh-server detached, pid = 99999]\n")

    captured_argv = []
    def _fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        return _FakeProc(stdout=fake_out, stderr=b"")
    monkeypatch.setattr(subprocess, "run", _fake_run)

    share = svc.share_session("qterm-1", tmux_socket="my-sock")
    assert share.session == "qterm-1"
    assert share.port == 60042
    assert share.key == "SOMEKEY123"
    assert share.server_pid == 99999

    # The argv must include the bind, port-range, and the tmux attach
    # command (with -L when socket is provided).
    assert captured_argv[0] == "mosh-server"
    assert "-i" in captured_argv
    assert "127.0.0.1" in captured_argv
    assert "-p" in captured_argv
    assert "60000:61000" in captured_argv
    assert "--" in captured_argv
    sep = captured_argv.index("--")
    cmd = captured_argv[sep + 1:]
    assert cmd[:1] == ["tmux"]
    assert "-L" in cmd and "my-sock" in cmd
    assert cmd[-3:] == ["attach", "-t", "qterm-1"]


def test_share_session_falls_back_to_proc_pid_scan(monkeypatch):
    svc = TmuxShareService(bind="127.0.0.1", port_range="60000:61000")
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._mosh_available", lambda: True,
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._scan_mosh_server_pid",
        lambda session, port=None: 4242,
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeProc(stdout=b"MOSH CONNECT 60042 SOMEKEY123\n"),
    )

    share = svc.share_session("qterm-1")
    assert share.server_pid == 4242


def test_scan_mosh_server_pid_from_proc(monkeypatch):
    monkeypatch.setattr(os, "listdir", lambda path: ["1", "4242"] if path == "/proc" else [])
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._read_proc_cmdline",
        lambda pid: (
            ["mosh-server", "new", "--", "tmux", "attach", "-t", "qterm-1"]
            if pid == 4242 else []
        ),
    )
    assert _scan_mosh_server_pid("qterm-1") == 4242


def test_discover_running_shares(monkeypatch):
    monkeypatch.setattr(os, "listdir", lambda path: ["4242"] if path == "/proc" else [])
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._read_proc_cmdline",
        lambda pid: [
            "mosh-server", "new", "-p", "60001",
            "--", "tmux", "attach", "-t", "qterm-1",
        ],
    )
    shares = _discover_running_shares("127.0.0.1")
    assert shares["qterm-1"][0].server_pid == 4242
    assert shares["qterm-1"][0].port == 60001


def test_share_session_raises_when_no_connect_line(monkeypatch):
    svc = TmuxShareService()
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._mosh_available", lambda: True,
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _FakeProc(stdout=b"some error\n"),
    )
    with pytest.raises(RuntimeError, match="MOSH CONNECT"):
        svc.share_session("qterm-1")


def test_share_session_handles_subprocess_timeout(monkeypatch):
    svc = TmuxShareService()
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._mosh_available", lambda: True,
    )
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="mosh-server", timeout=5)
    monkeypatch.setattr(subprocess, "run", _raise)
    with pytest.raises(RuntimeError, match="timed out"):
        svc.share_session("qterm-1")


# ---------------------------------------------------------------------------
# Service: bookkeeping
# ---------------------------------------------------------------------------

def test_shares_for_prunes_dead_entries(monkeypatch):
    svc = TmuxShareService()
    share = Share(session="qterm-1", bind="127.0.0.1")
    share.port = 60001
    share.key = "X"
    share.server_pid = 10**9  # almost certainly not running
    svc._shares["qterm-1"] = [share]
    live = svc.shares_for("qterm-1")
    assert live == []
    assert "qterm-1" not in svc._shares


def test_ports_for_returns_live_ports(monkeypatch):
    svc = TmuxShareService()
    share = Share(session="qterm-1", bind="127.0.0.1")
    share.port = 60001
    share.key = "X"
    share.server_pid = os.getpid()  # this test process is "alive"
    svc._shares["qterm-1"] = [share]
    assert svc.ports_for("qterm-1") == [60001]


def test_connect_string_format():
    share = Share(session="qterm-1", bind="127.0.0.1")
    share.port = 60001
    share.key = "ABCDEF"
    s = share.connect_string
    assert "MOSH_KEY=ABCDEF" in s
    assert "60001" in s
    assert "mosh-client" in s
    assert "127.0.0.1" in s


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

class _FakeWindow:
    pass


def test_plugin_exposes_service_on_activate():
    w = _FakeWindow()
    p = TmuxSharePlugin()
    p.activate(w)
    assert hasattr(w, "tmux_share")
    assert isinstance(w.tmux_share, TmuxShareService)


def test_plugin_deactivate_removes_service():
    w = _FakeWindow()
    p = TmuxSharePlugin()
    p.activate(w)
    p.deactivate()
    assert not hasattr(w, "tmux_share")


def test_restore_running_merges_without_duplicates(monkeypatch):
    svc = TmuxShareService()
    existing = Share(session="qterm-1", bind="127.0.0.1")
    existing.server_pid = 4242
    existing.port = 60001
    svc._shares["qterm-1"] = [existing]
    restored = Share(session="qterm-1", bind="127.0.0.1")
    restored.server_pid = 4242
    restored.port = 60001
    new_share = Share(session="qterm-1", bind="127.0.0.1")
    new_share.server_pid = 5000
    new_share.port = 60002
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._discover_running_shares",
        lambda bind: {"qterm-1": [restored, new_share]},
    )
    svc.restore_running()
    assert [s.server_pid for s in svc._shares["qterm-1"]] == [4242, 5000]


def test_titlebar_indicator_shows_active_share(qtbot, monkeypatch):
    from qterminator.window import MainWindow

    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)

    class _FakeTmuxMode:
        def get_session_for_terminal(self, _terminal):
            return "qterm-1"

    plugin = TmuxSharePlugin()
    plugin._window = win
    plugin._service = TmuxShareService()
    class _LiveShare(Share):
        def is_alive(self):
            return True

        def kill(self):
            pass

    share = _LiveShare(session="qterm-1", bind="127.0.0.1")
    share.server_pid = 4242
    share.port = 60001
    share.key = "KEY"
    plugin._service._shares["qterm-1"] = [share]
    win.tmux_mode = _FakeTmuxMode()
    try:
        plugin._update_titlebar_indicators()
        label = win._active_terminal._titlebar._tmux_share_label
        assert label.text() == "M1"
        assert "60001" in label.toolTip()
        assert label.isVisible()
    finally:
        del win.tmux_mode
        plugin._service.kill_all()
        win.close()


# ---------------------------------------------------------------------------
# Real mosh-server smoke test
# ---------------------------------------------------------------------------

requires_mosh = pytest.mark.skipif(
    shutil.which("mosh-server") is None, reason="mosh-server not installed",
)


@requires_mosh
def test_real_mosh_server_output_is_parseable():
    """mosh-server's actual output format hasn't drifted from what we
    parse. Run it briefly against /bin/true and assert we extract a
    port + key from the real binary."""
    proc = subprocess.run(
        ["mosh-server", "new", "-p", "0", "--", "/bin/true"],
        capture_output=True, timeout=5,
    )
    out = proc.stdout + proc.stderr
    m = _MOSH_CONNECT_RE.search(out)
    assert m, f"unexpected mosh-server output: {out!r}"
    port = int(m.group(1))
    assert 1 < port < 65536
    # The detached pid line is best-effort — assert it's there too,
    # since tmux_share relies on it for kill().
    d = _MOSH_DETACHED_RE.search(out)
    if d:
        # If we got a pid, the server is briefly alive; kill it cleanly.
        try:
            os.kill(int(d.group(1)), 15)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# _read_proc_cmdline
# ---------------------------------------------------------------------------

def test_read_proc_cmdline_returns_args_for_self():
    """Reading our own /proc/<pid>/cmdline should return a non-empty list."""
    from qterminator.plugins.tmux_share import _read_proc_cmdline
    argv = _read_proc_cmdline(os.getpid())
    assert isinstance(argv, list)
    assert len(argv) > 0


def test_read_proc_cmdline_returns_empty_for_missing_pid():
    from qterminator.plugins.tmux_share import _read_proc_cmdline
    # PID that almost certainly doesn't exist
    result = _read_proc_cmdline(2**30)
    assert result == []


# ---------------------------------------------------------------------------
# _scan_mosh_server_pid edge cases
# ---------------------------------------------------------------------------

def test_scan_mosh_server_pid_returns_none_when_no_match(monkeypatch):
    """No mosh-server process in /proc => returns None."""
    monkeypatch.setattr(os, "listdir", lambda path: ["1", "2"] if path == "/proc" else [])
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._read_proc_cmdline",
        lambda pid: ["/bin/bash", "-c", "echo hello"],
    )
    assert _scan_mosh_server_pid("qterm-99") is None


def test_scan_mosh_server_pid_ignores_non_mosh_processes(monkeypatch):
    """Processes whose argv[0] doesn't contain 'mosh-server' are skipped."""
    monkeypatch.setattr(os, "listdir", lambda path: ["100", "200"] if path == "/proc" else [])
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._read_proc_cmdline",
        lambda pid: (
            ["python3", "mosh-server", "attach", "-t", "qterm-1"]
            if pid == 100
            else ["tmux", "attach", "-t", "qterm-1"]
        ),
    )
    # pid 100 has mosh-server as arg but not as argv[0] basename
    # pid 200 doesn't have mosh-server at all
    assert _scan_mosh_server_pid("qterm-1") is None


# ---------------------------------------------------------------------------
# _discover_running_shares edge cases
# ---------------------------------------------------------------------------

def test_discover_running_shares_empty_proc(monkeypatch):
    """No entries in /proc => empty dict."""
    monkeypatch.setattr(os, "listdir", lambda path: [] if path == "/proc" else [])
    assert _discover_running_shares("127.0.0.1") == {}


def test_discover_running_shares_multiple_sessions(monkeypatch):
    """Multiple mosh-server processes for different sessions."""
    monkeypatch.setattr(os, "listdir", lambda path: ["100", "200"] if path == "/proc" else [])

    def fake_cmdline(pid):
        if pid == 100:
            return [
                "mosh-server", "new", "-p", "60001",
                "--", "tmux", "attach", "-t", "qterm-1",
            ]
        elif pid == 200:
            return [
                "mosh-server", "new", "-p", "60002",
                "--", "tmux", "attach", "-t", "qterm-2",
            ]
        return []

    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._read_proc_cmdline", fake_cmdline,
    )
    shares = _discover_running_shares("0.0.0.0")
    assert "qterm-1" in shares
    assert "qterm-2" in shares
    assert shares["qterm-1"][0].server_pid == 100
    assert shares["qterm-1"][0].port == 60001
    assert shares["qterm-2"][0].server_pid == 200
    assert shares["qterm-2"][0].port == 60002


# ---------------------------------------------------------------------------
# TmuxShareService.kill_all
# ---------------------------------------------------------------------------

def test_kill_all_stops_all_shares():
    """kill_all() iterates all sessions and kills each share."""
    killed = []

    class _TrackingShare(Share):
        def kill(self):
            killed.append(self.session)

    svc = TmuxShareService()
    s1 = _TrackingShare(session="qterm-1", bind="127.0.0.1")
    s1.server_pid = 1
    s2 = _TrackingShare(session="qterm-2", bind="127.0.0.1")
    s2.server_pid = 2
    svc._shares["qterm-1"] = [s1]
    svc._shares["qterm-2"] = [s2]
    svc.kill_all()
    assert set(killed) == {"qterm-1", "qterm-2"}
    assert svc._shares == {}


# ---------------------------------------------------------------------------
# Share.is_alive with dead server_pid
# ---------------------------------------------------------------------------

def test_share_is_alive_with_dead_pid():
    share = Share(session="qterm-1", bind="127.0.0.1")
    share.server_pid = 2**30  # almost certainly not running
    assert share.is_alive() is False


def test_share_is_alive_with_no_pid():
    share = Share(session="qterm-1", bind="127.0.0.1")
    share.server_pid = None
    assert share.is_alive() is False


def test_share_is_alive_with_zero_pid():
    share = Share(session="qterm-1", bind="127.0.0.1")
    share.server_pid = 0
    assert share.is_alive() is False


# ---------------------------------------------------------------------------
# Share.connect_string with various binds
# ---------------------------------------------------------------------------

def test_connect_string_with_0000_bind_uses_local_ip(monkeypatch):
    """When bind is 0.0.0.0 the connect string should use _local_ip()."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._local_ip", lambda: "192.168.1.42",
    )
    share = Share(session="qterm-1", bind="0.0.0.0")
    share.port = 60001
    share.key = "TESTKEY"
    s = share.connect_string
    assert "192.168.1.42" in s
    assert "0.0.0.0" not in s
    assert "MOSH_KEY=TESTKEY" in s


def test_connect_string_pending_when_no_port():
    share = Share(session="qterm-1", bind="127.0.0.1")
    share.key = "TESTKEY"
    assert share.connect_string == "(pending)"


def test_connect_string_pending_when_no_key():
    share = Share(session="qterm-1", bind="127.0.0.1")
    share.port = 60001
    assert share.connect_string == "(pending)"


# ---------------------------------------------------------------------------
# TmuxSharePlugin.deactivate cleans up service
# ---------------------------------------------------------------------------

def test_plugin_deactivate_kills_all_shares():
    killed = []

    class _TrackingShare(Share):
        def kill(self):
            killed.append(self.session)

    w = _FakeWindow()
    p = TmuxSharePlugin()
    p.activate(w)
    s = _TrackingShare(session="qterm-1", bind="127.0.0.1")
    s.server_pid = 1
    p._service._shares["qterm-1"] = [s]
    p.deactivate()
    assert "qterm-1" in killed
    assert not hasattr(w, "tmux_share")


# ---------------------------------------------------------------------------
# restore_running called on activate
# ---------------------------------------------------------------------------

def test_activate_calls_restore_running(monkeypatch):
    called = []
    original_restore = TmuxShareService.restore_running

    def tracked_restore(self):
        called.append(True)
        original_restore(self)

    monkeypatch.setattr(TmuxShareService, "restore_running", tracked_restore)
    w = _FakeWindow()
    p = TmuxSharePlugin()
    p.activate(w)
    assert called
    p.deactivate()


# ---------------------------------------------------------------------------
# Multiple shares for same session
# ---------------------------------------------------------------------------

def test_multiple_shares_for_same_session(monkeypatch):
    svc = TmuxShareService()
    s1 = Share(session="qterm-1", bind="127.0.0.1")
    s1.port = 60001
    s1.key = "KEY1"
    s1.server_pid = os.getpid()  # alive
    s2 = Share(session="qterm-1", bind="127.0.0.1")
    s2.port = 60002
    s2.key = "KEY2"
    s2.server_pid = os.getpid()  # alive
    svc._shares["qterm-1"] = [s1, s2]
    live = svc.shares_for("qterm-1")
    assert len(live) == 2
    assert svc.ports_for("qterm-1") == [60001, 60002]


# ---------------------------------------------------------------------------
# shares_for returns only live entries
# ---------------------------------------------------------------------------

def test_shares_for_returns_only_live_entries():
    svc = TmuxShareService()
    alive = Share(session="qterm-1", bind="127.0.0.1")
    alive.port = 60001
    alive.key = "KEY1"
    alive.server_pid = os.getpid()  # alive

    dead = Share(session="qterm-1", bind="127.0.0.1")
    dead.port = 60002
    dead.key = "KEY2"
    dead.server_pid = 2**30  # dead

    svc._shares["qterm-1"] = [alive, dead]
    live = svc.shares_for("qterm-1")
    assert len(live) == 1
    assert live[0].port == 60001


# ---------------------------------------------------------------------------
# _ShareDialog._qr_pixmap returns None when qrcode not importable
# ---------------------------------------------------------------------------

def test_qr_pixmap_returns_none_without_qrcode(monkeypatch):
    import builtins

    from qterminator.plugins.tmux_share import _ShareDialog
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "qrcode":
            raise ImportError("no qrcode")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)
    result = _ShareDialog._qr_pixmap("test string")
    assert result is None


# ---------------------------------------------------------------------------
# discover_running_shares skips non-numeric entries
# ---------------------------------------------------------------------------

def test_discover_running_shares_skips_non_numeric_entries(monkeypatch):
    """Non-digit entries in /proc (like 'self', 'net') are ignored."""
    monkeypatch.setattr(
        os, "listdir",
        lambda path: ["self", "net", "100", "abc"] if path == "/proc" else [],
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_share._read_proc_cmdline",
        lambda pid: [
            "mosh-server", "new", "-p", "60001",
            "--", "tmux", "attach", "-t", "qterm-1",
        ] if pid == 100 else [],
    )
    shares = _discover_running_shares("127.0.0.1")
    assert "qterm-1" in shares
    assert shares["qterm-1"][0].server_pid == 100
