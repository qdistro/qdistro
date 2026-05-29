#!/usr/bin/env python3
"""qsu — qdistro sudo replacement (v1, non-pty).

Usage:
    qsu [-u TARGET_USER] [--workflow-run RUN_ID] command [args...]

Connects to /run/qdistro-root-exec.sock, sends a JSON request for the
privileged exec, streams stdout/stderr back to the caller, and exits
with the target command's exit code. The admin broker mediates
approval — the first call for a given (uid, argv) typically prompts;
subsequent calls within the approved scope are cache/rule hits.

Limitations in v1 (documented, not bugs):
- No pty. Commands that want a controlling tty (vim, top, less) will
  misbehave; use only for non-interactive commands for now.
- No stdin forwarding. The target command runs with /dev/null as
  stdin.
- target_user defaults to `root`. Supplying -u <user> works but
  non-root targets are less tested.
- Doesn't rewrite `sudo` argv conventions (no -i, -s, -E, -H). A
  separate sudo-compat wrapper is the Phase-2 follow-up.

Spec: `doc/sudo.md`.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys


SOCKET_PATH = "/run/qdistro-root-exec/sock"


def _parse_argv(argv: list[str]) -> tuple[str, str, list[str]]:
    """Return (target_user, run_id, command_argv). Flags must precede the
    command. ``run_id`` is "" when --workflow-run was not given."""
    ap = argparse.ArgumentParser(
        prog="qsu",
        description="qdistro sudo replacement",
        allow_abbrev=False,
    )
    ap.add_argument("-u", "--user", default="root",
                    help="target user (default: root)")
    ap.add_argument(
        "--workflow-run", dest="run_id", default="",
        help=("workflow run id to inherit non-secret channel_env from "
              "(e.g. SSH_AUTH_SOCK for git-sign); omit for normal behaviour"))
    # Grab the remainder without argparse trying to interpret flags.
    # argparse.REMAINDER is deprecated-but-still-present; nargs=* +
    # '--' gets awkward on shebang-driven calls, so we accept the
    # deprecation for better UX.
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="command + args")
    args = ap.parse_args(argv)
    cmd = args.command
    # argparse.REMAINDER keeps the literal `--` separator (if present)
    # at the head of the captured list. Drop it so callers that follow
    # the conventional `qsu -u root -- id -u` shape don't end up trying
    # to exec "--" as the target program.
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("command required")
    return args.user, args.run_id, cmd


def _stream(sock: socket.socket) -> int:
    """Consume JSON frames from the server until an exit or error is
    seen; write stdout/stderr through to the real tty. Return the
    exit code reported by the server (non-zero if the server denied
    before forking)."""
    buf = b""
    exit_code = 1
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line:
                continue
            try:
                frame = json.loads(line.decode())
            except json.JSONDecodeError:
                sys.stderr.write(f"qsu: malformed server frame: {line!r}\n")
                continue
            kind = frame.get("type")
            if kind == "stdout":
                sys.stdout.write(frame.get("data", ""))
                sys.stdout.flush()
            elif kind == "stderr":
                sys.stderr.write(frame.get("data", ""))
                sys.stderr.flush()
            elif kind == "error":
                sys.stderr.write(f"qsu: {frame.get('message', 'error')}\n")
            elif kind == "exit":
                return int(frame.get("code", 1))
            else:
                sys.stderr.write(f"qsu: unknown frame type: {kind!r}\n")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    target_user, run_id, cmd = _parse_argv(
        sys.argv[1:] if argv is None else argv)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        sys.stderr.write(
            f"qsu: cannot reach qdistro-root-exec ({e}). "
            "Is the service active? `systemctl status qdistro-root-exec.socket`\n"
        )
        return 2
    try:
        request = {
            "target_user": target_user,
            "argv": cmd,
            "caller_name": "qsu",
        }
        # Optional workflow-run handshake: when present, the root-exec
        # daemon asks the broker for this run's non-secret channel_env
        # (allowlisted refs like SSH_AUTH_SOCK) and folds them into the
        # child's environment before exec. Omitting it = current behaviour.
        if run_id:
            request["run_id"] = run_id
        sock.sendall((json.dumps(request) + "\n").encode())
        return _stream(sock)
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
