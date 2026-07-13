#!/usr/bin/python3
"""Drive one real side of the supervised R6 nested product gate."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

from multimachine.remote_nested_protocol import SourceHelperConfig


SOURCE_RT = Path("/run/mm-r6-source")
VIEWER_RT = Path("/run/mm-r6-viewer")
MMBIN = Path("/tmp/mm/multimachine")


def wait_match(path: Path, pattern: str, timeout: float = 30) -> re.Match[str]:
    expression = re.compile(pattern)
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = path.read_text(errors="replace") if path.exists() else ""
        match = expression.search(text)
        if match:
            return match
        time.sleep(0.1)
    raise RuntimeError(f"missing {pattern!r} in {path}\n{text[-5000:]}")


def wait_count(path: Path, pattern: str, count: int,
               timeout: float = 30) -> None:
    expression = re.compile(pattern)
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = path.read_text(errors="replace") if path.exists() else ""
        if len(expression.findall(text)) >= count:
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"missing count={count} for {pattern!r} in {path}\n{text[-5000:]}")


def sealed_source_config(config: SourceHelperConfig) -> int:
    fd = os.memfd_create(
        "r6-source-helper-config", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    os.write(fd, config.encode())
    os.lseek(fd, 0, os.SEEK_SET)
    fcntl.fcntl(
        fd, fcntl.F_ADD_SEALS,
        fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK |
        fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
    return fd


def open_inputs(args: argparse.Namespace) -> list[int]:
    return [
        os.open(args.grant, os.O_RDONLY | os.O_CLOEXEC),
        os.open(args.secret, os.O_RDONLY | os.O_CLOEXEC),
        os.open(args.cert, os.O_RDONLY | os.O_CLOEXEC),
        os.open(args.key, os.O_RDONLY | os.O_CLOEXEC),
        os.open(args.peer_cert, os.O_RDONLY | os.O_CLOEXEC),
    ]


def supervisor_argv(args: argparse.Namespace, fds: list[int]) -> list[str]:
    role_machine = "vm-source" if args.role == "source" else "vm-viewer"
    return [
        str(MMBIN / "qdistro-mm-remote-nested-session"),
        "--role", args.role,
        "--grant-fd", str(fds[0]),
        "--secret-fd", str(fds[1]),
        "--tls-cert-fd", str(fds[2]),
        "--tls-key-fd", str(fds[3]),
        "--peer-cert-fd", str(fds[4]),
        "--local-machine-id", role_machine,
        "--session-id", "viewer-session-r6-product",
        "--stream-id", "stream_r6_product_0123456789",
        "--host", args.host,
        "--port", str(args.port),
        "--session-launcher-program",
        str(MMBIN / "qdistro-mm-remote-session-launcher"),
        "--endpoint-program", str(MMBIN / "qdistro-mm-remote-adapter"),
        "--controller-program",
        str(MMBIN / "qdistro-mm-remote-nested-controller"),
        "--source-helper-program", "/usr/bin/qdistro-mm-remote-source-helper",
        "--viewer-helper-program", "/usr/bin/qdistro-mm-remote-viewer-helper",
    ]


def run_source(args: argparse.Namespace) -> dict:
    outer = SOURCE_RT / "outer.log"
    inner = SOURCE_RT / "inner.log"
    advertise = wait_match(
        outer,
        r"nested-toplevel advertise pw_node='([^']+)' input_sink='([^']+)' "
        r"app_id=([^ ]+) title=(.*?) origin_uid=")
    pw_node, input_sink, app_id, title = advertise.groups()
    handle = int(wait_match(
        outer, r"nested-proxy: created handle=([0-9]+)").group(1))
    config_fd = sealed_source_config(SourceHelperConfig(
        source_revision=7, pw_node=pw_node, input_sink=input_sink,
        app_id=app_id, title=title))
    close_peer, close_child = socket.socketpair()
    authority_fds = open_inputs(args)
    argv = supervisor_argv(args, authority_fds) + [
        "--source-config-fd", str(config_fd),
        "--source-close-fd", str(close_child.fileno()),
    ]
    session_log = SOURCE_RT / "session.log"
    env = os.environ.copy()
    env.update({"XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/admin"})
    with session_log.open("wb") as log:
        session = subprocess.Popen(
            argv, pass_fds=tuple(authority_fds) +
            (config_fd, close_child.fileno()), env=env,
            stdout=log, stderr=subprocess.STDOUT)
    for fd in authority_fds + [config_fd]:
        os.close(fd)
    close_child.close()
    close_peer.settimeout(45)
    try:
        wait_match(session_log, r"controller connected epoch=1")
        wait_count(session_log, r"decoder acknowledged media epoch start", 1)
        wait_match(inner, r"qdwin/nested: key handle=[0-9]+ key=30 state=1")
        wait_match(inner, r"qdwin/nested: key handle=[0-9]+ key=30 state=0")
        app_pid = int((SOURCE_RT / "app.pid").read_text().strip())
        os.kill(app_pid, 0)

        (SOURCE_RT / "transport-drop-ready").touch()
        deadline = time.monotonic() + 30
        while (not (SOURCE_RT / "transport-dropped").exists()
               and time.monotonic() < deadline):
            time.sleep(0.1)
        if not (SOURCE_RT / "transport-dropped").exists():
            raise RuntimeError("root harness did not inject transport drop")
        wait_match(session_log, r"controller detached epoch=1")
        os.kill(app_pid, 0)
        wait_match(session_log, r"controller connected epoch=2 cached_frame=1")
        wait_count(session_log, r"decoder acknowledged media epoch start", 2)

        if close_peer.recv(1) != b"C":
            raise RuntimeError("source close supervisor byte is invalid")
        qdenv = env | {"WAYLAND_DISPLAY": "r6-source-outer"}
        subprocess.run(
            ["qs", "-p", "/tmp/qdshell-r5", "ipc", "call",
             "qdwin", "closeWindow", str(handle)],
            env=qdenv, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        wait_match(inner, r"qdwin/nested: outer close_requested handle=")
        time.sleep(1)
        os.kill(app_pid, 0)
        if re.search(
                rf"nested-proxy: destroy handle={handle}\b",
                outer.read_text(errors="replace")):
            raise RuntimeError("viewer close destroyed the source proxy")

        os.kill(app_pid, signal.SIGTERM)
        result = session.wait(timeout=30)
        if result != 0:
            raise RuntimeError(
                f"source supervised session exited {result}\n" +
                session_log.read_text(errors="replace")[-5000:])
        wait_match(outer, rf"nested-proxy: destroy handle={handle}\b")
        return {
            "role": "source", "handle": handle,
            "initial_decoder_ack": True, "input_round_trip": True,
            "transport_detached": True, "app_survived_detach": True,
            "epoch_2_cached_frame_ack": True,
            "viewer_close_was_source_mediated": True,
            "ignored_close_preserved_app": True,
            "source_close_removed_proxy": True,
        }
    finally:
        close_peer.close()
        if session.poll() is None:
            session.terminate()
            try:
                session.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.kill()


def run_viewer(args: argparse.Namespace) -> dict:
    weston = VIEWER_RT / "weston.log"
    session_log = VIEWER_RT / "session.log"
    authority_fds = open_inputs(args)
    argv = supervisor_argv(args, authority_fds)
    env = os.environ.copy()
    env.update({
        "XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/admin",
        "WAYLAND_DISPLAY": "r6-viewer",
    })
    with session_log.open("wb") as log:
        session = subprocess.Popen(
            argv, pass_fds=tuple(authority_fds), env=env,
            stdout=log, stderr=subprocess.STDOUT)
    for fd in authority_fds:
        os.close(fd)
    try:
        wait_match(session_log, r"controller connected epoch=1")
        handle = int(wait_match(
            weston, r"nested-proxy: created handle=([0-9]+)").group(1))
        wait_match(weston, rf"bind_proxy_pixels handle={handle}\b")
        wait_count(session_log, r"received media epoch start", 1)
        wait_match(session_log, r"controller detached epoch=1")
        if re.search(
                rf"nested-proxy: destroy handle={handle}\b",
                weston.read_text(errors="replace")):
            raise RuntimeError("detach destroyed viewer proxy")
        wait_match(session_log, r"controller connected epoch=2")
        wait_count(session_log, r"received media epoch start", 2)

        subprocess.run(
            ["qs", "-p", "/tmp/qdshell-r5", "ipc", "call",
             "qdwin", "closeWindow", str(handle)],
            env=env, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        result = session.wait(timeout=45)
        if result != 0:
            raise RuntimeError(
                f"viewer supervised session exited {result}\n" +
                session_log.read_text(errors="replace")[-5000:])
        wait_match(weston, rf"nested-proxy: destroy handle={handle}\b")
        return {
            "role": "viewer", "handle": handle,
            "proxy_bound_pixels": True, "transport_detached": True,
            "detach_preserved_proxy": True,
            "epoch_2_media_received": True,
            "close_requested_upstream": True,
            "source_close_removed_proxy": True,
        }
    finally:
        if session.poll() is None:
            session.terminate()
            try:
                session.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.kill()


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
    result = run_source(args) if args.role == "source" else run_viewer(args)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
