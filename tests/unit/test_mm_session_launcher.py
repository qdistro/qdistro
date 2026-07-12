"""Trusted-FD session launcher contract for the multi-machine broker."""
from __future__ import annotations

import errno
import json
import os
import stat
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import multimachine.mm_session_launcher as launcher
from multimachine.mm_pairing_authority import (
    issue_pairing_receipt,
    public_key_bytes,
)
from multimachine.mm_session_launcher import (
    combine_session_snapshots,
    combine_verified_session,
    exec_broker,
    sealed_config_fd,
)


NOW = 1_800_000_000
SESSION_ID = "viewer-session-7f5c"


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


def _signed_inputs(now: int = NOW) -> tuple[dict, bytes, dict]:
    key = Ed25519PrivateKey.generate()
    receipt = issue_pairing_receipt(
        origins=_pairing()["origins"], viewer_machine_id="vm-viewer",
        session_id=SESSION_ID, issued_at=now, expires_at=now + 120,
        private_key=key,
    )
    session = _session()
    session["pairing_session_id"] = SESSION_ID
    return receipt, public_key_bytes(key.public_key()), session


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


def test_verified_receipt_combines_with_bound_stream_session() -> None:
    receipt, public_key, session = _signed_inputs()
    combined = combine_verified_session(
        receipt, session, public_key, "vm-viewer", now=lambda: NOW)
    assert combined == {**_session(), "origins": _pairing()["origins"]}


def test_verified_receipt_rejects_cross_session_replay() -> None:
    receipt, public_key, session = _signed_inputs()
    session["pairing_session_id"] = "different-session"
    with pytest.raises(ValueError, match="different session"):
        combine_verified_session(
            receipt, session, public_key, "vm-viewer", now=lambda: NOW)


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


def _text_pipe(value: str) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, value.encode("ascii"))
    os.close(write_fd)
    return read_fd


def test_main_reads_owned_fds_and_hands_off_validated_snapshot(monkeypatch) -> None:
    receipt, public_key, session = _signed_inputs(int(time.time()))
    pairing_fd = _json_pipe(receipt)
    streams_fd = _json_pipe(session)
    shell_pid_fd = _text_pipe("4242\n")
    observed = {}

    def fake_exec(config, program):
        observed["config"] = config
        observed["program"] = program

    monkeypatch.setattr(launcher, "exec_broker", fake_exec)
    monkeypatch.setattr(
        launcher, "_read_pinned_authority_key", lambda: public_key)
    result = launcher.main([
        "--pairing-fd", str(pairing_fd),
        "--streams-fd", str(streams_fd),
        "--shell-pid-fd", str(shell_pid_fd),
        "--viewer-machine-id", "vm-viewer",
    ])
    assert result == 1  # a real exec never returns
    expected = combine_session_snapshots(_pairing(), _session())
    expected["shell_pid"] = 4242
    assert observed["config"] == expected
    assert observed["program"] == "/usr/local/bin/qdistro-mm-broker"
    for fd in (pairing_fd, streams_fd, shell_pid_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_pinned_key_requires_root_owned_nonwritable_regular_file(
        tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "authority.pub"
    key_file.write_bytes(b"k" * 32)
    real_fstat = os.fstat

    def safe_fstat(fd):
        observed = real_fstat(fd)
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644,
            st_uid=0,
            st_size=observed.st_size,
        )

    monkeypatch.setattr(os, "fstat", safe_fstat)
    assert launcher._read_pinned_authority_key(str(key_file)) == b"k" * 32

    def writable_fstat(fd):
        observed = real_fstat(fd)
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o664,
            st_uid=0,
            st_size=observed.st_size,
        )

    monkeypatch.setattr(os, "fstat", writable_fstat)
    with pytest.raises(ValueError, match="not group/other writable"):
        launcher._read_pinned_authority_key(str(key_file))
