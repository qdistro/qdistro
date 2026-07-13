#!/usr/bin/env python3
"""Run the clean-link R6 authority/launcher/TLS wire gate on two VMs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from multimachine.harness.vm_backend import QciVMBackend  # noqa: E402
from multimachine.mm_pairing_authority import public_key_bytes  # noqa: E402
from multimachine.mm_remote_session_authority import (  # noqa: E402
    issue_remote_session_grant,
)


def certificate(common_name: str) -> tuple[bytes, bytes, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
                ExtendedKeyUsageOID.CLIENT_AUTH,
            ]), False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    pin = hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_pem, key_pem, pin


def parse_result(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "role" in value:
            return value
    raise ValueError(f"peer produced no result JSON: {output[-2000:]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vm_source")
    ap.add_argument("vm_viewer")
    ap.add_argument("--port", type=int, default=14443)
    args = ap.parse_args()
    bundle = Path("/tmp/mm-live/r6-wire")
    bundle.mkdir(parents=True, exist_ok=True)
    backend = QciVMBackend(args.vm_source, args.vm_viewer, REPO)
    backend._ensure_hostfwd(args.vm_source, args.port)

    source_cert, source_key, source_pin = certificate("vm-source")
    viewer_cert, viewer_key, viewer_pin = certificate("vm-viewer")
    secret = os.urandom(32)
    authority = Ed25519PrivateKey.generate()
    now = int(time.time())
    receipt = issue_remote_session_grant(
        source_machine="vm-source", viewer_machine="vm-viewer",
        trust_domain_id="owner-machines", generation=71,
        session_id="viewer-session-r6-wire",
        stream_id="stream_r6_wire_0123456789", key_id="adapter-key-r6-wire",
        session_secret=secret, issued_at=now, handoff_expires_at=now + 300,
        key_expires_at=now + 8 * 60 * 60, allow_input=True,
        source_tls_cert_sha256=source_pin,
        viewer_tls_cert_sha256=viewer_pin, private_key=authority)

    peer_script = REPO / "multimachine/harness/vm/r6-wire-peer.py"
    entrypoints = [
        REPO / "multimachine/qdistro-mm-remote-session-launcher",
        REPO / "multimachine/qdistro-mm-remote-adapter",
    ]
    with tempfile.TemporaryDirectory(prefix="qdistro-r6-wire-") as tmp:
        tmpdir = Path(tmp)
        common = {
            "grant.json": json.dumps(receipt).encode(),
            "secret.bin": secret,
        }
        per_role = {
            "source": {
                **common, "cert.pem": source_cert, "key.pem": source_key,
                "peer.pem": viewer_cert,
            },
            "viewer": {
                **common, "cert.pem": viewer_cert, "key.pem": viewer_key,
                "peer.pem": source_cert,
            },
        }
        for vm, role in ((args.vm_source, "source"),
                         (args.vm_viewer, "viewer")):
            backend._push_mm_package(vm)
            for entrypoint in entrypoints:
                backend._push(
                    vm, entrypoint, f"/tmp/mm/multimachine/{entrypoint.name}",
                    mode=0o755)
            backend._push(vm, peer_script, "/tmp/r6-wire-peer.py", mode=0o755)
            backend._vmexec(vm, "mkdir -p /etc/qdistro/multimachine")
            pin_path = tmpdir / f"{role}-authority.pub"
            pin_path.write_bytes(public_key_bytes(authority.public_key()))
            backend._push(
                vm, pin_path,
                "/etc/qdistro/multimachine/pairing-authority.ed25519.pub")
            for name, payload in per_role[role].items():
                local = tmpdir / f"{role}-{name}"
                local.write_bytes(payload)
                backend._push(vm, local, f"/run/r6-{name}", mode=0o600)

    vm_exec = REPO / "scripts/vm/vm-exec"
    base = [
        "--port", str(args.port), "--grant", "/run/r6-grant.json",
        "--secret", "/run/r6-secret.bin", "--cert", "/run/r6-cert.pem",
        "--key", "/run/r6-key.pem", "--peer-cert", "/run/r6-peer.pem",
    ]
    source_cmd = " ".join([
        "python3", "/tmp/r6-wire-peer.py", "--role", "source",
        "--host", "0.0.0.0", *base])
    viewer_cmd = " ".join([
        "python3", "/tmp/r6-wire-peer.py", "--role", "viewer",
        "--host", "10.0.2.2", *base])
    source_process = subprocess.Popen(
        [str(vm_exec), args.vm_source, source_cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    viewer_process = subprocess.Popen(
        [str(vm_exec), args.vm_viewer, viewer_cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    source_output, _ = source_process.communicate(timeout=90)
    viewer_output, _ = viewer_process.communicate(timeout=90)
    (bundle / "source.log").write_text(source_output, encoding="utf-8")
    (bundle / "viewer.log").write_text(viewer_output, encoding="utf-8")
    source = parse_result(source_output) if source_process.returncode == 0 else {}
    viewer = parse_result(viewer_output) if viewer_process.returncode == 0 else {}
    assertions = {
        "source-peer-exited-cleanly": source_process.returncode == 0,
        "viewer-peer-exited-cleanly": viewer_process.returncode == 0,
        "mutual-tls13-and-pins": (
            source.get("connected", {}).get("tls_version") == "TLSv1.3"
            and viewer.get("connected", {}).get("tls_version") == "TLSv1.3"
            and source.get("connected", {}).get("peer_cert_sha256") == viewer_pin
            and viewer.get("connected", {}).get("peer_cert_sha256") == source_pin),
        "authenticated-control-announce": (
            viewer.get("announce_received", {}).get("kind") == "announce"),
        "bounded-media-with-decoder-ack": (
            viewer.get("media_received") == [1, 2]
            and source.get("ack_received", {}).get("ack") == 1
            and viewer.get("third_received", {}).get("seq") == 3),
        "authenticated-input-landed": (
            source.get("input_received", {}).get("kind") == "key"),
        "disconnect-released-held-input": (
            source.get("detached", {}).get("releases")
            == [{"code": 42, "kind": "key"}]),
    }
    result = {
        "schema": "qdistro-mm-r6-wire-evidence-v1",
        "profile": "lan-clean",
        "source_vm": args.vm_source,
        "viewer_vm": args.vm_viewer,
        "assertions": assertions,
        "source": source,
        "viewer": viewer,
    }
    (bundle / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name, passed in assertions.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    return 0 if all(assertions.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
