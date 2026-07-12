"""Trusted-FD session launcher contract for the multi-machine broker."""
from __future__ import annotations

import errno
import json
import os

import pytest

import multimachine.mm_session_launcher as launcher
from multimachine.mm_session_launcher import (
    combine_session_snapshots,
    exec_broker,
    sealed_config_fd,
)


def _pairing() -> dict:
    return {"origins": [{
        "machine_id": "vm-a",
        "trust_domain_id": "owner-machines",
        "generation": 51,
        "capabilities": ["attach_ui", "receive_input"],
    }]}


def _session() -> dict:
    return {
        "control_host": "127.0.0.1",
        "streams": [{
            "label": "a",
            "spec": {
                "origin": "vm-a", "stream_id": "source-a",
                "generation": 51, "app_id": "qdistro.mm.vm-a.a",
                "instance_id": "vm-a-a-1", "rdp_host": "10.0.2.2",
                "rdp_port": 5555, "width": 640, "height": 400,
                "allow_input": 1,
            },
            "control_port": 5571, "rdp_unit": "mm-rdp-a",
            "marker_unit": "mm-marker-a", "otp": "single-use-otp",
            "control_capability": "source-control-secret",
        }],
    }


def test_combines_separate_authority_and_secret_snapshots() -> None:
    pairing = _pairing()
    session = _session()
    combined = combine_session_snapshots(pairing, session)
    assert combined == {**session, "origins": pairing["origins"]}
    assert combined["origins"] is not pairing["origins"]
    assert combined["streams"] is not session["streams"]


@pytest.mark.parametrize(("pairing", "session", "message"), [
    ({}, _session(), "exactly 'origins'"),
    ({**_pairing(), "trusted": True}, _session(), "exactly 'origins'"),
    (_pairing(), {**_session(), "origins": []}, "unknown stream session"),
    (_pairing(), {"control_host": "127.0.0.1"}, "must contain 'streams'"),
])
def test_inputs_cannot_override_or_smuggle_authority(pairing, session, message) -> None:
    with pytest.raises(ValueError, match=message):
        combine_session_snapshots(pairing, session)


def test_combined_snapshot_still_passes_broker_authority() -> None:
    session = _session()
    session["streams"][0]["spec"]["generation"] = 52
    with pytest.raises(ValueError, match="generation"):
        combine_session_snapshots(_pairing(), session)


def test_config_memfd_is_immutable_and_inheritable() -> None:
    config = combine_session_snapshots(_pairing(), _session())
    fd = sealed_config_fd(config)
    try:
        assert os.get_inheritable(fd) is True
        assert json.loads(os.read(fd, 65536)) == config
        with pytest.raises(OSError) as caught:
            os.write(fd, b"tamper")
        assert caught.value.errno == errno.EPERM
    finally:
        os.close(fd)


def test_exec_argv_contains_only_program_flag_and_fd(monkeypatch) -> None:
    config = combine_session_snapshots(_pairing(), _session())
    observed = {}

    def fake_execvp(program, argv):
        observed["program"] = program
        observed["argv"] = argv
        fd = int(argv[-1])
        observed["config"] = json.loads(os.read(fd, 65536))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(os, "execvp", fake_execvp)
    with pytest.raises(RuntimeError, match="intercepted"):
        exec_broker(config, "/trusted/qdistro-mm-broker")
    assert observed["program"] == "/trusted/qdistro-mm-broker"
    assert observed["argv"][:2] == [
        "/trusted/qdistro-mm-broker", "--config-fd"]
    assert len(observed["argv"]) == 3
    assert "single-use-otp" not in " ".join(observed["argv"])
    assert "source-control-secret" not in " ".join(observed["argv"])
    assert observed["config"] == config


def _json_pipe(value: object) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps(value).encode("utf-8"))
    os.close(write_fd)
    return read_fd


def test_main_reads_owned_fds_and_hands_off_validated_snapshot(monkeypatch) -> None:
    pairing_fd = _json_pipe(_pairing())
    streams_fd = _json_pipe(_session())
    observed = {}

    def fake_exec(config, program):
        observed["config"] = config
        observed["program"] = program

    monkeypatch.setattr(launcher, "exec_broker", fake_exec)
    result = launcher.main([
        "--pairing-fd", str(pairing_fd),
        "--streams-fd", str(streams_fd),
    ])
    assert result == 1  # a real exec never returns
    assert observed["config"] == combine_session_snapshots(_pairing(), _session())
    assert observed["program"] == "/usr/local/bin/qdistro-mm-broker"
    for fd in (pairing_fd, streams_fd):
        with pytest.raises(OSError):
            os.fstat(fd)
