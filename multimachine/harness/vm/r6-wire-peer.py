#!/usr/bin/env python3
"""Guest-side controller for one R6 pinned-TLS wire-gate endpoint."""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/tmp/mm")

from multimachine.remote_adapter_transport import (
    MAX_LOCAL_FRAME_BYTES,
    recv_frame,
    send_frame,
)


def open_then_unlink(path: str) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    os.unlink(path)
    return fd


def send(controller: socket.socket, *, channel: str, kind: str,
         payload: bytes = b"", ack: int = 0) -> None:
    doc = {
        "op": "send", "channel": channel, "kind": kind, "ack": ack,
        "payload": base64.urlsafe_b64encode(payload).rstrip(b"=").decode(),
    }
    send_frame(
        controller, json.dumps(doc, separators=(",", ":")).encode(),
        maximum=MAX_LOCAL_FRAME_BYTES)


def event(controller: socket.socket) -> dict:
    payload = recv_frame(controller, maximum=MAX_LOCAL_FRAME_BYTES)
    if payload is None:
        raise RuntimeError("adapter local control closed before expected event")
    return json.loads(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=("source", "viewer"), required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--grant", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--peer-cert", required=True)
    args = ap.parse_args()

    grant_fd = open_then_unlink(args.grant)
    secret_fd = open_then_unlink(args.secret)
    cert_fd = open_then_unlink(args.cert)
    key_fd = open_then_unlink(args.key)
    peer_fd = open_then_unlink(args.peer_cert)
    endpoint_socket, controller = socket.socketpair()
    endpoint_fd = endpoint_socket.detach()
    local_machine = "vm-source" if args.role == "source" else "vm-viewer"
    launcher = "/tmp/mm/multimachine/qdistro-mm-remote-session-launcher"
    endpoint = "/tmp/mm/multimachine/qdistro-mm-remote-adapter"
    cmd = [
        launcher,
        "--grant-fd", str(grant_fd),
        "--secret-fd", str(secret_fd),
        "--tls-cert-fd", str(cert_fd),
        "--tls-key-fd", str(key_fd),
        "--peer-cert-fd", str(peer_fd),
        "--local-fd", str(endpoint_fd),
        "--role", args.role,
        "--local-machine-id", local_machine,
        "--session-id", "viewer-session-r6-wire",
        "--stream-id", "stream_r6_wire_0123456789",
        "--host", args.host,
        "--port", str(args.port),
        "--endpoint-program", endpoint,
    ]
    process = subprocess.Popen(
        cmd, pass_fds=(grant_fd, secret_fd, cert_fd, key_fd, peer_fd,
                       endpoint_fd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for fd in (grant_fd, secret_fd, cert_fd, key_fd, peer_fd, endpoint_fd):
        os.close(fd)
    controller.settimeout(20)
    result = {"role": args.role, "profile": "lan-clean"}
    try:
        connected = event(controller)
        result["connected"] = connected
        if args.role == "source":
            send(controller, channel="control", kind="announce",
                 payload=b'{"source_revision":1}')
            result["announce_sent"] = event(controller)
            for frame in (b"frame-one", b"frame-two"):
                send(controller, channel="media", kind="frame", payload=frame)
                event(controller)
            result["ack_received"] = event(controller)
            send(controller, channel="media", kind="frame",
                 payload=b"frame-three")
            result["third_sent"] = event(controller)
            result["input_received"] = event(controller)
            result["detached"] = event(controller)
        else:
            result["announce_received"] = event(controller)
            first = event(controller)
            second = event(controller)
            result["media_received"] = [first["seq"], second["seq"]]
            send(controller, channel="control", kind="media_ack", ack=1)
            result["ack_sent"] = event(controller)
            result["third_received"] = event(controller)
            send(controller, channel="input", kind="key",
                 payload=b'{"code":42,"pressed":true}')
            result["input_sent"] = event(controller)
            controller.close()
        stdout, stderr = process.communicate(timeout=20)
        result["endpoint_returncode"] = process.returncode
        result["endpoint_stdout"] = stdout
        result["endpoint_stderr"] = stderr
        if process.returncode != 0:
            raise RuntimeError(
                f"endpoint failed rc={process.returncode}: {stderr}")
    except Exception as exc:
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        raise RuntimeError(
            f"{exc}; endpoint rc={process.returncode}; "
            f"stdout={stdout!r}; stderr={stderr!r}") from exc
    finally:
        controller.close()
        if process.poll() is None:
            process.kill()
            process.communicate()
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
