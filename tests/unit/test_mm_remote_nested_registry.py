"""R7 multi-stream registry tests: isolation, fd custody and rollback."""
from __future__ import annotations

import os
import subprocess

import pytest

from multimachine.remote_nested_registry import (
    RemoteNestedRegistry,
    RemoteNestedStreamSpec,
)


class FakeProcess:
    next_pid = 7000

    def __init__(self, argv, *, pass_fds, close_fds):
        self.argv = argv
        self.pass_fds = pass_fds
        self.close_fds = close_fds
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode


def spec(origin: str, stream: str, port: int, *, source: bool = False,
         generation: int = 7):
    count = 7 if source else 5
    fds = [os.open("/dev/null", os.O_RDONLY) for _ in range(count)]
    return RemoteNestedStreamSpec(
        origin_machine_id=origin, stream_id=stream, generation=generation,
        host="127.0.0.1", port=port,
        grant_fd=fds[0], secret_fd=fds[1], tls_cert_fd=fds[2],
        tls_key_fd=fds[3], peer_cert_fd=fds[4],
        source_config_fd=fds[5] if source else None,
        source_close_fd=fds[6] if source else None)


def assert_closed(stream: RemoteNestedStreamSpec) -> None:
    for fd in stream.owned_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_two_viewer_streams_get_isolated_supervisors_and_parent_drops_fds():
    processes = []

    def factory(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        processes.append(process)
        return process

    a = spec("source-a", "stream-a", 14401)
    b = spec("source-a", "stream-b", 14402)
    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="viewer-b", session_id="dock-7",
        process_factory=factory)
    registry.add_many((a, b))

    assert registry.keys == (("source-a", "stream-a"),
                             ("source-a", "stream-b"))
    assert len(processes) == 2
    assert all(process.close_fds for process in processes)
    assert processes[0].pass_fds == a.owned_fds
    assert processes[1].pass_fds == b.owned_fds
    assert processes[0].argv[processes[0].argv.index("--stream-id") + 1] == "stream-a"
    assert processes[1].argv[processes[1].argv.index("--stream-id") + 1] == "stream-b"
    assert_closed(a)
    assert_closed(b)


def test_one_stream_failure_is_reported_without_stopping_sibling():
    processes = []

    def factory(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        processes.append(process)
        return process

    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="viewer-b", session_id="dock-7",
        process_factory=factory)
    registry.add_many((spec("source-a", "a", 14401),
                       spec("source-a", "b", 14402)))
    processes[0].returncode = 7

    exits = registry.poll()

    assert len(exits) == 1
    assert exits[0].stream_id == "a" and exits[0].failed
    assert registry.keys == (("source-a", "b"),)
    assert processes[1].poll() is None
    assert not processes[1].terminated


def test_remove_stops_only_exact_stream():
    processes = []

    def factory(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        processes.append(process)
        return process

    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="viewer-b", session_id="dock-7",
        process_factory=factory)
    registry.add_many((spec("source-a", "a", 14401),
                       spec("source-a", "b", 14402)))

    assert registry.remove("source-a", "a")
    assert not registry.remove("source-a", "missing")
    assert processes[0].terminated
    assert not processes[1].terminated
    assert registry.keys == (("source-a", "b"),)


def test_detach_is_atomic_and_remount_requires_fresh_generation():
    processes = []

    def factory(*args, **kwargs):
        process = FakeProcess(*args, **kwargs)
        processes.append(process)
        return process

    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="viewer-b", session_id="dock-7",
        process_factory=factory)
    registry.add_many((spec("source-a", "a", 14401, generation=7),
                       spec("source-a", "b", 14402, generation=7)))

    detached = registry.detach_all()

    assert detached == (("source-a", "a"), ("source-a", "b"))
    assert registry.keys == ()
    assert all(process.terminated for process in processes)

    stale = spec("source-a", "a", 14401, generation=7)
    with pytest.raises(ValueError, match="generation must increase"):
        registry.add(stale)
    assert_closed(stale)
    assert len(processes) == 2

    registry.add_many((spec("source-a", "a", 14401, generation=8),
                       spec("source-a", "b", 14402, generation=8)))
    assert registry.keys == (("source-a", "a"), ("source-a", "b"))
    assert len(processes) == 4
    assert not processes[2].terminated and not processes[3].terminated


def test_duplicate_batch_rejects_before_spawn_and_closes_every_fd():
    spawned = []
    a = spec("source-a", "same", 14401)
    b = spec("source-a", "same", 14402)
    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="viewer-b", session_id="dock-7",
        process_factory=lambda *args, **kwargs: spawned.append((args, kwargs)))

    with pytest.raises(ValueError, match="duplicate"):
        registry.add_many((a, b))

    assert spawned == []
    assert registry.keys == ()
    assert_closed(a)
    assert_closed(b)


def test_shared_fd_rejects_entire_batch():
    a = spec("source-a", "a", 14401)
    b = spec("source-a", "b", 14402)
    os.close(b.grant_fd)
    object.__setattr__(b, "grant_fd", a.grant_fd)
    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="viewer-b", session_id="dock-7",
        process_factory=FakeProcess)

    with pytest.raises(ValueError, match="share"):
        registry.add_many((a, b))

    assert registry.keys == ()
    assert_closed(a)
    for fd in b.owned_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_partial_spawn_failure_rolls_back_started_stream_and_all_fds():
    processes = []

    def factory(*args, **kwargs):
        if processes:
            raise OSError("spawn failed")
        process = FakeProcess(*args, **kwargs)
        processes.append(process)
        return process

    a = spec("source-a", "a", 14401)
    b = spec("source-a", "b", 14402)
    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="viewer-b", session_id="dock-7",
        process_factory=factory)

    with pytest.raises(OSError, match="spawn failed"):
        registry.add_many((a, b))

    assert registry.keys == ()
    assert processes[0].terminated
    assert_closed(a)
    assert_closed(b)


def test_source_specs_require_two_additional_distinct_fds():
    viewer_shape = spec("source-a", "a", 14401)
    registry = RemoteNestedRegistry(
        role="source", local_machine_id="source-a", session_id="dock-7",
        process_factory=FakeProcess)
    with pytest.raises(ValueError, match="requires local"):
        registry.add(viewer_shape)
    assert_closed(viewer_shape)

    source_shape = spec("source-a", "b", 14402, source=True)
    viewer = RemoteNestedRegistry(
        role="viewer", local_machine_id="viewer-b", session_id="dock-7",
        process_factory=FakeProcess)
    with pytest.raises(ValueError, match="cannot receive source"):
        viewer.add(source_shape)
    assert_closed(source_shape)
