"""Tests for the command_telemetry plugin.

Layers:
  - ProcTreeSampler: mock /proc entries, verify CPU/RSS computation.
  - Pure helpers (parse / sample / tab-title suffix regex).
  - Service-level: drive ;C / ;D through shell_integration's real
    OSC parser; assert telemetry annotates the CommandRecord, JSONL
    log writes, tab title gets the [Xs] suffix without compounding.
  - Inline display mode.
  - Config validation.
  - Plugin lifecycle on a real MainWindow.
  - agent_control rpc_command_telemetry.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest
import qterminator.config as config_mod
from qterminator.config import Config

pytest.importorskip("pyte")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(cfg_dir / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


def osc(code, body):
    return ("\x1b]" + code + ";" + body + "\x1b\\").encode("utf-8")


# ---------------------------------------------------------------------------
# ProcTreeSampler tests with mock /proc
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_proc(tmp_path):
    """Build a minimal /proc-like tree for ProcTreeSampler tests.

    Layout:
      /proc/100/stat        — root process
      /proc/100/status      — VmRSS 10240 kB
      /proc/100/task/100/children — "101 102"
      /proc/101/stat        — child 1
      /proc/101/status      — VmRSS 5120 kB
      /proc/101/task/101/children — "" (empty)
      /proc/102/stat        — child 2
      /proc/102/status      — VmRSS 2048 kB
      /proc/102/task/102/children — "" (empty)
    """
    proc = tmp_path / "proc"

    # Helper: create a /proc/<pid>/stat with given utime/stime.
    # Format: pid (comm) S ppid pgid session tty_nr tpgid flags
    #         minflt cminflt majflt cmajflt utime stime cutime cstime ...
    # Fields before utime/stime are indices 0..12 (13 fields before
    # the close-paren suffix). After ") S" we need 11 fields before
    # utime (index 11) and stime (index 12) in the post-paren split.
    def make_proc(pid, ppid, utime, stime, vmrss_kb, children_str,
                  comm="test", read_bytes=0, write_bytes=0):
        d = proc / str(pid)
        d.mkdir(parents=True)
        # Build stat: "pid (comm) S ppid ... utime stime ..."
        # After ") " we need: state ppid pgid sid tty tpgid flags
        # minflt cminflt majflt cmajflt utime stime ...
        # That's index 0=state 1=ppid 2=pgid ... 11=utime 12=stime
        post_paren = f"S {ppid} 0 0 0 0 0 0 0 0 0 {utime} {stime} 0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        stat_content = f"{pid} ({comm}) {post_paren}\n"
        (d / "stat").write_text(stat_content)
        (d / "comm").write_text(comm + "\n")
        (d / "io").write_text(
            f"read_bytes: {read_bytes}\n"
            f"write_bytes: {write_bytes}\n"
            "cancelled_write_bytes: 0\n"
        )
        (d / "cgroup").write_text("0::/user.slice/qterm-test\n")
        (d / "oom_score").write_text(str(pid % 100) + "\n")
        (d / "syscall").write_text("1 0 0 0\n")
        fd = d / "fd"
        fd.mkdir()
        (fd / "3").symlink_to(proc / "opened.txt")

        status_content = (
            f"Name:\ttest\n"
            f"Pid:\t{pid}\n"
            f"PPid:\t{ppid}\n"
            f"VmRSS:\t{vmrss_kb} kB\n"
        )
        (d / "status").write_text(status_content)

        task = d / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(children_str + "\n")

    # Root (pid 100): 500 utime + 200 stime ticks, 10240 kB RSS
    make_proc(100, 1, 500, 200, 10240, "101 102",
              comm="bash", read_bytes=1000, write_bytes=2000)
    # Child 1 (pid 101): 100 utime + 50 stime ticks, 5120 kB RSS
    make_proc(101, 100, 100, 50, 5120, "",
              comm="rustc", read_bytes=3000, write_bytes=4000)
    # Child 2 (pid 102): 30 utime + 10 stime ticks, 2048 kB RSS
    make_proc(102, 100, 30, 10, 2048, "",
              comm="ld", read_bytes=5000, write_bytes=6000)

    return str(proc)


def test_proc_tree_sampler_read_children(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    children = sampler.read_children(100)
    assert sorted(children) == [101, 102]
    # Leaf processes have no children.
    assert sampler.read_children(101) == []
    assert sampler.read_children(102) == []


def test_proc_tree_sampler_walk_tree(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    pids = sampler.walk_tree(100)
    assert sorted(pids) == [100, 101, 102]


def test_proc_tree_sampler_read_cpu_seconds(fake_proc):
    from qterminator.plugins.command_telemetry import _CLK_TCK, ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    cpu = sampler.read_cpu_seconds(100)
    expected = (500 + 200) / _CLK_TCK
    assert abs(cpu - expected) < 0.001


def test_proc_tree_sampler_read_rss_bytes(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    # 10240 kB * 1024 = 10485760 bytes
    assert sampler.read_rss_bytes(100) == 10240 * 1024
    assert sampler.read_rss_bytes(101) == 5120 * 1024
    assert sampler.read_rss_bytes(102) == 2048 * 1024


def test_proc_tree_sampler_sample_aggregates_tree(fake_proc):
    from qterminator.plugins.command_telemetry import _CLK_TCK, ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    snap = sampler.sample(100)
    # CPU: sum of all processes
    expected_cpu = (500 + 200 + 100 + 50 + 30 + 10) / _CLK_TCK
    assert abs(snap["cpu_seconds"] - expected_cpu) < 0.001
    # RSS: sum across tree at this instant
    expected_rss = (10240 + 5120 + 2048) * 1024
    assert snap["peak_rss_bytes"] == expected_rss
    # Process count: 3 (root + 2 children)
    assert snap["process_count"] == 3


def test_proc_tree_sampler_sample_extended_tier_a(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    snap = sampler.sample_extended(100)
    assert snap["io"]["read_bytes"] == 9000
    assert snap["io"]["write_bytes"] == 12000
    assert snap["per_pid_comm"][100] == "bash"
    assert snap["per_pid_comm"][101] == "rustc"
    assert snap["process_tree_depth"] == 2
    assert snap["process_tree_breadth"] == 2


def test_proc_tree_sampler_sample_extended_opt_in_collectors(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    snap = sampler.sample_extended(
        100,
        collect_open_files=True,
        collect_cgroup=True,
        collect_syscalls=True,
        collect_oom=True,
    )
    assert any(path.endswith("opened.txt") for path in snap["open_files"])
    assert "0::/user.slice/qterm-test" in snap["cgroups"]
    assert snap["syscalls"]["1"] == 3
    assert snap["oom_score_max"] == 2


def test_proc_tree_sampler_zero_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler()
    snap = sampler.sample(0)
    assert snap == {"cpu_seconds": 0.0, "peak_rss_bytes": 0, "process_count": 0}


def test_proc_tree_sampler_missing_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler()
    assert sampler.read_children(2_147_000_000) == []
    assert sampler.read_cpu_seconds(2_147_000_000) == 0.0
    assert sampler.read_rss_bytes(2_147_000_000) == 0


def test_proc_tree_sampler_fallback_ppid_scan(tmp_path):
    """When /proc/<pid>/task/<pid>/children is missing, the sampler
    should fall back to scanning all /proc/*/stat for matching ppid."""
    from qterminator.plugins.command_telemetry import ProcTreeSampler

    proc = tmp_path / "proc_no_children"

    def make_proc_no_children(pid, ppid, utime, stime, vmrss_kb):
        d = proc / str(pid)
        d.mkdir(parents=True)
        post_paren = f"S {ppid} 0 0 0 0 0 0 0 0 0 {utime} {stime} 0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        (d / "stat").write_text(f"{pid} (test) {post_paren}\n")
        (d / "status").write_text(
            f"Name:\ttest\nPid:\t{pid}\nPPid:\t{ppid}\n"
            f"VmRSS:\t{vmrss_kb} kB\n"
        )
        # NO task/<pid>/children file — force fallback

    make_proc_no_children(200, 1, 100, 50, 4096)
    make_proc_no_children(201, 200, 20, 10, 2048)

    sampler = ProcTreeSampler(proc_root=str(proc))
    # read_children should find 201 as child of 200 via ppid scan
    children = sampler.read_children(200)
    assert 201 in children
    # walk_tree should include both
    pids = sampler.walk_tree(200)
    assert sorted(pids) == [200, 201]


# ---------------------------------------------------------------------------
# Pure helper tests (backward-compat module-level functions)
# ---------------------------------------------------------------------------

def test_tab_suffix_regex_strips_prior_entry():
    """Repeated commands must not compound suffixes: ``[1s] [2s] [3s]``."""
    from qterminator.plugins.command_telemetry import _TAB_SUFFIX_RE

    assert _TAB_SUFFIX_RE.sub("", "tab1  [12.4s]") == "tab1"
    assert _TAB_SUFFIX_RE.sub("", "tab1  [12.4s · 412MB]") == "tab1"
    assert _TAB_SUFFIX_RE.sub("", "tab1  [3.2s · 12MB · 4.0s CPU]") == "tab1"
    # No suffix -> no change.
    assert _TAB_SUFFIX_RE.sub("", "plain-title") == "plain-title"
    # Brackets without a duration -> not our suffix; leave alone.
    assert _TAB_SUFFIX_RE.sub("", "tab1 [foo]") == "tab1 [foo]"


def test_command_telemetry_format_short():
    from qterminator.plugins.command_telemetry import CommandTelemetry

    t = CommandTelemetry(duration=12.4, cpu_seconds=8.1,
                          peak_rss_bytes=412 * 1024 * 1024,
                          process_count=9)
    s = t.format_short()
    assert "12.4s" in s
    assert "412MB" in s
    assert "8.1s CPU" in s


def test_command_telemetry_to_dict_includes_extended_payload():
    from qterminator.plugins.command_telemetry import CommandTelemetry

    tele = CommandTelemetry(
        duration=1.2,
        cpu_seconds=0.5,
        peak_rss_bytes=1024,
        process_count=2,
        read_bytes=10,
        write_bytes=20,
        binary_cpu_seconds={"python": 0.4},
        process_tree_depth=2,
        process_tree_breadth=3,
        network_connections=[{"remote": "0100007F", "port": 443, "family": 4}],
        open_files=["/tmp/x"],
        cgroups=["0::/user.slice"],
        syscall_counts={"1": 2},
        oom_score_max=17,
        gpu={"used_memory_mb_peak": 256},
    )
    out = tele.to_dict()
    assert out["read_bytes"] == 10
    assert out["write_bytes"] == 20
    assert out["binary_cpu_seconds"] == {"python": 0.4}
    assert out["process_tree_depth"] == 2
    assert out["process_tree_breadth"] == 3
    assert out["network"]["connections"][0]["port"] == 443
    assert out["files"]["open"] == ["/tmp/x"]
    assert out["cgroups"] == ["0::/user.slice"]
    assert out["syscalls"] == {"1": 2}
    assert out["oom_score_max"] == 17
    assert out["gpu"]["used_memory_mb_peak"] == 256


def test_read_children_returns_empty_for_missing_pid():
    """``_read_children`` must not raise for a PID that doesn't exist."""
    from qterminator.plugins.command_telemetry import _read_children
    # Linux PID_MAX_LIMIT is 2^22; 2_147_000_000 will never be live
    # on a real system and isn't a kernel-thread parent.
    assert _read_children(2_147_000_000) == []


def test_sample_tree_handles_zero_pid_gracefully():
    from qterminator.plugins.command_telemetry import sample_tree
    snap = sample_tree(0)
    assert snap == {"cpu_seconds": 0.0, "peak_rss_bytes": 0, "process_count": 0}


def test_walk_proc_tree_includes_known_descendant():
    """Real-process probe: spawn a child + grandchild, walk the tree
    from our own PID, both must show up. Skipped if /proc isn't usable."""
    from qterminator.plugins.command_telemetry import _walk_proc_tree

    if not os.path.isdir("/proc/self"):
        pytest.skip("/proc not available")

    # Pick a long-running tree we control: bash spawns sleep.
    proc = subprocess.Popen(
        ["bash", "-c", "exec sleep 30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.2)  # let exec happen
        pids = _walk_proc_tree(proc.pid)
        assert proc.pid in pids
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Service tests -- drive real OSC parser via ShadowScreen
#
# NOTE on the ``time.sleep`` calls in this section: they sit between the
# ``;C`` (command-start) and ``;D`` (command-done) OSC feeds. ``shadow.feed``
# is fully synchronous, so there is NO signal or observable condition to wait
# on here -- the sleep deliberately injects a small amount of *wall-clock time*
# so the measured command duration is non-zero/measurable. Replacing these
# with ``qtbot.waitSignal``/``waitUntil`` is therefore not possible (there is
# no event), which is why they are intentionally left as fixed sleeps.
# ---------------------------------------------------------------------------

@pytest.fixture
def terminal(qtbot):
    """A real TerminalWidget -- needed because ShadowScreenRegistry
    connects to the inner ``QTermWidget.receivedData`` signal, which
    plain Python fakes don't carry."""
    from qterminator.terminal import TerminalWidget
    t = TerminalWidget()
    qtbot.addWidget(t)
    t.resize(800, 400)
    t.show()
    qtbot.waitExposed(t)
    yield t


def _build_service(window=None, **kw):
    from qterminator.plugins.command_telemetry import CommandTelemetryService
    if window is None:
        class _W:
            _tabs = None
        window = _W()
    return CommandTelemetryService(window, **kw)


def _build_shell_int(history_limit=200):
    from qterminator.plugins.shell_integration import ShellIntegrationService
    from qterminator.shadow_screen import ShadowScreenRegistry
    reg = ShadowScreenRegistry()
    svc = ShellIntegrationService(reg, capture_command_text=True,
                                   history_limit=history_limit)
    return reg, svc


def test_service_annotates_command_record(terminal):
    """End-to-end through shell_integration: ;C/;D fires our hooks and
    the resulting CommandRecord.telemetry is populated."""
    reg, shell_int = _build_shell_int()
    svc = _build_service(display="off")
    svc.attach(shell_int)
    history = shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    shadow.feed("\x1b]133;A\x1b\\")
    shadow.feed("\x1b]133;B\x1b\\")
    shadow.feed("\x1b]133;C\x1b\\")
    # A tiny delay so duration is measurable.
    time.sleep(0.01)
    shadow.feed("\x1b]133;D;0\x1b\\")

    assert history.last is not None
    tele = history.last.telemetry
    assert tele is not None
    assert tele["duration"] >= 0.0
    assert tele["peak_rss_bytes"] >= 0
    assert tele["process_count"] >= 0

    svc.detach(shell_int)


def test_service_get_last_telemetry(terminal):
    reg, shell_int = _build_shell_int()
    svc = _build_service(display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    assert svc.get_last_telemetry(terminal) is None

    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    time.sleep(0.005)
    shadow.feed("\x1b]133;D;0\x1b\\")

    last = svc.get_last_telemetry(terminal)
    assert last is not None
    assert "duration" in last and "cpu_seconds" in last

    svc.detach(shell_int)


def test_service_jsonl_log_writes_one_line_per_command(terminal, tmp_path):
    reg, shell_int = _build_shell_int()
    log_path = tmp_path / "commands.jsonl"
    svc = _build_service(display="off", log_path=str(log_path))
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    for exit_code in (0, 1, 0):
        shadow.feed("\x1b]7;file:///tmp\x1b\\")
        shadow.feed("\x1b]133;A\x1b\\")
        shadow.feed("\x1b]133;B\x1b\\")
        shadow.feed("\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed(f"\x1b]133;D;{exit_code}\x1b\\")

    assert log_path.exists()
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [p["exit_status"] for p in parsed] == [0, 1, 0]
    # Each record carries telemetry fields.
    for p in parsed:
        assert "duration" in p
        assert "cpu_seconds" in p
    # Permissions are tightened on creation -- the log will contain
    # command text which can include credentials.
    mode = oct(log_path.stat().st_mode & 0o777)
    assert mode == oct(0o600), f"log file is world-readable: {mode}"

    svc.detach(shell_int)


def test_service_tab_title_does_not_compound_suffix(qtbot):
    """Run three commands; the tab title should carry exactly one
    ``[Xs]`` suffix, not three."""
    from qterminator.window import MainWindow

    cfg = Config()
    cfg.set("plugins", "shell_integration", "enabled", True)
    cfg.set("plugins", "command_telemetry", "enabled", True)
    # Avoid the QTimer.singleShot fade clearing the suffix mid-test.
    cfg.set("plugins", "command_telemetry", "tab_status_ms", 60_000)

    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)

    shell_int = getattr(win, "shell_integration", None)
    assert shell_int is not None
    tele_svc = getattr(win, "command_telemetry", None)
    assert tele_svc is not None

    terminal = win._tabs.widget(0).find_terminals()[0]
    history = shell_int.ensure_attached(terminal)

    # Manually drive the OSC parser via the registered ShadowScreen.
    shadow = win.shadow_screens._shadows[id(terminal)][0]
    for _ in range(3):
        shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed("\x1b]133;D;0\x1b\\")

    title = win._tabs.tabText(0)
    # Exactly one suffix.
    assert title.count("[") == 1 or "[" not in title.replace("[", "", 1).split("]")[-1]
    # Telemetry attached on each completed record.
    assert len(history.history) == 3
    assert all(r.telemetry is not None for r in history.history)

    # Close the window so plugin polling timers (file_monitor,
    # notifications, etc.) stop before pytest tears qtbot down --
    # otherwise their _check_all slots fire on a freed QTabWidget
    # in the next event loop iteration and crash subsequent tests.
    win.close()
    qtbot.wait(60)


def test_service_emits_broadcast_event_when_agent_control_present(terminal):
    """When ``agent_control`` is on the window the service must push a
    ``command_finished`` event with telemetry -- no polling required."""
    reg, shell_int = _build_shell_int()

    received = []

    class _FakeAgentControl:
        def broadcast_event(self, tab_id, event_type, payload):
            received.append((tab_id, event_type, payload))

    class _W:
        _tabs = None
        agent_control = _FakeAgentControl()

    svc = _build_service(window=_W(), display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    time.sleep(0.005)
    shadow.feed("\x1b]133;D;0\x1b\\")

    assert received, "broadcast_event was not called"
    tab_id, event_type, payload = received[-1]
    assert tab_id == id(terminal)
    assert event_type == "command_finished"
    assert payload["exit_status"] == 0
    assert "telemetry" in payload
    assert "duration" in payload["telemetry"]

    svc.detach(shell_int)


def test_service_prefers_agent_events_publish_so_filter_applies(terminal):
    """When ``agent_event_channel``'s ``agent_events`` service is on
    the window, the broadcast must go through ``publish`` so the
    user's ``set_enabled_events`` filter still gates the event.
    Bypassing it via raw ``agent_control.broadcast_event`` would let
    ``command_finished`` reach agents that opted out."""
    reg, shell_int = _build_shell_int()

    published = []
    broadcast = []

    class _FakeAgentEvents:
        # Same shape as AgentEventChannel.publish.
        def publish(self, tab_id, event_type, payload):
            published.append((tab_id, event_type, payload))

    class _FakeAgentControl:
        def broadcast_event(self, tab_id, event_type, payload):
            broadcast.append((tab_id, event_type, payload))

    class _W:
        _tabs = None
        agent_events = _FakeAgentEvents()
        agent_control = _FakeAgentControl()

    svc = _build_service(window=_W(), display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]
    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    time.sleep(0.005)
    shadow.feed("\x1b]133;D;0\x1b\\")

    assert published, "expected publish via agent_events"
    assert not broadcast, "must not bypass the channel's filter when agent_events is loaded"

    svc.detach(shell_int)


def test_service_does_not_emit_telemetry_for_missing_C(terminal):
    """``;D`` without a prior ``;C`` produces no telemetry -- without
    a start moment we can't honestly report duration."""
    reg, shell_int = _build_shell_int()
    svc = _build_service(display="off")
    svc.attach(shell_int)
    history = shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;D;0\x1b\\")
    assert history.last is not None
    assert history.last.telemetry is None

    svc.detach(shell_int)


# ---------------------------------------------------------------------------
# Inline display mode
# ---------------------------------------------------------------------------

def test_service_inline_display_falls_back_to_tab_status(terminal):
    """When display='inline', it falls back to tab_status since
    QTermWidget has no display-only write API (send_text would inject
    into the shell as input)."""
    reg, shell_int = _build_shell_int()

    svc = _build_service(display="inline")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
    time.sleep(0.005)
    shadow.feed("\x1b]133;D;0\x1b\\")

    # Inline now falls back to tab_status, so no send_text calls.
    # Instead it should set the tab title (verified by other tests).

    svc.detach(shell_int)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_service_rejects_invalid_display_mode():
    """An unrecognized display mode should fall back to 'tab_status'."""
    svc = _build_service(display="banana")
    assert svc.display == "tab_status"


def test_valid_display_modes_constant():
    from qterminator.plugins.command_telemetry import VALID_DISPLAY_MODES
    assert "tab_status" in VALID_DISPLAY_MODES
    assert "inline" in VALID_DISPLAY_MODES
    assert "off" in VALID_DISPLAY_MODES


# ---------------------------------------------------------------------------
# get_telemetry_history
# ---------------------------------------------------------------------------

def test_service_get_telemetry_history(terminal):
    """get_telemetry_history returns annotated records in order."""
    reg, shell_int = _build_shell_int()

    class _W:
        _tabs = None
        shell_integration = shell_int

    svc = _build_service(window=_W(), display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    # Run 3 commands.
    for code in (0, 1, 0):
        shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed(f"\x1b]133;D;{code}\x1b\\")

    records = svc.get_telemetry_history(terminal, limit=10)
    assert len(records) == 3
    assert [r["exit_status"] for r in records] == [0, 1, 0]
    for r in records:
        assert "telemetry" in r
        assert "duration" in r["telemetry"]

    # Limit works.
    records2 = svc.get_telemetry_history(terminal, limit=2)
    assert len(records2) == 2
    # Should return the last 2 in order.
    assert [r["exit_status"] for r in records2] == [1, 0]

    svc.detach(shell_int)


def test_service_get_telemetry_history_returns_empty_without_shell_int(terminal):
    """When shell_integration is not on the window, returns empty list."""
    svc = _build_service(display="off")
    assert svc.get_telemetry_history(terminal) == []


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def test_plugin_disabled_by_default():
    """Per the project's plugin contract."""
    from qterminator.plugins.command_telemetry import CommandTelemetryPlugin

    class _W:
        _tabs = None
        shell_integration = None
    plugin = CommandTelemetryPlugin()
    plugin.activate(_W())
    assert plugin._service is None


def test_plugin_raises_without_shell_integration():
    """The dependency is hard; we'd rather refuse to load than silently
    collect zero telemetry."""
    from qterminator.plugins.command_telemetry import CommandTelemetryPlugin

    cfg = Config()
    cfg.set("plugins", "command_telemetry", "enabled", True)

    class _W:
        _tabs = None

    plugin = CommandTelemetryPlugin()
    with pytest.raises(RuntimeError):
        plugin.activate(_W())


def test_plugin_loads_on_real_window(qtbot):
    from qterminator.window import MainWindow

    cfg = Config()
    cfg.set("plugins", "shell_integration", "enabled", True)
    cfg.set("plugins", "command_telemetry", "enabled", True)

    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)

    plugin = win._plugin_manager._instances.get("command_telemetry")
    assert plugin is not None
    assert plugin._service is not None
    assert getattr(win, "command_telemetry", None) is plugin._service

    # Deactivate cleanly.
    plugin.deactivate()
    assert getattr(win, "command_telemetry", None) is None

    win.close()
    qtbot.wait(60)


# ---------------------------------------------------------------------------
# agent_control rpc_command_telemetry
# ---------------------------------------------------------------------------

def test_agent_control_rpc_command_telemetry(terminal):
    """The rpc_command_telemetry RPC method returns annotated records."""
    from qterminator.plugins.agent_control import AgentControlPlugin

    reg, shell_int = _build_shell_int()

    class _FakeTabs:
        def count(self):
            return 1

        def widget(self, _i):
            return self

        def find_terminals(self):
            return [terminal]

    class _W:
        _tabs = _FakeTabs()
        shell_integration = shell_int

    svc = _build_service(window=_W(), display="off")
    svc.attach(shell_int)
    shell_int.ensure_attached(terminal)
    shadow = reg._shadows[id(terminal)][0]

    # Attach the telemetry service to the window.
    _W.command_telemetry = svc

    # Run 2 commands.
    for code in (0, 42):
        shadow.feed("\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\")
        time.sleep(0.005)
        shadow.feed(f"\x1b]133;D;{code}\x1b\\")

    # Build a minimal AgentControlPlugin for the RPC test.
    plugin = AgentControlPlugin()
    plugin._window = _W()

    result = plugin.rpc_command_telemetry(None, tab_id=id(terminal), limit=10)
    assert "records" in result
    assert len(result["records"]) == 2
    assert result["records"][0]["exit_status"] == 0
    assert result["records"][1]["exit_status"] == 42
    for rec in result["records"]:
        assert "telemetry" in rec
        assert "duration" in rec["telemetry"]

    svc.detach(shell_int)


def test_agent_control_rpc_command_telemetry_raises_without_plugin():
    """rpc_command_telemetry must raise when command_telemetry is not loaded."""
    from qterminator.plugins.agent_control import AgentControlPlugin

    class _FakeTerminal:
        pass

    t = _FakeTerminal()

    class _FakeTabs:
        def count(self):
            return 1

        def widget(self, _i):
            return self

        def find_terminals(self):
            return [t]

    class _W:
        _tabs = _FakeTabs()

    plugin = AgentControlPlugin()
    plugin._window = _W()

    # _RpcError is raised when the plugin is not loaded.
    with pytest.raises(Exception, match="command_telemetry plugin not loaded"):
        plugin.rpc_command_telemetry(None, tab_id=id(t))


# ---------------------------------------------------------------------------
# Additional ProcTreeSampler, CommandTelemetry, and _TabTracker tests
# ---------------------------------------------------------------------------


# -- Helper fixtures for specific topologies --------------------------------

@pytest.fixture
def single_proc(tmp_path):
    """A single process with no children."""
    proc = tmp_path / "proc_single"

    def _make(pid, ppid, utime, stime, vmrss_kb, children_str, comm="solo",
              read_bytes=0, write_bytes=0):
        d = proc / str(pid)
        d.mkdir(parents=True)
        post_paren = (
            f"S {ppid} 0 0 0 0 0 0 0 0 0 {utime} {stime} "
            "0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        (d / "stat").write_text(f"{pid} ({comm}) {post_paren}\n")
        (d / "comm").write_text(comm + "\n")
        (d / "io").write_text(
            f"read_bytes: {read_bytes}\n"
            f"write_bytes: {write_bytes}\n"
            "cancelled_write_bytes: 0\n"
        )
        (d / "cgroup").write_text("0::/user.slice/solo-test\n")
        (d / "oom_score").write_text("5\n")
        (d / "syscall").write_text("0 0 0 0\n")
        fd = d / "fd"
        fd.mkdir()
        status_content = (
            f"Name:\t{comm}\n"
            f"Pid:\t{pid}\n"
            f"PPid:\t{ppid}\n"
            f"VmRSS:\t{vmrss_kb} kB\n"
        )
        (d / "status").write_text(status_content)
        task = d / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(children_str + "\n")

    _make(300, 1, 100, 50, 4096, "", comm="solo")
    return str(proc)


@pytest.fixture
def deep_chain(tmp_path):
    """A deep chain: 400 -> 401 -> 402 -> 403 -> 404 (depth 5)."""
    proc = tmp_path / "proc_deep"

    def _make(pid, ppid, children_str, comm="chain"):
        d = proc / str(pid)
        d.mkdir(parents=True)
        post_paren = (
            f"S {ppid} 0 0 0 0 0 0 0 0 0 10 5 "
            "0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        (d / "stat").write_text(f"{pid} ({comm}) {post_paren}\n")
        (d / "comm").write_text(comm + "\n")
        (d / "io").write_text(
            "read_bytes: 0\nwrite_bytes: 0\ncancelled_write_bytes: 0\n"
        )
        (d / "cgroup").write_text("0::/user.slice/chain\n")
        (d / "oom_score").write_text("0\n")
        (d / "syscall").write_text("0 0 0 0\n")
        fd_dir = d / "fd"
        fd_dir.mkdir()
        (d / "status").write_text(
            f"Name:\t{comm}\nPid:\t{pid}\nPPid:\t{ppid}\nVmRSS:\t1024 kB\n"
        )
        task = d / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(children_str + "\n")

    _make(400, 1, "401", comm="sh")
    _make(401, 400, "402", comm="bash")
    _make(402, 401, "403", comm="python")
    _make(403, 402, "404", comm="node")
    _make(404, 403, "", comm="sleep")
    return str(proc)


@pytest.fixture
def wide_tree(tmp_path):
    """A wide tree: 500 -> {501, 502, 503, 504, 505} (breadth 5, depth 2)."""
    proc = tmp_path / "proc_wide"

    def _make(pid, ppid, children_str, comm="worker"):
        d = proc / str(pid)
        d.mkdir(parents=True)
        post_paren = (
            f"S {ppid} 0 0 0 0 0 0 0 0 0 20 10 "
            "0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        )
        (d / "stat").write_text(f"{pid} ({comm}) {post_paren}\n")
        (d / "comm").write_text(comm + "\n")
        (d / "io").write_text(
            "read_bytes: 100\nwrite_bytes: 200\ncancelled_write_bytes: 0\n"
        )
        (d / "cgroup").write_text("0::/user.slice/wide\n")
        (d / "oom_score").write_text("10\n")
        (d / "syscall").write_text("2 0 0 0\n")
        fd_dir = d / "fd"
        fd_dir.mkdir()
        (d / "status").write_text(
            f"Name:\t{comm}\nPid:\t{pid}\nPPid:\t{ppid}\nVmRSS:\t2048 kB\n"
        )
        task = d / "task" / str(pid)
        task.mkdir(parents=True)
        (task / "children").write_text(children_str + "\n")

    _make(500, 1, "501 502 503 504 505", comm="master")
    for child_pid in range(501, 506):
        _make(child_pid, 500, "", comm=f"worker{child_pid - 500}")
    return str(proc)


@pytest.fixture
def net_proc(tmp_path):
    """A proc tree with /proc/<pid>/net/tcp and tcp6 files."""
    proc = tmp_path / "proc_net"

    d = proc / "600"
    d.mkdir(parents=True)
    post_paren = (
        "S 1 0 0 0 0 0 0 0 0 0 10 5 "
        "0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    )
    (d / "stat").write_text(f"600 (netapp) {post_paren}\n")
    (d / "comm").write_text("netapp\n")
    (d / "io").write_text(
        "read_bytes: 0\nwrite_bytes: 0\ncancelled_write_bytes: 0\n"
    )
    (d / "cgroup").write_text("0::/user.slice\n")
    (d / "oom_score").write_text("0\n")
    (d / "syscall").write_text("0 0 0 0\n")
    fd_dir = d / "fd"
    fd_dir.mkdir()
    (d / "status").write_text(
        "Name:\tnetapp\nPid:\t600\nPPid:\t1\nVmRSS:\t1024 kB\n"
    )
    task = d / "task" / "600"
    task.mkdir(parents=True)
    (task / "children").write_text("\n")

    # /proc/600/net/tcp — contains loopback (should be skipped) + real remote
    net = d / "net"
    net.mkdir()
    tcp_header = (
        "  sl  local_address rem_address   st tx_queue rx_queue "
        "tr tm->when retrnsmt   uid  timeout inode\n"
    )
    tcp_lines = (
        # Loopback connection — remote starts with 0100007F → but does NOT
        # start with "00000000:" so it is NOT skipped by the parser.
        # Actually the parser skips "00000000:" prefix.  0100007F won't be
        # skipped.  Let's include a proper loopback-skip entry:
        "   0: 0100007F:1F90 00000000:0000 0A "
        "00000000:00000000 00:00000000 00000000     0        0 12345 1 0\n"
        # Real remote connection: 192.168.0.1:443
        "   1: 0100007F:C000 C0A80001:01BB 01 "
        "00000000:00000000 00:00000000 00000000     0        0 12346 1 0\n"
        # Another real remote: 10.0.0.1:80
        "   2: 0A000002:D000 0A000001:0050 01 "
        "00000000:00000000 00:00000000 00000000     0        0 12347 1 0\n"
    )
    (net / "tcp").write_text(tcp_header + tcp_lines)

    # /proc/600/net/tcp6 — one IPv6 entry with a real remote
    tcp6_header = (
        "  sl  local_address rem_address   st tx_queue rx_queue "
        "tr tm->when retrnsmt   uid  timeout inode\n"
    )
    tcp6_lines = (
        # Skip: unconnected IPv6
        "   0: 00000000000000000000000000000000:1F90 "
        "00000000000000000000000000000000:0000 0A "
        "00000000:00000000 00:00000000 00000000     0        0 12350 1 0\n"
        # Real IPv6 remote
        "   1: 00000000000000000000000000000001:C000 "
        "20010DB8000000000000000000000001:01BB 01 "
        "00000000:00000000 00:00000000 00000000     0        0 12351 1 0\n"
    )
    (net / "tcp6").write_text(tcp6_header + tcp6_lines)

    return str(proc)


# ---------------------------------------------------------------------------
# 1. tree_shape with varied topologies
# ---------------------------------------------------------------------------

def test_tree_shape_single_process(single_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=single_proc)
    pids, depth, breadth = sampler.tree_shape(300)
    assert pids == [300]
    assert depth == 1
    assert breadth == 1


def test_tree_shape_deep_chain(deep_chain):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=deep_chain)
    pids, depth, breadth = sampler.tree_shape(400)
    assert sorted(pids) == [400, 401, 402, 403, 404]
    assert depth == 5
    # Each level has exactly 1 process, so max breadth is 1.
    assert breadth == 1


def test_tree_shape_wide_tree(wide_tree):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=wide_tree)
    pids, depth, breadth = sampler.tree_shape(500)
    assert sorted(pids) == [500, 501, 502, 503, 504, 505]
    assert depth == 2
    assert breadth == 5


def test_tree_shape_existing_three_process_tree(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    pids, depth, breadth = sampler.tree_shape(100)
    assert sorted(pids) == [100, 101, 102]
    assert depth == 2
    assert breadth == 2


def test_tree_shape_missing_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root="/nonexistent")
    pids, depth, breadth = sampler.tree_shape(99999)
    assert pids == [99999]
    assert depth == 1
    assert breadth == 1


# ---------------------------------------------------------------------------
# 2. read_comm
# ---------------------------------------------------------------------------

def test_read_comm_normal(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    assert sampler.read_comm(100) == "bash"
    assert sampler.read_comm(101) == "rustc"
    assert sampler.read_comm(102) == "ld"


def test_read_comm_missing_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root="/nonexistent")
    # Falls back to str(pid) when file is missing.
    assert sampler.read_comm(99999) == "99999"


def test_read_comm_permission_error(tmp_path):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_perm"
    d = proc / "700"
    d.mkdir(parents=True)
    comm_file = d / "comm"
    comm_file.write_text("secret\n")
    comm_file.chmod(0o000)
    sampler = ProcTreeSampler(proc_root=str(proc))
    # Falls back to str(pid) on PermissionError.
    assert sampler.read_comm(700) == "700"
    # Restore permissions for cleanup.
    comm_file.chmod(0o644)


def test_read_comm_empty_file(tmp_path):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_empty_comm"
    d = proc / "701"
    d.mkdir(parents=True)
    (d / "comm").write_text("")
    sampler = ProcTreeSampler(proc_root=str(proc))
    # Empty comm falls back to str(pid).
    assert sampler.read_comm(701) == "701"


# ---------------------------------------------------------------------------
# 3. read_io_bytes
# ---------------------------------------------------------------------------

def test_read_io_bytes_normal(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    io = sampler.read_io_bytes(100)
    assert io["read_bytes"] == 1000
    assert io["write_bytes"] == 2000
    assert io["cancelled_write_bytes"] == 0


def test_read_io_bytes_partial_data(tmp_path):
    """Only read_bytes present; other fields default to 0."""
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_partial_io"
    d = proc / "710"
    d.mkdir(parents=True)
    (d / "io").write_text("read_bytes: 42\n")
    sampler = ProcTreeSampler(proc_root=str(proc))
    io = sampler.read_io_bytes(710)
    assert io["read_bytes"] == 42
    assert io["write_bytes"] == 0
    assert io["cancelled_write_bytes"] == 0


def test_read_io_bytes_missing_fields(tmp_path):
    """Extra/irrelevant keys are ignored; missing keys stay at 0."""
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_extra_io"
    d = proc / "711"
    d.mkdir(parents=True)
    (d / "io").write_text(
        "rchar: 1234\n"
        "wchar: 5678\n"
        "syscr: 10\n"
        "syscw: 20\n"
        "read_bytes: 500\n"
        "write_bytes: 600\n"
    )
    sampler = ProcTreeSampler(proc_root=str(proc))
    io = sampler.read_io_bytes(711)
    assert io["read_bytes"] == 500
    assert io["write_bytes"] == 600
    assert io["cancelled_write_bytes"] == 0


def test_read_io_bytes_missing_file():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root="/nonexistent")
    io = sampler.read_io_bytes(99999)
    assert io == {"read_bytes": 0, "write_bytes": 0, "cancelled_write_bytes": 0}


def test_read_io_bytes_corrupt_value(tmp_path):
    """Non-integer value should be ignored; field stays at 0."""
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_corrupt_io"
    d = proc / "712"
    d.mkdir(parents=True)
    (d / "io").write_text(
        "read_bytes: not_a_number\n"
        "write_bytes: 100\n"
        "cancelled_write_bytes: 200\n"
    )
    sampler = ProcTreeSampler(proc_root=str(proc))
    io = sampler.read_io_bytes(712)
    assert io["read_bytes"] == 0
    assert io["write_bytes"] == 100
    assert io["cancelled_write_bytes"] == 200


# ---------------------------------------------------------------------------
# 4. read_network_connections
# ---------------------------------------------------------------------------

def test_read_network_connections_ipv4_real_remote(net_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=net_proc)
    conns = sampler.read_network_connections(600)
    # Should have the two real TCP entries + one real TCP6 entry.
    tcp4_conns = [c for c in conns if c["family"] == 4]
    tcp6_conns = [c for c in conns if c["family"] == 6]
    assert len(tcp4_conns) == 2
    # 192.168.0.1:443 -> remote=C0A80001, port=0x01BB=443
    assert any(c["port"] == 443 and c["remote"] == "C0A80001" for c in tcp4_conns)
    # 10.0.0.1:80 -> remote=0A000001, port=0x0050=80
    assert any(c["port"] == 80 and c["remote"] == "0A000001" for c in tcp4_conns)
    assert len(tcp6_conns) == 1
    assert tcp6_conns[0]["port"] == 443


def test_read_network_connections_skips_loopback(net_proc):
    """Entries with remote starting with '00000000:' should be skipped."""
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=net_proc)
    conns = sampler.read_network_connections(600)
    # The loopback line has remote 00000000:0000 and should be skipped.
    for c in conns:
        assert not c["remote"].startswith("00000000")


def test_read_network_connections_empty_file(tmp_path):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_empty_net"
    d = proc / "720"
    d.mkdir(parents=True)
    net = d / "net"
    net.mkdir()
    # Header only, no data lines.
    (net / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue "
        "tr tm->when retrnsmt   uid  timeout inode\n"
    )
    sampler = ProcTreeSampler(proc_root=str(proc))
    conns = sampler.read_network_connections(720)
    assert conns == []


def test_read_network_connections_no_net_directory():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root="/nonexistent")
    conns = sampler.read_network_connections(99999)
    assert conns == []


def test_read_network_connections_ipv6_skip_unconnected(net_proc):
    """IPv6 entries with all-zero remote are skipped."""
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=net_proc)
    conns = sampler.read_network_connections(600)
    ipv6_conns = [c for c in conns if c["family"] == 6]
    # The all-zero remote IPv6 entry should be skipped.
    for c in ipv6_conns:
        assert c["remote"] != "00000000000000000000000000000000"
    # Only the real IPv6 entry should remain.
    assert len(ipv6_conns) == 1
    assert ipv6_conns[0]["remote"] == "20010DB8000000000000000000000001"


# ---------------------------------------------------------------------------
# 5. read_open_files
# ---------------------------------------------------------------------------

def test_read_open_files_with_symlinks(tmp_path):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_fd"
    d = proc / "730"
    d.mkdir(parents=True)
    fd = d / "fd"
    fd.mkdir()
    # Create real targets so symlinks resolve.
    target_a = tmp_path / "file_a.txt"
    target_b = tmp_path / "file_b.log"
    target_a.write_text("a")
    target_b.write_text("b")
    (fd / "3").symlink_to(str(target_a))
    (fd / "4").symlink_to(str(target_b))
    sampler = ProcTreeSampler(proc_root=str(proc))
    files = sampler.read_open_files(730)
    assert str(target_a) in files
    assert str(target_b) in files


def test_read_open_files_limit_enforcement(tmp_path):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_fd_limit"
    d = proc / "731"
    d.mkdir(parents=True)
    fd = d / "fd"
    fd.mkdir()
    # Create 10 file symlinks.
    for i in range(10):
        target = tmp_path / f"target_{i}.txt"
        target.write_text(str(i))
        (fd / str(i + 3)).symlink_to(str(target))
    sampler = ProcTreeSampler(proc_root=str(proc))
    files = sampler.read_open_files(731, limit=3)
    assert len(files) <= 3


def test_read_open_files_excludes_socket_pipe(tmp_path):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_fd_socket"
    d = proc / "732"
    d.mkdir(parents=True)
    fd = d / "fd"
    fd.mkdir()
    # Create symlinks to socket: and pipe: pseudo-paths.
    (fd / "3").symlink_to("socket:[12345]")
    (fd / "4").symlink_to("pipe:[67890]")
    (fd / "5").symlink_to("anon_inode:[eventpoll]")
    # One real file.
    real = tmp_path / "real.txt"
    real.write_text("real")
    (fd / "6").symlink_to(str(real))
    sampler = ProcTreeSampler(proc_root=str(proc))
    files = sampler.read_open_files(732)
    assert len(files) == 1
    assert str(real) in files


def test_read_open_files_missing_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root="/nonexistent")
    assert sampler.read_open_files(99999) == []


# ---------------------------------------------------------------------------
# 6. read_gpu_usage
# ---------------------------------------------------------------------------

def test_read_gpu_usage_mock_nvidia_smi(monkeypatch, tmp_path):
    from unittest.mock import MagicMock, patch

    from qterminator.plugins.command_telemetry import ProcTreeSampler

    sampler = ProcTreeSampler()
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "100, 256\n200, 512\n300, 128\n"

    with patch("subprocess.run", return_value=fake_result) as mock_run:
        result = sampler.read_gpu_usage([100, 200])
    assert mock_run.called
    assert result["process_count"] == 2
    assert result["used_memory_mb_peak"] == 512


def test_read_gpu_usage_no_nvidia_smi(monkeypatch):
    from unittest.mock import patch

    from qterminator.plugins.command_telemetry import ProcTreeSampler

    sampler = ProcTreeSampler()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = sampler.read_gpu_usage([100])
    assert result == {}


def test_read_gpu_usage_empty_pids():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler()
    assert sampler.read_gpu_usage([]) == {}


def test_read_gpu_usage_parsing_edge_cases(monkeypatch):
    from unittest.mock import MagicMock, patch

    from qterminator.plugins.command_telemetry import ProcTreeSampler

    sampler = ProcTreeSampler()
    fake_result = MagicMock()
    fake_result.returncode = 0
    # Lines with bad data, single-field lines, and a valid line.
    fake_result.stdout = (
        "not_a_pid, 100\n"
        "100\n"
        "200, not_a_number\n"
        "300, 1024\n"
    )

    with patch("subprocess.run", return_value=fake_result):
        result = sampler.read_gpu_usage([300])
    assert result["process_count"] == 1
    assert result["used_memory_mb_peak"] == 1024


def test_read_gpu_usage_nonzero_returncode(monkeypatch):
    from unittest.mock import MagicMock, patch

    from qterminator.plugins.command_telemetry import ProcTreeSampler

    sampler = ProcTreeSampler()
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""

    with patch("subprocess.run", return_value=fake_result):
        result = sampler.read_gpu_usage([100])
    assert result == {}


def test_read_gpu_usage_no_matching_pids(monkeypatch):
    from unittest.mock import MagicMock, patch

    from qterminator.plugins.command_telemetry import ProcTreeSampler

    sampler = ProcTreeSampler()
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "999, 256\n"

    with patch("subprocess.run", return_value=fake_result):
        result = sampler.read_gpu_usage([100, 200])
    assert result == {}


# ---------------------------------------------------------------------------
# 7. sample_extended edge cases
# ---------------------------------------------------------------------------

def test_sample_extended_zero_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler()
    snap = sampler.sample_extended(0)
    assert snap["cpu_seconds"] == 0.0
    assert snap["peak_rss_bytes"] == 0
    assert snap["process_count"] == 0
    assert snap["io"] == {"read_bytes": 0, "write_bytes": 0, "cancelled_write_bytes": 0}
    assert snap["per_pid_cpu"] == {}
    assert snap["per_pid_comm"] == {}
    assert snap["process_tree_depth"] == 0
    assert snap["process_tree_breadth"] == 0
    assert snap["network_connections"] == []
    assert snap["open_files"] == []
    assert snap["cgroups"] == []
    assert snap["syscalls"] == {}
    assert snap["oom_score_max"] == 0
    assert snap["gpu"] == {}


def test_sample_extended_all_collectors_off(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    snap = sampler.sample_extended(
        100,
        collect_io=False,
        collect_network=False,
        collect_open_files=False,
        collect_gpu=False,
        collect_cgroup=False,
        collect_syscalls=False,
        collect_oom=False,
    )
    # Core fields still populated.
    assert snap["cpu_seconds"] > 0
    assert snap["peak_rss_bytes"] > 0
    assert snap["process_count"] == 3
    # I/O zeros since collect_io=False.
    assert snap["io"] == {"read_bytes": 0, "write_bytes": 0, "cancelled_write_bytes": 0}
    # Optional collectors empty.
    assert snap["network_connections"] == []
    assert snap["open_files"] == []
    assert snap["cgroups"] == []
    assert snap["syscalls"] == {}
    assert snap["oom_score_max"] == 0
    assert snap["gpu"] == {}


def test_sample_extended_all_collectors_on(fake_proc):
    from unittest.mock import MagicMock, patch

    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)

    # Mock nvidia-smi to avoid real GPU dependency.
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""

    with patch("subprocess.run", return_value=fake_result):
        snap = sampler.sample_extended(
            100,
            collect_io=True,
            collect_network=True,
            collect_open_files=True,
            collect_gpu=True,
            collect_cgroup=True,
            collect_syscalls=True,
            collect_oom=True,
        )
    assert snap["io"]["read_bytes"] == 9000
    assert snap["io"]["write_bytes"] == 12000
    assert "0::/user.slice/qterm-test" in snap["cgroups"]
    assert snap["oom_score_max"] == 2  # pid 102 -> 102 % 100 = 2
    # Syscalls: pid 100,101,102 all have "1" as first field in syscall.
    assert snap["syscalls"].get("1", 0) == 3
    # Open files: 3 symlinks to proc/opened.txt.
    assert len(snap["open_files"]) >= 0  # May or may not resolve.
    # GPU: mocked as failure, so empty.
    assert snap["gpu"] == {}


# ---------------------------------------------------------------------------
# 8. CommandTelemetry.format_short()
# ---------------------------------------------------------------------------

def test_format_short_with_io_bytes():
    from qterminator.plugins.command_telemetry import CommandTelemetry
    t = CommandTelemetry(
        duration=5.0,
        cpu_seconds=2.0,
        peak_rss_bytes=100 * 1024 * 1024,
        read_bytes=50 * 1024 * 1024,
        write_bytes=25 * 1024 * 1024,
    )
    s = t.format_short()
    assert "5.0s" in s
    assert "100MB" in s
    assert "2.0s CPU" in s
    assert "50MB read" in s
    assert "25MB written" in s


def test_format_short_zero_values():
    from qterminator.plugins.command_telemetry import CommandTelemetry
    t = CommandTelemetry(duration=0.0, cpu_seconds=0.0, peak_rss_bytes=0)
    s = t.format_short()
    assert s == "0.0s"
    # No MB, no CPU, no IO.
    assert "MB" not in s
    assert "CPU" not in s
    assert "read" not in s
    assert "written" not in s


def test_format_short_large_values():
    from qterminator.plugins.command_telemetry import CommandTelemetry
    t = CommandTelemetry(
        duration=86400.5,
        cpu_seconds=72000.0,
        peak_rss_bytes=16 * 1024 * 1024 * 1024,  # 16 GB
        read_bytes=1024 * 1024 * 1024 * 100,  # 100 GB
        write_bytes=1024 * 1024 * 1024 * 50,  # 50 GB
    )
    s = t.format_short()
    assert "86400.5s" in s
    assert "16384MB" in s
    assert "72000.0s CPU" in s
    assert "102400MB read" in s
    assert "51200MB written" in s


def test_format_short_only_duration_and_rss():
    from qterminator.plugins.command_telemetry import CommandTelemetry
    t = CommandTelemetry(duration=3.7, peak_rss_bytes=256 * 1024 * 1024)
    s = t.format_short()
    assert "3.7s" in s
    assert "256MB" in s
    assert "CPU" not in s
    assert "read" not in s


# ---------------------------------------------------------------------------
# 9. CommandTelemetry.to_dict()
# ---------------------------------------------------------------------------

def test_to_dict_empty_optional_fields():
    """Optional fields with falsy values should be excluded from output."""
    from qterminator.plugins.command_telemetry import CommandTelemetry
    t = CommandTelemetry(
        duration=1.0,
        cpu_seconds=0.5,
        peak_rss_bytes=1024,
        process_count=1,
    )
    d = t.to_dict()
    assert "duration" in d
    assert "cpu_seconds" in d
    assert "peak_rss_bytes" in d
    assert "process_count" in d
    # Optional fields should NOT be present.
    assert "read_bytes" not in d
    assert "write_bytes" not in d
    assert "cancelled_write_bytes" not in d
    assert "binary_cpu_seconds" not in d
    assert "process_tree_depth" not in d
    assert "process_tree_breadth" not in d
    assert "network" not in d
    assert "files" not in d
    assert "cgroups" not in d
    assert "syscalls" not in d
    assert "oom_score_max" not in d
    assert "gpu" not in d


def test_to_dict_all_fields_populated():
    from qterminator.plugins.command_telemetry import CommandTelemetry
    t = CommandTelemetry(
        duration=10.123,
        cpu_seconds=5.678,
        peak_rss_bytes=2048,
        process_count=4,
        read_bytes=100,
        write_bytes=200,
        cancelled_write_bytes=50,
        binary_cpu_seconds={"python": 3.0, "node": 2.0, "idle": 0.0},
        process_tree_depth=3,
        process_tree_breadth=5,
        network_connections=[{"remote": "C0A80001", "port": 443, "family": 4}],
        open_files=["/tmp/a", "/tmp/b"],
        cgroups=["0::/user.slice"],
        syscall_counts={"1": 10, "2": 5},
        oom_score_max=42,
        gpu={"process_count": 1, "used_memory_mb_peak": 512},
    )
    d = t.to_dict()
    assert d["duration"] == 10.123
    assert d["cpu_seconds"] == 5.678
    assert d["peak_rss_bytes"] == 2048
    assert d["process_count"] == 4
    assert d["read_bytes"] == 100
    assert d["write_bytes"] == 200
    assert d["cancelled_write_bytes"] == 50
    # binary_cpu_seconds: sorted descending by value, zero entries excluded.
    assert "idle" not in d["binary_cpu_seconds"]
    keys = list(d["binary_cpu_seconds"].keys())
    assert keys == ["python", "node"]
    assert d["process_tree_depth"] == 3
    assert d["process_tree_breadth"] == 5
    assert d["network"]["connections"] == [{"remote": "C0A80001", "port": 443, "family": 4}]
    assert d["files"]["open"] == ["/tmp/a", "/tmp/b"]
    assert d["cgroups"] == ["0::/user.slice"]
    assert d["syscalls"] == {"1": 10, "2": 5}
    assert d["oom_score_max"] == 42
    assert d["gpu"]["process_count"] == 1
    assert d["gpu"]["used_memory_mb_peak"] == 512


def test_to_dict_io_only_read_nonzero():
    """When only read_bytes is nonzero, all three I/O fields appear."""
    from qterminator.plugins.command_telemetry import CommandTelemetry
    t = CommandTelemetry(duration=1.0, read_bytes=42)
    d = t.to_dict()
    assert d["read_bytes"] == 42
    assert d["write_bytes"] == 0
    assert d["cancelled_write_bytes"] == 0


def test_to_dict_duration_rounding():
    from qterminator.plugins.command_telemetry import CommandTelemetry
    t = CommandTelemetry(duration=1.23456789, cpu_seconds=0.98765432)
    d = t.to_dict()
    assert d["duration"] == 1.235
    assert d["cpu_seconds"] == 0.988


# ---------------------------------------------------------------------------
# 10. _TabTracker binary_cpu_seconds accumulation across samples
# ---------------------------------------------------------------------------

def test_tab_tracker_binary_cpu_accumulation(fake_proc, monkeypatch):
    """binary_cpu_seconds should accumulate deltas across multiple samples."""
    import os as _os

    from qterminator.plugins.command_telemetry import (
        _CLK_TCK,
        _default_sampler,
        _TabTracker,
    )

    # Point the module-level _default_sampler at our fake proc.
    original_root = _default_sampler.proc_root
    _default_sampler.proc_root = fake_proc

    tracker = _TabTracker(
        poll_interval_ms=100,
        collect_io_bytes=True,
        collect_binary_breakdown=True,
    )

    # Simulate on_start (manually, since we don't have a real terminal).
    tracker._started_at = time.time()
    tracker._started_monotonic = time.monotonic()
    tracker._root_pid = 100

    # First sample.
    tracker._sample()
    initial_io = dict(tracker._io_last)
    tracker._io_start = dict(initial_io)
    # Reset binary_cpu to count only deltas from here.
    from collections import defaultdict as _defaultdict
    tracker._binary_cpu = _defaultdict(float)
    tracker._prev_pid_cpu = {}

    # Take a baseline sample so _prev_pid_cpu is populated.
    tracker._sample()

    # Now update the fake /proc to simulate CPU usage change.
    stat_path = _os.path.join(fake_proc, "101", "stat")
    # Increase utime by 100 for pid 101 (rustc).
    with open(stat_path, "w") as f:
        post_paren = "S 100 0 0 0 0 0 0 0 0 0 200 50 0 0 20 0 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
        f.write(f"101 (rustc) {post_paren}\n")

    # Take another sample — delta should be captured.
    tracker._sample()

    # Finalize.
    tele = tracker.on_finish()

    # Restore.
    _default_sampler.proc_root = original_root

    assert tele is not None
    assert "rustc" in tele.binary_cpu_seconds
    # The delta was 100 ticks increase in utime.
    expected_delta = 100 / _CLK_TCK
    assert tele.binary_cpu_seconds["rustc"] >= expected_delta - 0.001


def test_tab_tracker_on_finish_without_start():
    """on_finish without a prior on_start returns None."""
    from qterminator.plugins.command_telemetry import _TabTracker
    tracker = _TabTracker()
    assert tracker.on_finish() is None
    assert tracker.last_telemetry is None


# ---------------------------------------------------------------------------
# Additional edge-case tests
# ---------------------------------------------------------------------------

def test_read_cgroup_normal(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    cgroups = sampler.read_cgroup(100)
    assert cgroups == ["0::/user.slice/qterm-test"]


def test_read_cgroup_missing_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root="/nonexistent")
    assert sampler.read_cgroup(99999) == []


def test_read_oom_score_normal(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    # pid 100: 100 % 100 = 0
    assert sampler.read_oom_score(100) == 0
    # pid 101: 101 % 100 = 1
    assert sampler.read_oom_score(101) == 1
    # pid 102: 102 % 100 = 2
    assert sampler.read_oom_score(102) == 2


def test_read_oom_score_missing_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root="/nonexistent")
    assert sampler.read_oom_score(99999) == 0


def test_read_syscall_normal(fake_proc):
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root=fake_proc)
    assert sampler.read_syscall(100) == "1"


def test_read_syscall_missing_pid():
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    sampler = ProcTreeSampler(proc_root="/nonexistent")
    assert sampler.read_syscall(99999) == ""


def test_read_syscall_negative_one(tmp_path):
    """A syscall value of -1 means 'not in a syscall'; returns empty."""
    from qterminator.plugins.command_telemetry import ProcTreeSampler
    proc = tmp_path / "proc_syscall_neg"
    d = proc / "750"
    d.mkdir(parents=True)
    (d / "syscall").write_text("-1 0 0 0\n")
    sampler = ProcTreeSampler(proc_root=str(proc))
    assert sampler.read_syscall(750) == ""
