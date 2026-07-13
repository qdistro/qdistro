#!/usr/bin/python3
"""Drive one side of the two-stream R7 nested product gate."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import signal
import socket
import subprocess
import time
from pathlib import Path

from multimachine.remote_nested_protocol import SourceHelperConfig
from multimachine.remote_nested_registry import (
    RegistryPrograms,
    RemoteNestedRegistry,
    RemoteNestedStreamSpec,
)
from multimachine.remote_nested_supervisor import SupervisorPrograms


SOURCE_RT = Path("/run/mm-r7-source")
VIEWER_RT = Path("/run/mm-r7-viewer")
MMBIN = Path("/tmp/mm/multimachine")
SESSION_ID = "viewer-session-r7-multistream"
STREAMS = (
    "stream_r7_alpha_0123456789",
    "stream_r7_beta_0123456789",
)
GENERATION = 73


def wait_match(path: Path, pattern: str, timeout: float = 45) -> re.Match[str]:
    expression = re.compile(pattern)
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = path.read_text(errors="replace") if path.exists() else ""
        match = expression.search(text)
        if match:
            return match
        time.sleep(0.1)
    raise RuntimeError(f"missing {pattern!r} in {path}\n{text[-8000:]}")


def wait_count(path: Path, pattern: str, count: int,
               timeout: float = 45) -> None:
    expression = re.compile(pattern)
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = path.read_text(errors="replace") if path.exists() else ""
        if len(expression.findall(text)) >= count:
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"missing count={count} for {pattern!r} in {path}\n{text[-8000:]}")


def wait_path(path: Path, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {path}")


def sealed_source_config(config: SourceHelperConfig) -> int:
    fd = os.memfd_create(
        "r7-source-helper-config", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    os.write(fd, config.encode())
    os.lseek(fd, 0, os.SEEK_SET)
    fcntl.fcntl(
        fd, fcntl.F_ADD_SEALS,
        fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK |
        fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
    return fd


def open_authority(index: int) -> tuple[int, ...]:
    prefix = f"/run/r7-product-{index}"
    return tuple(os.open(prefix + suffix, os.O_RDONLY | os.O_CLOEXEC) for suffix in (
        "-grant.json", "-secret.bin", "-cert.pem", "-key.pem", "-peer.pem"))


def programs() -> RegistryPrograms:
    return RegistryPrograms(
        supervisor=str(MMBIN / "qdistro-mm-remote-nested-session"),
        children=SupervisorPrograms(
            session_launcher=str(MMBIN / "qdistro-mm-remote-session-launcher"),
            endpoint=str(MMBIN / "qdistro-mm-remote-adapter"),
            controller=str(MMBIN / "qdistro-mm-remote-nested-controller"),
            source_helper="/usr/bin/qdistro-mm-remote-source-helper",
            viewer_helper="/usr/bin/qdistro-mm-remote-viewer-helper"))


class LoggedFactory:
    def __init__(self, runtime: Path):
        self.runtime = runtime
        self.files = []

    def __call__(self, argv, **kwargs):
        stream = argv[argv.index("--stream-id") + 1]
        log = (self.runtime / f"session-{stream}.log").open("wb")
        self.files.append(log)
        return subprocess.Popen(
            argv, stdout=log, stderr=subprocess.STDOUT, **kwargs)

    def close(self) -> None:
        for file in self.files:
            file.close()


def wait_registry_exit(registry: RemoteNestedRegistry, stream_id: str,
                       timeout: float = 45) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in registry.poll():
            if event.stream_id == stream_id:
                return event.returncode
            raise RuntimeError(f"unexpected sibling exit: {event}")
        time.sleep(0.1)
    raise RuntimeError(f"stream {stream_id} did not exit")


def source_specs(args: argparse.Namespace, close_children: list[int]) -> list[RemoteNestedStreamSpec]:
    outer = SOURCE_RT / "outer.log"
    wait_count(outer, r"nested-toplevel advertise pw_node=", 2)
    adverts = re.findall(
        r"nested-toplevel advertise pw_node='([^']+)' input_sink='([^']+)' "
        r"app_id=([^ ]+) title=(.*?) origin_uid=", outer.read_text(errors="replace"))
    if len(adverts) < 2:
        raise RuntimeError("source advertisements are incomplete")
    specs = []
    for index, (stream, advert) in enumerate(zip(STREAMS, adverts[:2]), 1):
        pw_node, input_sink, app_id, title = advert
        authority = open_authority(index)
        config_fd = sealed_source_config(SourceHelperConfig(
            source_revision=6 + index, pw_node=pw_node,
            input_sink=input_sink, app_id=app_id, title=title))
        specs.append(RemoteNestedStreamSpec(
            origin_machine_id="vm-source", stream_id=stream,
            generation=GENERATION, host="0.0.0.0",
            port=args.base_port + index - 1,
            grant_fd=authority[0], secret_fd=authority[1],
            tls_cert_fd=authority[2], tls_key_fd=authority[3],
            peer_cert_fd=authority[4], source_config_fd=config_fd,
            source_close_fd=close_children[index - 1]))
    return specs


def viewer_specs(args: argparse.Namespace) -> list[RemoteNestedStreamSpec]:
    specs = []
    for index, stream in enumerate(STREAMS, 1):
        authority = open_authority(index)
        specs.append(RemoteNestedStreamSpec(
            origin_machine_id="vm-source", stream_id=stream,
            generation=GENERATION, host="10.0.2.2",
            port=args.base_port + index - 1,
            grant_fd=authority[0], secret_fd=authority[1],
            tls_cert_fd=authority[2], tls_key_fd=authority[3],
            peer_cert_fd=authority[4]))
    return specs


def run_source(args: argparse.Namespace) -> dict:
    outer = SOURCE_RT / "outer.log"
    inner = SOURCE_RT / "inner.log"
    handles = [int(value) for value in re.findall(
        r"nested-proxy: created handle=([0-9]+)",
        outer.read_text(errors="replace"))[:2]]
    if len(handles) != 2 or handles[0] == handles[1]:
        raise RuntimeError(f"source handles are not distinct: {handles}")
    close_peers = []
    close_children = []
    for _ in STREAMS:
        peer, child = socket.socketpair()
        peer.settimeout(120)
        close_peers.append(peer)
        close_children.append(child.detach())
    factory = LoggedFactory(SOURCE_RT)
    registry = RemoteNestedRegistry(
        role="source", local_machine_id="vm-source", session_id=SESSION_ID,
        programs=programs(), process_factory=factory)
    env = os.environ.copy()
    env.update({"XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/admin"})
    old_env = os.environ.copy()
    os.environ.update(env)
    try:
        registry.add_many(source_specs(args, close_children))
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    try:
        session_logs = [SOURCE_RT / f"session-{stream}.log" for stream in STREAMS]
        for log in session_logs:
            wait_match(log, r"controller connected epoch=1")
            wait_match(log, r"decoder acknowledged media epoch start")
        wait_count(inner, r"qdwin/nested: key handle=[0-9]+ key=30 state=1", 2)
        wait_count(inner, r"qdwin/nested: key handle=[0-9]+ key=30 state=0", 2)
        app_pids = [int((SOURCE_RT / f"app{i}.pid").read_text().strip())
                    for i in (1, 2)]
        for pid in app_pids:
            os.kill(pid, 0)
        (SOURCE_RT / "two-ready").touch()

        readable, _, _ = select.select(close_peers, [], [], 120)
        if readable != [close_peers[1]] or close_peers[1].recv(1) != b"C":
            raise RuntimeError("stream-two close was not independently routed")
        qdenv = env | {"WAYLAND_DISPLAY": "r7-source-outer"}
        subprocess.run(
            ["qs", "-p", "/tmp/qdshell-r5", "ipc", "call",
             "qdwin", "closeWindow", str(handles[1])], env=qdenv,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        wait_count(inner, r"qdwin/nested: outer close_requested handle=", 1)
        for pid in app_pids:
            os.kill(pid, 0)

        os.kill(app_pids[1], signal.SIGTERM)
        if wait_registry_exit(registry, STREAMS[1]) != 0:
            raise RuntimeError("source stream two did not close cleanly")
        wait_match(outer, rf"nested-proxy: destroy handle={handles[1]}\b")
        os.kill(app_pids[0], 0)
        if ("vm-source", STREAMS[0]) not in registry.keys:
            raise RuntimeError("stream one source supervisor did not survive")
        return {
            "role": "source", "handles": handles,
            "two_sessions_connected": True,
            "two_decoder_acks": True,
            "two_qdni_round_trips": True,
            "both_apps_alive_before_failure": True,
            "targeted_close_only_stream_two": True,
            "ignored_close_preserved_both_apps": True,
            "source_close_removed_stream_two": True,
            "stream_one_app_and_supervisor_survived": True,
        }
    finally:
        registry.stop_all()
        factory.close()
        for peer in close_peers:
            peer.close()


def shell_list(env: dict[str, str]) -> str:
    return subprocess.run(
        ["qs", "-p", "/tmp/qdshell-r5", "ipc", "call",
         "multimachine", "list"], env=env, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True).stdout


def run_viewer(args: argparse.Namespace) -> dict:
    weston = VIEWER_RT / "weston.log"
    qdshell = VIEWER_RT / "qdshell.log"
    factory = LoggedFactory(VIEWER_RT)
    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="vm-viewer", session_id=SESSION_ID,
        programs=programs(), process_factory=factory)
    env = os.environ.copy()
    env.update({
        "XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/admin",
        "WAYLAND_DISPLAY": "r7-viewer",
    })
    old_env = os.environ.copy()
    os.environ.update(env)
    try:
        registry.add_many(viewer_specs(args))
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    try:
        for stream in STREAMS:
            wait_match(VIEWER_RT / f"session-{stream}.log",
                       r"controller connected epoch=1")
            wait_match(VIEWER_RT / f"session-{stream}.log",
                       r"received media epoch start")
            wait_match(qdshell, rf"nested_proxy_remote_identity handle=[0-9]+ "
                       rf"source=vm-source trust_domain=owner-machines stream={stream} "
                       rf"generation={GENERATION}")
        identities = re.findall(
            r"nested_proxy_remote_identity handle=([0-9]+) source=vm-source "
            r"trust_domain=owner-machines stream=(stream_r7_[a-z]+_0123456789)",
            qdshell.read_text(errors="replace"))
        by_stream = {stream: int(handle) for handle, stream in identities}
        handles = [by_stream[stream] for stream in STREAMS]
        if len(set(handles)) != 2:
            raise RuntimeError(f"viewer handles are not distinct: {handles}")
        for handle in handles:
            wait_match(weston, rf"bind_proxy_pixels handle={handle}\b")
            subprocess.run(
                ["qs", "-p", "/tmp/qdshell-r5", "ipc", "call",
                 "qdwin", "focusWindow", str(handle)], env=env, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        badge = shell_list(env)
        if (badge.count("REMOTE vm-source @ owner-machines") < 2
                or badge.count('\"authorized\":true') < 2
                or not all(stream in badge for stream in STREAMS)):
            raise RuntimeError("two protected identities missing from shell IPC\n" + badge)
        (VIEWER_RT / "two-ready").touch()
        wait_path(VIEWER_RT / "drop-stream-one")

        if not registry.remove("vm-source", STREAMS[0]):
            raise RuntimeError("stream one was not registered")
        wait_match(weston, rf"nested-proxy: destroy handle={handles[0]}\b")
        if ("vm-source", STREAMS[1]) not in registry.keys:
            raise RuntimeError("stream two supervisor died with sibling")
        if re.search(rf"nested-proxy: destroy handle={handles[1]}\b",
                     weston.read_text(errors="replace")):
            raise RuntimeError("stream two proxy died with sibling")

        subprocess.run(
            ["qs", "-p", "/tmp/qdshell-r5", "ipc", "call",
             "qdwin", "closeWindow", str(handles[1])], env=env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if wait_registry_exit(registry, STREAMS[1]) != 0:
            raise RuntimeError("viewer stream two did not close cleanly")
        wait_match(weston, rf"nested-proxy: destroy handle={handles[1]}\b")
        return {
            "role": "viewer", "handles": handles,
            "two_distinct_proxies": True,
            "two_pixel_feeds": True,
            "two_protected_badges": True,
            "both_handles_focusable": True,
            "stream_one_removed_independently": True,
            "stream_two_survived_sibling_failure": True,
            "stream_two_close_requested_upstream": True,
            "source_close_removed_stream_two": True,
        }
    finally:
        registry.stop_all()
        factory.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("source", "viewer"), required=True)
    parser.add_argument("--base-port", type=int, required=True)
    args = parser.parse_args()
    result = run_source(args) if args.role == "source" else run_viewer(args)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
