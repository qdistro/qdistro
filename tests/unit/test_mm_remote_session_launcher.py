"""Sealed inherited-FD handoff for one R6 adapter endpoint."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import multimachine.mm_remote_session_launcher as launcher
from multimachine.mm_pairing_authority import public_key_bytes
from multimachine.mm_remote_session_authority import issue_remote_session_grant
from multimachine.mm_remote_session_launcher import (
    build_verified_endpoint_config,
    exec_endpoint,
    parse_endpoint_config,
)
from multimachine.mm_session_launcher import sealed_config_fd


NOW = 1_800_000_000
SESSION = "viewer-session-7f5c"
STREAM = "stream_0123456789abcdef"
SECRET = bytes(range(32))
SOURCE_PIN = hashlib.sha256(b"source cert").hexdigest()
VIEWER_PIN = hashlib.sha256(b"viewer cert").hexdigest()


def _inputs(now=NOW) -> tuple[dict, bytes]:
    key = Ed25519PrivateKey.generate()
    receipt = issue_remote_session_grant(
        source_machine="vm-source", viewer_machine="vm-viewer",
        trust_domain_id="owner-machines", generation=51,
        session_id=SESSION, stream_id=STREAM, key_id="adapter-key-7f5c",
        session_secret=SECRET, issued_at=now, handoff_expires_at=now + 120,
        key_expires_at=now + 8 * 60 * 60, allow_input=True,
        source_tls_cert_sha256=SOURCE_PIN,
        viewer_tls_cert_sha256=VIEWER_PIN, private_key=key)
    return receipt, public_key_bytes(key.public_key())


def _config(*, role="viewer", machine="vm-viewer", secret=SECRET,
            now=NOW) -> dict:
    receipt, public_key = _inputs()
    return build_verified_endpoint_config(
        receipt, secret, public_key, role=role, local_machine_id=machine,
        session_id=SESSION, stream_id=STREAM, now=lambda: now)


@pytest.mark.parametrize(("role", "machine"), [
    ("source", "vm-source"),
    ("viewer", "vm-viewer"),
])
def test_builds_exact_parseable_endpoint_snapshot(role, machine) -> None:
    config = _config(role=role, machine=machine)
    parsed_role, allow_input, binding, key = parse_endpoint_config(
        config, now=lambda: NOW)
    assert parsed_role == role
    assert allow_input is True
    assert binding.source_machine == "vm-source"
    assert binding.viewer_machine == "vm-viewer"
    assert binding.epoch == 1
    assert binding.source_tls_cert_sha256 == SOURCE_PIN
    assert binding.viewer_tls_cert_sha256 == VIEWER_PIN
    assert key.key_id == "adapter-key-7f5c"
    assert key.require_active(now=NOW) == SECRET


def test_secret_fd_is_cryptographically_bound_to_signed_grant() -> None:
    with pytest.raises(ValueError, match="signed key digest"):
        _config(secret=b"x" * 32)
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        _config(secret=b"short")


def test_parser_rejects_smuggled_fields_binding_key_mixup_and_expiry() -> None:
    config = _config()
    config["untrusted"] = True
    with pytest.raises(ValueError, match="config fields"):
        parse_endpoint_config(config)

    config = _config()
    config["key"]["key_id"] = "other-key-7f5c"
    with pytest.raises(ValueError, match="binding/key mismatch"):
        parse_endpoint_config(config)

    config = _config()
    with pytest.raises(ValueError, match="expired"):
        parse_endpoint_config(
            config, now=lambda: NOW + 8 * 60 * 60)


def test_sealed_config_is_immutable_and_exec_argv_contains_no_secret(
        monkeypatch) -> None:
    config = _config()
    fd = sealed_config_fd(config)
    try:
        assert json.loads(os.read(fd, 65536)) == config
        with pytest.raises(OSError) as caught:
            os.write(fd, b"tamper")
        assert caught.value.errno == errno.EPERM
    finally:
        os.close(fd)

    observed = {}

    def fake_execvp(program, argv):
        observed["program"] = program
        observed["argv"] = argv
        observed["config"] = json.loads(os.read(int(argv[-1]), 65536))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(os, "execvp", fake_execvp)
    with pytest.raises(RuntimeError, match="intercepted"):
        exec_endpoint(config, "/trusted/qdistro-mm-remote-adapter")
    assert observed["argv"] == [
        "/trusted/qdistro-mm-remote-adapter", "--config-fd",
        observed["argv"][-1],
    ]
    assert SECRET.hex() not in " ".join(observed["argv"])
    assert observed["config"] == config


def _json_pipe(value: object) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps(value).encode())
    os.close(write_fd)
    return read_fd


def _secret_pipe(secret: bytes) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, secret)
    os.close(write_fd)
    return read_fd


def test_main_closes_owned_inputs_and_hands_off_only_validated_config(
        monkeypatch) -> None:
    receipt, public_key = _inputs(int(time.time()))
    grant_fd = _json_pipe(receipt)
    secret_fd = _secret_pipe(SECRET)
    observed = {}

    monkeypatch.setattr(
        launcher, "_read_pinned_authority_key", lambda: public_key)
    monkeypatch.setattr(
        launcher, "exec_endpoint",
        lambda config, program: observed.update(
            config=config, program=program))
    result = launcher.main([
        "--grant-fd", str(grant_fd), "--secret-fd", str(secret_fd),
        "--role", "viewer", "--local-machine-id", "vm-viewer",
        "--session-id", SESSION, "--stream-id", STREAM,
    ])
    assert result == 1
    assert observed["program"] == "/usr/local/bin/qdistro-mm-remote-adapter"
    assert parse_endpoint_config(observed["config"])[0] == "viewer"
    for fd in (grant_fd, secret_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


def test_secret_reader_closes_wrong_length_fd() -> None:
    fd = _secret_pipe(b"short")
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        launcher._read_secret_fd(fd)
    with pytest.raises(OSError):
        os.fstat(fd)


def test_main_closes_unread_secret_when_grant_read_fails() -> None:
    grant_fd = _secret_pipe(b"not JSON")
    secret_fd = _secret_pipe(SECRET)
    with pytest.raises(ValueError, match="cannot read adapter session grant"):
        launcher.main([
            "--grant-fd", str(grant_fd), "--secret-fd", str(secret_fd),
            "--role", "viewer", "--local-machine-id", "vm-viewer",
            "--session-id", SESSION, "--stream-id", STREAM,
        ])
    for fd in (grant_fd, secret_fd):
        with pytest.raises(OSError):
            os.fstat(fd)
