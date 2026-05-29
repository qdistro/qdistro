#!/usr/bin/env python3
"""Thin client for qdistro-media-exec.

Connects to the media-exec AF_UNIX socket, sends one JSON request
(passed as argv[1], already-built by the caller), and prints the single
reply line to stdout. Exit code 0 always when a reply was received; the
reply's ``ok`` field carries the real success/failure (qdshell parses
the JSON, never the exit code, for the verdict — but a non-zero exit on
connect failure is still a fail-closed signal).

qdshell (unprivileged) invokes this via Quickshell.execDetached/Process
with a TOKENIZED argv: ``python3 <this> '<json>'``. The JSON is built by
Services/Qdshell/RemovableMedia.js::buildMediaRequest, so device labels
are carried as a display-only ``label`` field and never interpolated
into any command.
"""
from __future__ import annotations

import json
import socket
import sys

SOCKET_PATH = "/run/qdistro-media-exec/sock"
_RECV_TIMEOUT_S = 920  # slightly longer than the helper's WaitForDecision


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"type": "result", "ok": False,
                          "error": "usage: client.py <json-request>"}))
        return 1
    try:
        req = json.loads(sys.argv[1])
    except (ValueError, TypeError):
        print(json.dumps({"type": "result", "ok": False,
                          "error": "malformed request json"}))
        return 1
    if not isinstance(req, dict):
        print(json.dumps({"type": "result", "ok": False,
                          "error": "request must be an object"}))
        return 1
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(_RECV_TIMEOUT_S)
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0].decode(errors="replace")
        print(line if line else json.dumps(
            {"type": "result", "ok": False, "error": "no reply"}))
        return 0
    except OSError as e:
        print(json.dumps({"type": "result", "ok": False,
                          "error": f"media-exec unreachable: {e}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
