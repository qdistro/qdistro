"""Tests for the tmux_mode plugin.

Two layers:
  - Unit tests with subprocess mocked, covering the service / plugin
    contract in isolation.
  - Integration tests that talk to a real tmux server on a private
    ``-L`` socket — only run when tmux is installed.
"""

import os
import shutil
import subprocess
import time

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.tmux_mode import (
    TmuxModePlugin,
    TmuxModeService,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"),
    )
    # Plugin writes tmux.conf into CONFIG_DIR; also override there.
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode.CONFIG_DIR", str(tmp_path),
    )
    Config._instance = None
    yield
    Config._instance = None


class _FakeProc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _FakeTerminal:
    def __init__(self, pid=12345):
        self._pid = pid
    def shell_pid(self):
        return self._pid


# ---------------------------------------------------------------------------
# Service: pure helpers
# ---------------------------------------------------------------------------

def test_list_sessions_parses_tmux_output(monkeypatch):
    svc = TmuxModeService(prefix="qterm")
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._run_tmux",
        lambda *a, **k: _FakeProc(stdout="qterm-1\nother\nqterm-2\n"),
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    assert svc.list_sessions() == ["qterm-1", "other", "qterm-2"]


def test_list_sessions_handles_no_tmux(monkeypatch):
    svc = TmuxModeService()
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: False,
    )
    assert svc.list_sessions() == []


def test_list_sessions_handles_tmux_failure(monkeypatch):
    svc = TmuxModeService()
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._run_tmux",
        lambda *a, **k: _FakeProc(returncode=1, stderr="no server"),
    )
    assert svc.list_sessions() == []


def test_own_sessions_filters_by_prefix(monkeypatch):
    svc = TmuxModeService(prefix="qterm")
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._run_tmux",
        lambda *a, **k: _FakeProc(stdout="qterm-1\nfoo\nqterm-bg\nbar\n"),
    )
    assert svc.own_sessions() == ["qterm-1", "qterm-bg"]


def test_next_session_name_picks_lowest_gap(monkeypatch):
    svc = TmuxModeService(prefix="qterm")
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._run_tmux",
        lambda *a, **k: _FakeProc(stdout="qterm-1\nqterm-3\n"),
    )
    # 1 and 3 used → next is 2.
    assert svc._next_session_name() == "qterm-2"


def test_shell_for_new_tab_returns_none_when_disabled():
    svc = TmuxModeService(enabled=False)
    assert svc.shell_for_new_tab() is None


def test_shell_for_new_tab_returns_argv_with_conf(monkeypatch):
    svc = TmuxModeService(enabled=True, prefix="qterm",
                          conf_path="/tmp/qterm-tmux.conf")
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._run_tmux",
        lambda *a, **k: _FakeProc(stdout=""),
    )
    argv = svc.shell_for_new_tab(session="qterm-fixed")
    assert argv[:3] == ["tmux", "-f", "/tmp/qterm-tmux.conf"]
    assert argv[3:] == ["new-session", "-A", "-s", "qterm-fixed"]


# ---------------------------------------------------------------------------
# Detection: get_session_for_terminal
# ---------------------------------------------------------------------------

def test_get_session_for_terminal_no_proc(monkeypatch, tmp_path):
    svc = TmuxModeService()
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    # pid that almost certainly has no /proc entry
    assert svc.get_session_for_terminal(_FakeTerminal(pid=10**9)) is None


def test_get_session_for_terminal_non_tmux_comm(monkeypatch, tmp_path):
    svc = TmuxModeService()
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    # Real pid of this Python process — comm is "python3" not "tmux".
    assert svc.get_session_for_terminal(_FakeTerminal(pid=os.getpid())) is None


def test_get_session_for_terminal_match(monkeypatch):
    svc = TmuxModeService()
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    # Stub the /proc read.
    monkeypatch.setattr(
        "builtins.open",
        lambda path, *a, **k: _StubFile("tmux"),
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._run_tmux",
        lambda *a, **k: _FakeProc(stdout="999 foo\n12345 qterm-1\n"),
    )
    assert svc.get_session_for_terminal(_FakeTerminal(pid=12345)) == "qterm-1"


class _StubFile:
    def __init__(self, content): self.content = content
    def read(self): return self.content
    def __enter__(self): return self
    def __exit__(self, *a): return False


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

class _FakeWindow:
    def __init__(self):
        self._shell_provider = None


def test_plugin_disabled_does_not_install_provider(monkeypatch):
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    Config._instance = None
    w = _FakeWindow()
    p = TmuxModePlugin()
    p.activate(w)
    assert w._shell_provider is None
    # Service is still exposed (for detection of user-started tmux).
    assert hasattr(w, "tmux_mode")
    assert w.tmux_mode.enabled is False


def test_plugin_enabled_installs_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    cfg = Config()
    cfg._data.setdefault("plugins", {}).setdefault("tmux_mode", {})["enabled"] = True
    w = _FakeWindow()
    p = TmuxModePlugin()
    p.activate(w)
    assert callable(w._shell_provider)
    assert w.tmux_mode.enabled is True


def test_plugin_deactivate_clears_hook(monkeypatch):
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    cfg = Config()
    cfg._data.setdefault("plugins", {}).setdefault("tmux_mode", {})["enabled"] = True
    w = _FakeWindow()
    p = TmuxModePlugin()
    p.activate(w)
    assert w._shell_provider is not None
    p.deactivate()
    assert w._shell_provider is None
    assert not hasattr(w, "tmux_mode")


def test_plugin_writes_conf_to_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "qterminator.plugins.tmux_mode._tmux_available", lambda: True,
    )
    cfg = Config()
    cfg._data.setdefault("plugins", {}).setdefault("tmux_mode", {})["enabled"] = True
    w = _FakeWindow()
    p = TmuxModePlugin()
    p.activate(w)
    conf = tmp_path / "tmux.conf"
    assert conf.exists()
    text = conf.read_text()
    assert "status off" in text
    assert "set-titles on" in text


# ---------------------------------------------------------------------------
# Real tmux integration (skipped if tmux missing)
# ---------------------------------------------------------------------------

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed",
)


@pytest.fixture
def tmux_socket(tmp_path):
    """Spawn a private tmux server, yield socket name, kill on exit."""
    sock = f"qterm-test-{os.getpid()}-{int(time.time()*1000)}"
    yield sock
    try:
        subprocess.run(["tmux", "-L", sock, "kill-server"],
                       capture_output=True, timeout=2)
    except Exception:
        pass


@requires_tmux
def test_real_tmux_list_and_detect(tmux_socket, monkeypatch):
    # Start a session on a private socket.
    name = "qterm-real"
    r = subprocess.run(
        ["tmux", "-L", tmux_socket, "new-session", "-d", "-s", name,
         "sleep", "30"],
        capture_output=True, timeout=3,
    )
    assert r.returncode == 0, r.stderr

    # Re-route the service's _run_tmux to use the same socket.
    def _run(*args, **kwargs):
        return subprocess.run(
            ["tmux", "-L", tmux_socket, *args],
            capture_output=True, text=True, timeout=2,
        )
    monkeypatch.setattr("qterminator.plugins.tmux_mode._run_tmux", _run)

    svc = TmuxModeService(prefix="qterm")
    sessions = svc.list_sessions()
    assert name in sessions
    assert svc.own_sessions() == [name]
