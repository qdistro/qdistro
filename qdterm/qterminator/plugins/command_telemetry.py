"""command_telemetry — duration / CPU / memory per command.

Annotates every completed shell command with telemetry: wall-clock
duration, total CPU seconds, peak resident memory across the process
tree, and process count. Surfaces are:

* ``CommandRecord.telemetry`` on shell_integration's history dict (so
  agent_control's ``list_tabs[].last_command.telemetry`` carries it
  for free, and the MCP ``command_history`` tool exposes it).
* An optional ``[12.3s]`` suffix on the tab title (configurable via
  ``display = "tab_status"``), a dim grey inline line after command
  output (``display = "inline"``), or no UI at all (``display = "off"``).
* An optional JSONL append-log under ``~/.local/share/qterminator/``
  (configurable via ``log_path``).
* A push event broadcast through ``agent_control.broadcast_event``
  with ``event_type == "command_finished"`` whenever ``agent_control``
  is loaded — so attached agents are notified without polling.
* An ``rpc_command_telemetry`` method on agent_control returning the
  last N annotated command records for a tab.

Configuration (config.toml):

    [plugins.command_telemetry]
    enabled = false                    # default false; opt-in
    poll_interval_ms = 100             # /proc sampling cadence
    display = "tab_status"             # "tab_status", "inline", or "off"
    log_path = ""                      # empty/null = no JSONL log

SECURITY NOTE: the JSONL log captures ``rec.text`` — the shell command
line — when shell_integration's ``capture_command_text`` is also on.
That includes anything typed at the prompt: ``mysql -p secret``,
``export API_KEY=...``, etc. The log is opt-in (``log_path`` is empty
by default) and is written with ``0o600`` permissions, but anyone
enabling it is implicitly accepting credential persistence. Mirror the
warning from ``qterminator/layout.py`` if you're enabling both this
and ``capture_command_text``.

Requires the ``shell_integration`` plugin (OSC 133 ;C/;D).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass

from PyQt6.QtCore import QTimer

from qterminator.config import Config
from qterminator.plugin import Plugin

log = logging.getLogger("qterminator.command_telemetry")

#: Valid display modes.
VALID_DISPLAY_MODES = ("tab_status", "inline", "off")


# ---------------------------------------------------------------------------
# Telemetry data
# ---------------------------------------------------------------------------

@dataclass
class CommandTelemetry:
    """Telemetry data for a single command."""
    duration: float = 0.0
    cpu_seconds: float = 0.0
    peak_rss_bytes: int = 0
    process_count: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    read_bytes: int = 0
    write_bytes: int = 0
    cancelled_write_bytes: int = 0
    binary_cpu_seconds: dict[str, float] | None = None
    process_tree_depth: int = 0
    process_tree_breadth: int = 0
    network_connections: list[dict] | None = None
    open_files: list[str] | None = None
    cgroups: list[str] | None = None
    syscall_counts: dict[str, int] | None = None
    oom_score_max: int = 0
    gpu: dict | None = None

    def to_dict(self) -> dict:
        out = {
            "duration": round(self.duration, 3),
            "cpu_seconds": round(self.cpu_seconds, 3),
            "peak_rss_bytes": self.peak_rss_bytes,
            "process_count": self.process_count,
        }
        if self.read_bytes or self.write_bytes or self.cancelled_write_bytes:
            out["read_bytes"] = self.read_bytes
            out["write_bytes"] = self.write_bytes
            out["cancelled_write_bytes"] = self.cancelled_write_bytes
        if self.binary_cpu_seconds:
            out["binary_cpu_seconds"] = {
                k: round(v, 3)
                for k, v in sorted(
                    self.binary_cpu_seconds.items(),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                if v > 0
            }
        if self.process_tree_depth:
            out["process_tree_depth"] = self.process_tree_depth
        if self.process_tree_breadth:
            out["process_tree_breadth"] = self.process_tree_breadth
        if self.network_connections:
            out["network"] = {"connections": self.network_connections}
        if self.open_files:
            out["files"] = {"open": self.open_files}
        if self.cgroups:
            out["cgroups"] = self.cgroups
        if self.syscall_counts:
            out["syscalls"] = self.syscall_counts
        if self.oom_score_max:
            out["oom_score_max"] = self.oom_score_max
        if self.gpu:
            out["gpu"] = self.gpu
        return out

    def format_short(self) -> str:
        """Human-readable short form for the tab title."""
        parts = [f"{self.duration:.1f}s"]
        if self.peak_rss_bytes > 0:
            mb = self.peak_rss_bytes / (1024 * 1024)
            parts.append(f"{mb:.0f}MB")
        if self.cpu_seconds > 0:
            parts.append(f"{self.cpu_seconds:.1f}s CPU")
        if self.read_bytes > 0:
            parts.append(f"{self.read_bytes / (1024 * 1024):.0f}MB read")
        if self.write_bytes > 0:
            parts.append(f"{self.write_bytes / (1024 * 1024):.0f}MB written")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Process tree introspection (/proc, Linux-only)
# ---------------------------------------------------------------------------

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


class ProcTreeSampler:
    """Walk /proc to collect CPU time and RSS for a process tree.

    Encapsulates all /proc parsing logic with a configurable ``proc_root``
    for testability. Instantiate with a custom path to point at a fake
    /proc tree in tests.

    The ``proc_root`` parameter (default ``"/proc"``) allows tests to
    point the sampler at a fake filesystem tree.
    """

    def __init__(self, proc_root: str = "/proc"):
        self.proc_root = proc_root

    def read_children(self, pid: int) -> list[int]:
        """Return immediate child PIDs of ``pid``.

        Prefers ``/proc/<pid>/task/<pid>/children`` (cheap; one read).
        Falls back to scanning all ``/proc/*/stat`` entries for matching
        ppid (slower but works on kernels without ``CONFIG_PROC_CHILDREN``).
        """
        try:
            path = f"{self.proc_root}/{pid}/task/{pid}/children"
            with open(path) as f:
                line = f.read().strip()
            if line:
                return [int(c) for c in line.split() if c.isdigit()]
            return []
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            pass
        out: list[int] = []
        try:
            entries = os.listdir(self.proc_root)
        except OSError:
            return out
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"{self.proc_root}/{entry}/stat") as f:
                    line = f.read()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            close = line.rfind(")")
            if close == -1:
                continue
            fields = line[close + 2:].split()
            if len(fields) < 2:
                continue
            try:
                ppid = int(fields[1])
            except ValueError:
                continue
            if ppid == pid:
                try:
                    out.append(int(entry))
                except ValueError:
                    pass
        return out

    def walk_tree(self, root_pid: int) -> list[int]:
        """BFS over the process tree rooted at ``root_pid``."""
        seen = {root_pid}
        frontier = [root_pid]
        while frontier:
            next_frontier: list[int] = []
            for pid in frontier:
                for child in self.read_children(pid):
                    if child not in seen:
                        seen.add(child)
                        next_frontier.append(child)
            frontier = next_frontier
        return list(seen)

    def tree_shape(self, root_pid: int) -> tuple[list[int], int, int]:
        """Return ``(pids, max_depth, max_breadth)`` for ``root_pid``."""
        seen = {root_pid}
        frontier = [(root_pid, 1)]
        max_depth = 0
        max_breadth = 0
        ordered: list[int] = []
        while frontier:
            max_breadth = max(max_breadth, len(frontier))
            next_frontier: list[tuple[int, int]] = []
            for pid, depth in frontier:
                ordered.append(pid)
                max_depth = max(max_depth, depth)
                for child in self.read_children(pid):
                    if child not in seen:
                        seen.add(child)
                        next_frontier.append((child, depth + 1))
            frontier = next_frontier
        return ordered, max_depth, max_breadth

    def read_cpu_seconds(self, pid: int) -> float:
        """utime + stime for a single PID, in seconds. 0 on any failure."""
        try:
            with open(f"{self.proc_root}/{pid}/stat") as f:
                line = f.read()
        except (FileNotFoundError, PermissionError, OSError):
            return 0.0
        close = line.rfind(")")
        if close == -1:
            return 0.0
        fields = line[close + 2:].split()
        try:
            utime = int(fields[11])
            stime = int(fields[12])
        except (IndexError, ValueError):
            return 0.0
        return (utime + stime) / _CLK_TCK

    def read_rss_bytes(self, pid: int) -> int:
        """VmRSS for a single PID, in bytes. 0 on any failure."""
        try:
            with open(f"{self.proc_root}/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                return int(parts[1]) * 1024
                            except ValueError:
                                return 0
                        return 0
        except (FileNotFoundError, PermissionError, OSError):
            pass
        return 0

    def read_comm(self, pid: int) -> str:
        """Process basename from ``/proc/<pid>/comm``."""
        try:
            with open(f"{self.proc_root}/{pid}/comm") as f:
                return f.read().strip() or str(pid)
        except (FileNotFoundError, PermissionError, OSError):
            return str(pid)

    def read_io_bytes(self, pid: int) -> dict[str, int]:
        """I/O byte counters from ``/proc/<pid>/io``. Missing fields are 0."""
        out = {
            "read_bytes": 0,
            "write_bytes": 0,
            "cancelled_write_bytes": 0,
        }
        try:
            with open(f"{self.proc_root}/{pid}/io") as f:
                for line in f:
                    key, sep, value = line.partition(":")
                    if sep and key in out:
                        try:
                            out[key] = int(value.strip())
                        except ValueError:
                            pass
        except (FileNotFoundError, PermissionError, OSError):
            pass
        return out

    def read_cgroup(self, pid: int) -> list[str]:
        try:
            with open(f"{self.proc_root}/{pid}/cgroup") as f:
                return [line.strip() for line in f if line.strip()]
        except (FileNotFoundError, PermissionError, OSError):
            return []

    def read_oom_score(self, pid: int) -> int:
        try:
            with open(f"{self.proc_root}/{pid}/oom_score") as f:
                return int(f.read().strip() or 0)
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            return 0

    def read_syscall(self, pid: int) -> str:
        try:
            with open(f"{self.proc_root}/{pid}/syscall") as f:
                first = f.read().split(maxsplit=1)[0]
        except (FileNotFoundError, PermissionError, OSError, IndexError):
            return ""
        return first if first and first != "-1" else ""

    def read_open_files(self, pid: int, limit: int = 100) -> list[str]:
        fd_dir = f"{self.proc_root}/{pid}/fd"
        out: list[str] = []
        try:
            entries = os.listdir(fd_dir)
        except (FileNotFoundError, PermissionError, OSError):
            return out
        for entry in entries:
            if len(out) >= limit:
                break
            try:
                target = os.readlink(os.path.join(fd_dir, entry))
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if target and not target.startswith(("socket:", "pipe:", "anon_inode:")):
                out.append(target)
        return out

    def read_network_connections(self, pid: int) -> list[dict]:
        conns: list[dict] = []
        for name, family in (("tcp", 4), ("tcp6", 6), ("udp", 4), ("udp6", 6)):
            path = f"{self.proc_root}/{pid}/net/{name}"
            try:
                with open(path) as f:
                    lines = f.readlines()[1:]
            except (FileNotFoundError, PermissionError, OSError):
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 3:
                    continue
                remote = parts[2]
                if remote.startswith("00000000:") or remote.startswith("00000000000000000000000000000000:"):
                    continue
                host_hex, port_hex = remote.rsplit(":", 1)
                try:
                    port = int(port_hex, 16)
                except ValueError:
                    continue
                conns.append({"remote": host_hex, "port": port, "family": family})
        return conns

    def read_gpu_usage(self, pids: list[int]) -> dict:
        if not pids:
            return {}
        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=1,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return {}
        if proc.returncode != 0:
            return {}
        wanted = set(pids)
        peak = 0
        matched = 0
        for line in proc.stdout.splitlines():
            bits = [b.strip() for b in line.split(",")]
            if len(bits) < 2:
                continue
            try:
                pid = int(bits[0])
                mem = int(bits[1])
            except ValueError:
                continue
            if pid in wanted:
                matched += 1
                peak = max(peak, mem)
        return {"process_count": matched, "used_memory_mb_peak": peak} if matched else {}

    def sample(self, root_pid: int) -> dict:
        """One-shot snapshot of the process tree rooted at ``root_pid``.

        Returns ``{"cpu_seconds", "peak_rss_bytes", "process_count"}``.
        ``peak_rss_bytes`` is the SUM across the tree at this instant
        (the caller maintains running max across samples).
        """
        if root_pid <= 0:
            return {"cpu_seconds": 0.0, "peak_rss_bytes": 0, "process_count": 0}
        pids = self.walk_tree(root_pid)
        total_cpu = 0.0
        total_rss = 0
        counted = 0
        for pid in pids:
            cpu = self.read_cpu_seconds(pid)
            total_cpu += cpu
            rss = self.read_rss_bytes(pid)
            if rss > 0:
                total_rss += rss
                counted += 1
        if counted == 0:
            counted = 1 if self.read_rss_bytes(root_pid) > 0 else len(pids)
        return {
            "cpu_seconds": total_cpu,
            "peak_rss_bytes": total_rss,
            "process_count": max(counted, len(pids)),
        }

    def sample_extended(
        self,
        root_pid: int,
        collect_io: bool = True,
        collect_network: bool = False,
        collect_open_files: bool = False,
        collect_gpu: bool = False,
        collect_cgroup: bool = False,
        collect_syscalls: bool = False,
        collect_oom: bool = False,
        files_limit: int = 100,
    ) -> dict:
        """One-shot snapshot with cheap Tier-A process-tree details."""
        if root_pid <= 0:
            return {
                "cpu_seconds": 0.0,
                "peak_rss_bytes": 0,
                "process_count": 0,
                "io": {
                    "read_bytes": 0,
                    "write_bytes": 0,
                    "cancelled_write_bytes": 0,
                },
                "per_pid_cpu": {},
                "per_pid_comm": {},
                "process_tree_depth": 0,
                "process_tree_breadth": 0,
                "network_connections": [],
                "open_files": [],
                "cgroups": [],
                "syscalls": {},
                "oom_score_max": 0,
                "gpu": {},
            }
        pids, depth, breadth = self.tree_shape(root_pid)
        total_cpu = 0.0
        total_rss = 0
        counted = 0
        io = {
            "read_bytes": 0,
            "write_bytes": 0,
            "cancelled_write_bytes": 0,
        }
        per_pid_cpu: dict[int, float] = {}
        per_pid_comm: dict[int, str] = {}
        network_seen = {}
        open_files: set[str] = set()
        cgroups: set[str] = set()
        syscalls: dict[str, int] = defaultdict(int)
        oom_score_max = 0
        for pid in pids:
            cpu = self.read_cpu_seconds(pid)
            total_cpu += cpu
            per_pid_cpu[pid] = cpu
            per_pid_comm[pid] = self.read_comm(pid)
            rss = self.read_rss_bytes(pid)
            if rss > 0:
                total_rss += rss
                counted += 1
            if collect_io:
                pid_io = self.read_io_bytes(pid)
                for key in io:
                    io[key] += pid_io.get(key, 0)
            if collect_network:
                for conn in self.read_network_connections(pid):
                    key = (conn.get("remote"), conn.get("port"), conn.get("family"))
                    network_seen[key] = conn
            if collect_open_files and len(open_files) < files_limit:
                for path in self.read_open_files(pid, files_limit):
                    open_files.add(path)
                    if len(open_files) >= files_limit:
                        break
            if collect_cgroup:
                cgroups.update(self.read_cgroup(pid))
            if collect_syscalls:
                syscall = self.read_syscall(pid)
                if syscall:
                    syscalls[syscall] += 1
            if collect_oom:
                oom_score_max = max(oom_score_max, self.read_oom_score(pid))
        if counted == 0:
            counted = 1 if self.read_rss_bytes(root_pid) > 0 else len(pids)
        return {
            "cpu_seconds": total_cpu,
            "peak_rss_bytes": total_rss,
            "process_count": max(counted, len(pids)),
            "io": io,
            "per_pid_cpu": per_pid_cpu,
            "per_pid_comm": per_pid_comm,
            "process_tree_depth": depth,
            "process_tree_breadth": breadth,
            "network_connections": list(network_seen.values()),
            "open_files": sorted(open_files)[:files_limit],
            "cgroups": sorted(cgroups),
            "syscalls": dict(syscalls),
            "oom_score_max": oom_score_max,
            "gpu": self.read_gpu_usage(pids) if collect_gpu else {},
        }


# Default sampler instance pointing at the real /proc.
_default_sampler = ProcTreeSampler()


# Module-level convenience wrappers (backward compat + used by _TabTracker).
def _read_children(pid: int) -> list[int]:
    return _default_sampler.read_children(pid)


def _walk_proc_tree(root_pid: int) -> list[int]:
    return _default_sampler.walk_tree(root_pid)


def _read_cpu_seconds(pid: int) -> float:
    return _default_sampler.read_cpu_seconds(pid)


def _read_rss_bytes(pid: int) -> int:
    return _default_sampler.read_rss_bytes(pid)


def sample_tree(root_pid: int) -> dict:
    return _default_sampler.sample(root_pid)


# ---------------------------------------------------------------------------
# Per-tab tracker
# ---------------------------------------------------------------------------

class _TabTracker:
    """Per-terminal in-flight telemetry. One instance per tab; reused
    across commands. Holds a QTimer only while a command is running."""

    def __init__(
        self,
        poll_interval_ms: int = 100,
        collect_io_bytes: bool = True,
        collect_binary_breakdown: bool = True,
        collect_network: bool = False,
        collect_open_files: bool = False,
        collect_gpu: bool = False,
        collect_cgroup: bool = False,
        collect_syscalls: bool = False,
        collect_oom: bool = False,
        files_limit: int = 100,
    ):
        self._poll_interval_ms = poll_interval_ms
        self._collect_io_bytes = collect_io_bytes
        self._collect_binary_breakdown = collect_binary_breakdown
        self._collect_network = collect_network
        self._collect_open_files = collect_open_files
        self._collect_gpu = collect_gpu
        self._collect_cgroup = collect_cgroup
        self._collect_syscalls = collect_syscalls
        self._collect_oom = collect_oom
        self._files_limit = files_limit
        self._timer: QTimer | None = None
        self._reset()
        # Most recent finalized telemetry — exposed via
        # ``CommandTelemetryService.get_last_telemetry``.
        self.last_telemetry: CommandTelemetry | None = None

    def _reset(self) -> None:
        self._started_at: float | None = None
        self._started_monotonic: float | None = None
        self._root_pid: int = 0
        self._cpu_last: float = 0.0
        self._peak_rss: int = 0
        self._peak_count: int = 0
        self._io_start = {
            "read_bytes": 0,
            "write_bytes": 0,
            "cancelled_write_bytes": 0,
        }
        self._io_last = dict(self._io_start)
        self._prev_pid_cpu: dict[int, float] = {}
        self._binary_cpu = defaultdict(float)
        self._tree_depth = 0
        self._tree_breadth = 0
        self._network_seen = {}
        self._open_files: set[str] = set()
        self._cgroups: set[str] = set()
        self._syscall_counts = defaultdict(int)
        self._oom_score_max = 0
        self._gpu = {}

    def on_start(self, terminal, started_at: float,
                 started_at_monotonic: float) -> None:
        """Called from the ;C subscription."""
        self.stop_timer()
        self._reset()
        self._started_at = started_at
        self._started_monotonic = started_at_monotonic
        try:
            self._root_pid = int(terminal.foreground_pid() or 0)
        except Exception:
            self._root_pid = 0
        # Take one immediate sample so even sub-poll-interval commands
        # produce non-zero counts.
        self._sample()
        self._io_start = dict(self._io_last)
        self._binary_cpu = defaultdict(float)
        if self._root_pid > 0:
            self._timer = QTimer()
            self._timer.setSingleShot(False)
            self._timer.timeout.connect(self._sample)
            self._timer.start(self._poll_interval_ms)

    def _sample(self) -> None:
        if self._root_pid <= 0:
            return
        snap = _default_sampler.sample_extended(
            self._root_pid,
            collect_io=self._collect_io_bytes,
            collect_network=self._collect_network,
            collect_open_files=self._collect_open_files,
            collect_gpu=self._collect_gpu,
            collect_cgroup=self._collect_cgroup,
            collect_syscalls=self._collect_syscalls,
            collect_oom=self._collect_oom,
            files_limit=self._files_limit,
        )
        self._cpu_last = max(self._cpu_last, snap["cpu_seconds"])
        if snap["peak_rss_bytes"] > self._peak_rss:
            self._peak_rss = snap["peak_rss_bytes"]
        if snap["process_count"] > self._peak_count:
            self._peak_count = snap["process_count"]
        self._tree_depth = max(self._tree_depth, snap.get("process_tree_depth", 0))
        self._tree_breadth = max(self._tree_breadth, snap.get("process_tree_breadth", 0))
        if self._collect_io_bytes:
            io = snap.get("io", {})
            for key in self._io_last:
                self._io_last[key] = max(self._io_last[key], int(io.get(key, 0)))
        if self._collect_binary_breakdown:
            per_pid_cpu = snap.get("per_pid_cpu", {})
            per_pid_comm = snap.get("per_pid_comm", {})
            for pid, cpu in per_pid_cpu.items():
                prev = self._prev_pid_cpu.get(pid)
                delta = cpu if prev is None else max(0.0, cpu - prev)
                if delta > 0:
                    self._binary_cpu[per_pid_comm.get(pid, str(pid))] += delta
            self._prev_pid_cpu = dict(per_pid_cpu)
        if self._collect_network:
            for conn in snap.get("network_connections", []):
                key = (conn.get("remote"), conn.get("port"), conn.get("family"))
                self._network_seen[key] = conn
        if self._collect_open_files:
            for path in snap.get("open_files", []):
                self._open_files.add(path)
        if self._collect_cgroup:
            self._cgroups.update(snap.get("cgroups", []))
        if self._collect_syscalls:
            for syscall, count in snap.get("syscalls", {}).items():
                self._syscall_counts[syscall] += count
        if self._collect_oom:
            self._oom_score_max = max(self._oom_score_max, snap.get("oom_score_max", 0))
        if self._collect_gpu:
            gpu = snap.get("gpu") or {}
            if gpu:
                self._gpu["process_count"] = max(
                    self._gpu.get("process_count", 0),
                    gpu.get("process_count", 0),
                )
                self._gpu["used_memory_mb_peak"] = max(
                    self._gpu.get("used_memory_mb_peak", 0),
                    gpu.get("used_memory_mb_peak", 0),
                )

    def stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except RuntimeError:
                pass
            self._timer = None

    def on_finish(self) -> CommandTelemetry | None:
        """Called from the ;D subscription. Returns the finalized
        telemetry (also cached on ``last_telemetry``) or None if we
        never saw a matching ;C."""
        if self._started_at is None:
            self.stop_timer()
            return None
        # Final sample.
        self._sample()
        self.stop_timer()

        finished_at = time.time()
        finished_monotonic = time.monotonic()
        if self._started_monotonic is not None:
            duration = finished_monotonic - self._started_monotonic
        else:
            duration = max(0.0, finished_at - self._started_at)

        tele = CommandTelemetry(
            duration=duration,
            cpu_seconds=self._cpu_last,
            peak_rss_bytes=self._peak_rss,
            process_count=self._peak_count,
            started_at=self._started_at,
            finished_at=finished_at,
            read_bytes=max(0, self._io_last["read_bytes"] - self._io_start["read_bytes"]),
            write_bytes=max(0, self._io_last["write_bytes"] - self._io_start["write_bytes"]),
            cancelled_write_bytes=max(
                0,
                self._io_last["cancelled_write_bytes"]
                - self._io_start["cancelled_write_bytes"],
            ),
            binary_cpu_seconds=dict(self._binary_cpu),
            process_tree_depth=self._tree_depth,
            process_tree_breadth=self._tree_breadth,
            network_connections=list(self._network_seen.values()),
            open_files=sorted(self._open_files)[:self._files_limit],
            cgroups=sorted(self._cgroups),
            syscall_counts=dict(self._syscall_counts),
            oom_score_max=self._oom_score_max,
            gpu=dict(self._gpu),
        )
        self.last_telemetry = tele
        self._reset()
        return tele


# ---------------------------------------------------------------------------
# JSONL log writer
# ---------------------------------------------------------------------------

class _TelemetryLogger:
    """Append-only JSONL writer with 0o600 perms (the log captures
    command lines, which can include credentials — see module note)."""

    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def append(self, record: dict) -> None:
        line = json.dumps(record, default=str) + "\n"
        try:
            fd = os.open(self.path,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                         0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as e:
            log.warning("command_telemetry: write failed: %s", e)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

#: Regex matching the trailing ``  [12.3s · 412MB]`` suffix we add to
#: tab titles, so repeated commands don't compound the suffix.
_TAB_SUFFIX_RE = re.compile(r"\s*\[[^\]]*?s(?:\s*·[^\]]*)?\]\s*$")

#: ANSI SGR dim grey used by the inline display mode.
_INLINE_DIM = "\x1b[2;37m"
_INLINE_RESET = "\x1b[0m"


class CommandTelemetryService:
    """Per-window service. Subscribes to shell_integration's ;C and
    ;D events, samples /proc on a per-command timer, and annotates
    each completed CommandRecord."""

    def __init__(self, window, poll_interval_ms: int = 100,
                 display: str = "tab_status",
                 log_path: str | None = None,
                 tab_status_ms: int = 30_000,
                 collect_io_bytes: bool = True,
                 collect_binary_breakdown: bool = True,
                 collect_network: bool = False,
                 collect_open_files: bool = False,
                 collect_gpu: bool = False,
                 collect_cgroup: bool = False,
                 collect_syscalls: bool = False,
                 collect_oom: bool = False,
                 files_limit: int = 100):
        self._window = window
        self._poll_interval_ms = poll_interval_ms
        self._collect_io_bytes = collect_io_bytes
        self._collect_binary_breakdown = collect_binary_breakdown
        self._collect_network = collect_network
        self._collect_open_files = collect_open_files
        self._collect_gpu = collect_gpu
        self._collect_cgroup = collect_cgroup
        self._collect_syscalls = collect_syscalls
        self._collect_oom = collect_oom
        self._files_limit = files_limit
        if display not in VALID_DISPLAY_MODES:
            log.warning(
                "command_telemetry: invalid display mode %r, falling back "
                "to 'tab_status'", display,
            )
            display = "tab_status"
        self._display = display
        self._tab_status_ms = tab_status_ms
        self._trackers: dict[int, _TabTracker] = {}
        self._logger: _TelemetryLogger | None = None
        if log_path:
            try:
                self._logger = _TelemetryLogger(log_path)
            except OSError as e:
                log.warning("command_telemetry: cannot open log %r: %s",
                            log_path, e)
        # Subscriptions registered on shell_integration; stashed so
        # ``deactivate`` can unwind them.
        self._sub_started = None
        self._sub_finished = None
        # Pending fade-tab-title QTimer instances. We hold strong
        # references so ``detach`` can stop them — otherwise the
        # ``QTimer.singleShot`` callback fires up to 30 s after the
        # window was destroyed and crashes on a deleted QTabWidget.
        self._fade_timers: list[QTimer] = []

    @property
    def display(self) -> str:
        """Current display mode."""
        return self._display

    # -- subscription wiring --

    def attach(self, shell_int) -> None:
        if self._sub_started is None:
            self._sub_started = self._on_started
            shell_int.subscribe_command_started(self._sub_started)
        if self._sub_finished is None:
            self._sub_finished = self._on_finished
            shell_int.subscribe_command_finished(self._sub_finished)

    def detach(self, shell_int) -> None:
        if self._sub_started is not None:
            try:
                shell_int.unsubscribe_command_started(self._sub_started)
            except Exception:
                pass
            self._sub_started = None
        if self._sub_finished is not None:
            try:
                shell_int.unsubscribe_command_finished(self._sub_finished)
            except Exception:
                pass
            self._sub_finished = None
        for tracker in self._trackers.values():
            tracker.stop_timer()
        self._trackers.clear()
        # Cancel any pending tab-title fade timers so they don't fire
        # after the window is gone.
        for t in self._fade_timers:
            try:
                t.stop()
            except RuntimeError:
                pass
        self._fade_timers.clear()

    # -- shell_integration callbacks --

    def _tracker_for(self, terminal) -> _TabTracker:
        tid = id(terminal)
        tracker = self._trackers.get(tid)
        if tracker is None:
            tracker = _TabTracker(
                self._poll_interval_ms,
                collect_io_bytes=self._collect_io_bytes,
                collect_binary_breakdown=self._collect_binary_breakdown,
                collect_network=self._collect_network,
                collect_open_files=self._collect_open_files,
                collect_gpu=self._collect_gpu,
                collect_cgroup=self._collect_cgroup,
                collect_syscalls=self._collect_syscalls,
                collect_oom=self._collect_oom,
                files_limit=self._files_limit,
            )
            self._trackers[tid] = tracker
        return tracker

    def _on_started(self, terminal, ev) -> None:
        tracker = self._tracker_for(terminal)
        tracker.on_start(
            terminal,
            started_at=ev.started_at,
            started_at_monotonic=ev.started_at_monotonic,
        )

    def _on_finished(self, terminal, rec) -> None:
        tracker = self._tracker_for(terminal)
        tele = tracker.on_finish()
        if tele is None:
            return
        # Annotate the CommandRecord so agent_control / MCP surfaces
        # pick it up via shell_integration.serialize_*.
        rec.telemetry = tele.to_dict()
        # Display.
        if self._display == "tab_status":
            self._update_tab_status(terminal, tele)
        elif self._display == "inline":
            self._inject_inline(terminal, tele)
        # Log.
        if self._logger is not None:
            self._logger.append({
                "tab_id": id(terminal),
                "started_at": tele.started_at,
                "finished_at": tele.finished_at,
                "duration": round(tele.duration, 3),
                "exit_status": rec.exit_status,
                "command": rec.text,
                "cwd": rec.cwd,
                **tele.to_dict(),
            })
        # Push event to attached agents. Prefer the agent_event_channel
        # ``publish`` API when it's loaded so the user's per-event
        # filter (``set_enabled_events``) still applies. Fall back to
        # the raw ``agent_control.broadcast_event`` only when no event
        # channel is configured — that path skips filtering, which is
        # acceptable as a last resort but not the default.
        payload = {
            "command": rec.text,
            "exit_status": rec.exit_status,
            "cwd": rec.cwd,
            "telemetry": tele.to_dict(),
        }
        events_svc = getattr(self._window, "agent_events", None)
        if events_svc is not None:
            try:
                events_svc.publish(id(terminal), "command_finished", payload)
                return
            except Exception:
                pass
        ac = getattr(self._window, "agent_control", None)
        if ac is not None:
            try:
                ac.broadcast_event(id(terminal), "command_finished", payload)
            except Exception:
                pass

    # -- display helpers --

    def _update_tab_status(self, terminal, tele: CommandTelemetry) -> None:
        if self._window is None:
            return
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return
        for i in range(tabs.count()):
            split = tabs.widget(i)
            if split is None or not hasattr(split, "find_terminals"):
                continue
            if terminal not in split.find_terminals():
                continue
            title = tabs.tabText(i)
            # Strip a prior ``  [Xs · YMB]`` suffix before appending
            # the new one — otherwise repeated commands compound:
            # ``foo  [1s]  [2s]  [3s]``.
            base = _TAB_SUFFIX_RE.sub("", title)
            short = tele.format_short()
            try:
                tabs.setTabText(i, f"{base}  [{short}]")
                tabs.setTabToolTip(i, short)
            except Exception:
                pass
            # Fade after a configurable interval. Use a stateful
            # QTimer (not the QTimer.singleShot static call) so we
            # keep a handle and can cancel from ``detach``. Without
            # that the closure fires up to ``_tab_status_ms`` later
            # — long after the window may have been destroyed in a
            # test teardown — and crashes the event loop on a deleted
            # C++ QTabWidget.
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda t=terminal, tm=timer: self._fade_fire(t, tm)
            )
            self._fade_timers.append(timer)
            timer.start(self._tab_status_ms)
            return

    def _inject_inline(self, terminal, tele: CommandTelemetry) -> None:
        """Inject a dim grey telemetry line into the terminal output.

        QTermWidget has no public API to write to the display layer
        without going through the PTY master (which the shell would
        interpret as input and try to execute). Until a display-only
        write API is available, fall back to tab_status display.
        """
        self._update_tab_status(terminal, tele)

    def _fade_fire(self, terminal, timer: QTimer) -> None:
        """Bridge slot for fade timers: clear the title and drop the
        timer from the pending list so ``detach`` has nothing stale
        to stop."""
        try:
            self._fade_timers.remove(timer)
        except ValueError:
            pass
        self._clear_tab_status(terminal)

    def _clear_tab_status(self, terminal) -> None:
        """Strip the trailing ``  [Xs]`` suffix. Safe against a
        destroyed C++ QTabWidget — fade timers can fire long after
        the window is gone (tests, window-close-mid-fade)."""
        if self._window is None:
            return
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return
        try:
            count = tabs.count()
        except RuntimeError:
            self._window = None
            return
        for i in range(count):
            try:
                split = tabs.widget(i)
            except RuntimeError:
                return
            if split is None or not hasattr(split, "find_terminals"):
                continue
            if terminal not in split.find_terminals():
                continue
            try:
                title = tabs.tabText(i)
                tabs.setTabText(i, _TAB_SUFFIX_RE.sub("", title))
            except (RuntimeError, Exception):
                pass
            return

    # -- read API --

    def get_last_telemetry(self, terminal) -> dict | None:
        """Return the last command's telemetry dict for ``terminal``,
        or ``None`` if no command has completed yet."""
        tracker = self._trackers.get(id(terminal))
        if tracker is None or tracker.last_telemetry is None:
            return None
        return tracker.last_telemetry.to_dict()

    def get_telemetry_history(self, terminal, limit: int = 10) -> list[dict]:
        """Return the last ``limit`` annotated command records for a tab.

        Delegates to shell_integration's history; only records that carry
        a ``telemetry`` dict are included (i.e. commands that completed
        while this plugin was active).
        """
        shell_int = getattr(self._window, "shell_integration", None)
        if shell_int is None:
            return []
        hist = shell_int.get_history(terminal)
        if hist is None:
            return []
        out = []
        for rec in reversed(hist.history):
            if rec.telemetry is not None:
                entry = {
                    "text": rec.text,
                    "exit_status": rec.exit_status,
                    "started_at": rec.started_at,
                    "finished_at": rec.finished_at,
                    "cwd": rec.cwd,
                    "telemetry": rec.telemetry,
                }
                out.append(entry)
                if len(out) >= limit:
                    break
        out.reverse()
        return out


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

class CommandTelemetryPlugin(Plugin):
    name = "command_telemetry"
    description = (
        "Track duration, CPU time, and peak memory for each command "
        "run in a terminal tab; annotate CommandRecord.telemetry."
    )
    version = "0.1"
    capabilities = ["command_telemetry"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: CommandTelemetryService | None = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = bool(cfg.get(
            "plugins", "command_telemetry", "enabled", default=False,
        ))
        if not enabled:
            return

        shell_int = getattr(app_controller, "shell_integration", None)
        if shell_int is None:
            # Documented dependency — don't silently degrade.
            raise RuntimeError(
                "command_telemetry requires the shell_integration plugin"
            )

        poll_ms = int(cfg.get(
            "plugins", "command_telemetry", "poll_interval_ms", default=100,
        ))
        display = cfg.get(
            "plugins", "command_telemetry", "display", default="tab_status",
        )
        log_path = cfg.get(
            "plugins", "command_telemetry", "log_path", default="",
        )
        tab_status_ms = int(cfg.get(
            "plugins", "command_telemetry", "tab_status_ms", default=30_000,
        ))
        collect_io_bytes = bool(cfg.get(
            "plugins", "command_telemetry", "collect_io_bytes", default=True,
        ))
        collect_binary_breakdown = bool(cfg.get(
            "plugins", "command_telemetry", "collect_binary_breakdown", default=True,
        ))
        collect_network = bool(cfg.get(
            "plugins", "command_telemetry", "collect_network", default=False,
        ))
        collect_open_files = bool(cfg.get(
            "plugins", "command_telemetry", "collect_open_files", default=False,
        ))
        collect_gpu = bool(cfg.get(
            "plugins", "command_telemetry", "collect_gpu", default=False,
        ))
        collect_cgroup = bool(cfg.get(
            "plugins", "command_telemetry", "collect_cgroup", default=False,
        ))
        collect_syscalls = bool(cfg.get(
            "plugins", "command_telemetry", "collect_syscalls", default=False,
        ))
        collect_oom = bool(cfg.get(
            "plugins", "command_telemetry", "collect_oom", default=False,
        ))
        files_limit = int(cfg.get(
            "plugins", "command_telemetry", "files_limit", default=100,
        ))

        self._window = app_controller
        self._service = CommandTelemetryService(
            app_controller,
            poll_interval_ms=poll_ms,
            display=display,
            log_path=log_path or None,
            tab_status_ms=tab_status_ms,
            collect_io_bytes=collect_io_bytes,
            collect_binary_breakdown=collect_binary_breakdown,
            collect_network=collect_network,
            collect_open_files=collect_open_files,
            collect_gpu=collect_gpu,
            collect_cgroup=collect_cgroup,
            collect_syscalls=collect_syscalls,
            collect_oom=collect_oom,
            files_limit=files_limit,
        )
        if not hasattr(app_controller, "command_telemetry"):
            app_controller.command_telemetry = self._service
        self._service.attach(shell_int)

    def deactivate(self):
        shell_int = (
            getattr(self._window, "shell_integration", None)
            if self._window else None
        )
        if self._service is not None and shell_int is not None:
            self._service.detach(shell_int)
        if (self._window is not None
                and getattr(self._window, "command_telemetry", None) is self._service):
            try:
                del self._window.command_telemetry
            except AttributeError:
                pass
        self._service = None
        self._window = None
