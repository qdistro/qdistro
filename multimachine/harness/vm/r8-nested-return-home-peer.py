#!/usr/bin/python3
"""Drive one side of the R8 two-stream detach/return-home/remount gate."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
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
SESSION_ID = "viewer-session-r8-return-home"
STREAMS = (
    "stream_r8_alpha_0123456789",
    "stream_r8_beta_0123456789",
)
GENERATIONS = {1: 74, 2: 75}


def wait_match(path: Path, pattern: str, timeout: float = 60) -> re.Match[str]:
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
               timeout: float = 60) -> None:
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


def sealed_source_config(config: SourceHelperConfig, phase: int) -> int:
    fd = os.memfd_create(
        f"r8-source-helper-config-{phase}",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    os.write(fd, config.encode())
    os.lseek(fd, 0, os.SEEK_SET)
    fcntl.fcntl(
        fd, fcntl.F_ADD_SEALS,
        fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK |
        fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
    return fd


def open_authority(phase: int, index: int) -> tuple[int, ...]:
    prefix = f"/run/r8-product-{phase}-{index}"
    suffixes = (
        "-grant.json", "-secret.bin", "-cert.pem", "-key.pem", "-peer.pem")
    return tuple(os.open(prefix + suffix, os.O_RDONLY | os.O_CLOEXEC)
                 for suffix in suffixes)


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
        self.phase = 1
        self.files = []

    def __call__(self, argv, **kwargs):
        stream = argv[argv.index("--stream-id") + 1]
        log = (self.runtime / f"phase{self.phase}-session-{stream}.log").open("wb")
        self.files.append(log)
        return subprocess.Popen(
            argv, stdout=log, stderr=subprocess.STDOUT, **kwargs)

    def close(self) -> None:
        for file in self.files:
            file.close()


def source_specs(args: argparse.Namespace, phase: int,
                 close_children: list[int]) -> list[RemoteNestedStreamSpec]:
    outer = SOURCE_RT / "outer.log"
    wait_count(outer, r"nested-toplevel advertise pw_node=", 2)
    adverts = re.findall(
        r"nested-toplevel advertise pw_node='([^']+)' input_sink='([^']+)' "
        r"app_id=([^ ]+) title=(.*?) origin_uid=",
        outer.read_text(errors="replace"))
    if len(adverts) < 2:
        raise RuntimeError("source advertisements are incomplete")
    specs = []
    for index, (stream, advert) in enumerate(zip(STREAMS, adverts[:2]), 1):
        pw_node, input_sink, app_id, title = advert
        authority = open_authority(phase, index)
        config_fd = sealed_source_config(SourceHelperConfig(
            source_revision=phase * 10 + index, pw_node=pw_node,
            input_sink=input_sink, app_id=app_id, title=title), phase)
        specs.append(RemoteNestedStreamSpec(
            origin_machine_id="vm-source", stream_id=stream,
            generation=GENERATIONS[phase], host="0.0.0.0",
            port=args.base_port + index - 1,
            grant_fd=authority[0], secret_fd=authority[1],
            tls_cert_fd=authority[2], tls_key_fd=authority[3],
            peer_cert_fd=authority[4], source_config_fd=config_fd,
            source_close_fd=close_children[index - 1]))
    return specs


def viewer_specs(args: argparse.Namespace,
                 phase: int) -> list[RemoteNestedStreamSpec]:
    specs = []
    for index, stream in enumerate(STREAMS, 1):
        authority = open_authority(phase, index)
        specs.append(RemoteNestedStreamSpec(
            origin_machine_id="vm-source", stream_id=stream,
            generation=GENERATIONS[phase], host="10.0.2.2",
            port=args.base_port + index - 1,
            grant_fd=authority[0], secret_fd=authority[1],
            tls_cert_fd=authority[2], tls_key_fd=authority[3],
            peer_cert_fd=authority[4]))
    return specs


def phase_logs(runtime: Path, phase: int) -> list[Path]:
    return [runtime / f"phase{phase}-session-{stream}.log" for stream in STREAMS]


def wait_connected(runtime: Path, phase: int, *, viewer: bool) -> None:
    for log in phase_logs(runtime, phase):
        wait_match(log, r"controller connected epoch=1")
        wait_match(log, (r"received media epoch start" if viewer else
                         r"decoder acknowledged media epoch start"))


def shell_call(env: dict[str, str], target: str, method: str,
               *args: str) -> str:
    return subprocess.run(
        ["qs", "-p", "/tmp/qdshell-r5", "ipc", "call",
         target, method, *args], env=env, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True).stdout


def shell_list(env: dict[str, str]) -> str:
    return shell_call(env, "multimachine", "list")


def wait_shell(env: dict[str, str], predicate, label: str,
               timeout: float = 60) -> str:
    deadline = time.monotonic() + timeout
    observed = ""
    while time.monotonic() < deadline:
        observed = shell_list(env)
        if predicate(observed):
            return observed
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {label}\n{observed}")


def new_close_fds() -> tuple[list[object], list[int]]:
    peers = []
    children = []
    import socket
    for _stream in STREAMS:
        peer, child = socket.socketpair()
        peers.append(peer)
        children.append(child.detach())
    return peers, children


def run_source(args: argparse.Namespace) -> dict:
    outer = SOURCE_RT / "outer.log"
    inner = SOURCE_RT / "inner.log"
    handles = [int(value) for value in re.findall(
        r"nested-proxy: created handle=([0-9]+)",
        outer.read_text(errors="replace"))[:2]]
    if len(handles) != 2 or len(set(handles)) != 2:
        raise RuntimeError(f"source handles are not distinct: {handles}")
    app_pids = [int((SOURCE_RT / f"app{i}.pid").read_text().strip())
                for i in (1, 2)]
    factory = LoggedFactory(SOURCE_RT)
    registry = RemoteNestedRegistry(
        role="source", local_machine_id="vm-source", session_id=SESSION_ID,
        programs=programs(), process_factory=factory)
    env = os.environ.copy() | {
        "XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/admin",
        "WAYLAND_DISPLAY": "r7-source-outer",
    }
    close_peers = []
    old_env = os.environ.copy()
    try:
        for phase in (1, 2):
            peers, children = new_close_fds()
            close_peers.extend(peers)
            factory.phase = phase
            os.environ.update(env)
            try:
                registry.add_many(source_specs(args, phase, children))
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            wait_connected(SOURCE_RT, phase, viewer=False)
            wait_count(inner, r"qdwin/nested: key handle=[0-9]+ key=30 state=1",
                       phase * 2)
            wait_count(inner, r"qdwin/nested: key handle=[0-9]+ key=30 state=0",
                       phase * 2)
            for pid in app_pids:
                os.kill(pid, 0)

            if phase == 1:
                (SOURCE_RT / "r8-phase-one-ready").touch()
                wait_path(SOURCE_RT / "r8-detach")
                detached = registry.detach_all()
                if detached != tuple(("vm-source", stream) for stream in STREAMS):
                    raise RuntimeError(f"unexpected source detach set: {detached}")
                for pid in app_pids:
                    os.kill(pid, 0)
                text = outer.read_text(errors="replace")
                if any(re.search(rf"nested-proxy: destroy handle={handle}\b", text)
                       for handle in handles):
                    raise RuntimeError("a local source window died at detach")
                for handle in handles:
                    shell_call(env, "qdwin", "focusWindow", str(handle))
                (SOURCE_RT / "r8-detached-ready").touch()
                wait_path(SOURCE_RT / "r8-remount")

        if registry.keys != tuple(("vm-source", stream) for stream in STREAMS):
            raise RuntimeError(f"wrong source remount keys: {registry.keys}")
        for pid in app_pids:
            os.kill(pid, 0)
        (SOURCE_RT / "r8-remounted-ready").touch()
        wait_path(SOURCE_RT / "r8-finish")
        return {
            "role": "source", "handles": handles, "app_pids": app_pids,
            "initial_sessions_connected": True,
            "detach_set_exact": True,
            "source_apps_survived_detach": True,
            "source_pids_unchanged": True,
            "local_windows_focusable_after_detach": True,
            "fresh_generation_sessions_connected": True,
            "remount_registry_exact": True,
            "remount_qdni_round_trips": True,
        }
    finally:
        registry.stop_all()
        factory.close()
        for peer in close_peers:
            peer.close()


def viewer_handles(qdshell: Path, generation: int) -> list[int]:
    identities = re.findall(
        rf"nested_proxy_remote_identity handle=([0-9]+) source=vm-source "
        rf"trust_domain=owner-machines stream=(stream_r8_[a-z]+_0123456789) "
        rf"generation={generation}", qdshell.read_text(errors="replace"))
    by_stream = {stream: int(handle) for handle, stream in identities}
    if set(by_stream) != set(STREAMS):
        raise RuntimeError(
            f"generation {generation} identities incomplete: {by_stream}")
    return [by_stream[stream] for stream in STREAMS]


def run_viewer(args: argparse.Namespace) -> dict:
    weston = VIEWER_RT / "weston.log"
    qdshell = VIEWER_RT / "qdshell.log"
    factory = LoggedFactory(VIEWER_RT)
    registry = RemoteNestedRegistry(
        role="viewer", local_machine_id="vm-viewer", session_id=SESSION_ID,
        programs=programs(), process_factory=factory)
    env = os.environ.copy() | {
        "XDG_RUNTIME_DIR": "/run/user/1000", "HOME": "/home/admin",
        "WAYLAND_DISPLAY": "r7-viewer",
    }
    old_env = os.environ.copy()
    handles_by_phase = {}
    try:
        for phase in (1, 2):
            factory.phase = phase
            os.environ.update(env)
            try:
                registry.add_many(viewer_specs(args, phase))
            finally:
                os.environ.clear()
                os.environ.update(old_env)
            wait_connected(VIEWER_RT, phase, viewer=True)
            generation = GENERATIONS[phase]
            for stream in STREAMS:
                wait_match(
                    qdshell,
                    rf"nested_proxy_remote_identity handle=[0-9]+ "
                    rf"source=vm-source trust_domain=owner-machines "
                    rf"stream={stream} generation={generation}")
            handles = viewer_handles(qdshell, generation)
            handles_by_phase[phase] = handles
            if len(set(handles)) != 2:
                raise RuntimeError(f"viewer phase {phase} handles duplicate: {handles}")
            for handle in handles:
                wait_match(weston, rf"bind_proxy_pixels handle={handle}\b")
                shell_call(env, "qdwin", "focusWindow", str(handle))
            listing = wait_shell(
                env,
                lambda text: (
                    text.count('"authorized":true') == 2
                    and text.count("REMOTE vm-source @ owner-machines") == 2
                    and all(text.count(f'"streamId":"{stream}"') == 1
                            for stream in STREAMS)),
                f"exact protected stream set for phase {phase}")

            if phase == 1:
                (VIEWER_RT / "r8-phase-one-ready").touch()
                wait_path(VIEWER_RT / "r8-detach")
                detached = registry.detach_all()
                if detached != tuple(("vm-source", stream) for stream in STREAMS):
                    raise RuntimeError(f"unexpected viewer detach set: {detached}")
                for handle in handles:
                    wait_match(weston, rf"nested-proxy: destroy handle={handle}\b")
                wait_shell(
                    env,
                    lambda text: (
                        '"authorized":true' not in text
                        and all(stream not in text for stream in STREAMS)),
                    "empty remote attachment set after detach")
                (VIEWER_RT / "r8-detached-ready").touch()
                wait_path(VIEWER_RT / "r8-remount")
            else:
                if set(handles).intersection(handles_by_phase[1]):
                    raise RuntimeError(
                        f"remount reused stale proxy handles: {handles_by_phase}")
                if listing.count('"authorized":true') != 2:
                    raise RuntimeError("remount has phantom protected rows")

        created = re.findall(r"nested-proxy: created handle=([0-9]+)",
                             weston.read_text(errors="replace"))
        destroyed = re.findall(r"nested-proxy: destroy handle=([0-9]+)",
                               weston.read_text(errors="replace"))
        if len(created) != 4 or len(destroyed) != 2:
            raise RuntimeError(
                f"phantom proxy lifecycle created={created} destroyed={destroyed}")
        if registry.keys != tuple(("vm-source", stream) for stream in STREAMS):
            raise RuntimeError(f"wrong viewer remount keys: {registry.keys}")
        (VIEWER_RT / "r8-remounted-ready").touch()
        wait_path(VIEWER_RT / "r8-finish")
        return {
            "role": "viewer", "handles_by_phase": handles_by_phase,
            "initial_two_proxies_and_pixels": True,
            "detach_set_exact": True,
            "viewer_attachments_empty_after_detach": True,
            "fresh_generation_identities": True,
            "fresh_two_proxies_and_pixels": True,
            "remount_registry_exact": True,
            "no_phantom_duplicates": True,
            "protected_badges_exact_after_remount": True,
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
