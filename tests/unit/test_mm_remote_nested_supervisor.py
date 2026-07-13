"""Process-tree and descriptor tests for the R6 nested session supervisor."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from multimachine.remote_nested_supervisor import (
    RemoteNestedSupervisor,
    SupervisorPrograms,
)


REPO = Path(__file__).resolve().parents[2]
CHILD = REPO / "tests/fixtures/mm_remote_supervisor_child.py"


def _read_fd() -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"test")
    return read_fd, write_fd


def _run_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                helper_exit: int) -> tuple[int, list[int]]:
    pairs = [_read_fd() for _ in range(7)]
    read_fds = [pair[0] for pair in pairs]
    write_fds = [pair[1] for pair in pairs]
    log = tmp_path / "children.log"
    monkeypatch.setenv("MM_SUPERVISOR_TEST_LOG", str(log))
    monkeypatch.setenv("MM_SUPERVISOR_HELPER_EXIT", str(helper_exit))
    program = str(CHILD)
    supervisor = RemoteNestedSupervisor(
        role="source",
        grant_fd=read_fds[0], secret_fd=read_fds[1],
        tls_cert_fd=read_fds[2], tls_key_fd=read_fds[3],
        peer_cert_fd=read_fds[4], source_config_fd=read_fds[5],
        source_close_fd=read_fds[6], local_machine_id="source-a",
        session_id="session-1", stream_id="stream-1",
        host="127.0.0.1", port=4443,
        programs=SupervisorPrograms(
            session_launcher=program, endpoint=program,
            controller=program, source_helper=program,
            viewer_helper=program))
    try:
        result = supervisor.run()
        lines = log.read_text(encoding="ascii").splitlines()
    finally:
        for fd in write_fds:
            os.close(fd)
    for fd in read_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    return result, [int(line.split()[2]) for line in lines]


def test_source_supervisor_builds_exact_fd_graph_and_stops_siblings(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, inherited_counts = _run_source(tmp_path, monkeypatch, 0)
    assert result == 0
    assert sorted(inherited_counts) == [2, 3, 6]


def test_supervisor_propagates_first_child_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, inherited_counts = _run_source(tmp_path, monkeypatch, 7)
    assert result == 7
    assert sorted(inherited_counts) == [2, 3, 6]


def test_viewer_rejects_source_only_fds() -> None:
    fds = [os.open("/dev/null", os.O_RDONLY) for _ in range(7)]
    try:
        with pytest.raises(ValueError, match="viewer"):
            RemoteNestedSupervisor(
                role="viewer", grant_fd=fds[0], secret_fd=fds[1],
                tls_cert_fd=fds[2], tls_key_fd=fds[3], peer_cert_fd=fds[4],
                source_config_fd=fds[5], source_close_fd=fds[6],
                local_machine_id="viewer-b", session_id="session-1",
                stream_id="stream-1", host="127.0.0.1", port=4443)
    finally:
        for fd in fds:
            os.close(fd)
