"""Fail-closed process supervisor for one R6 nested remote stream."""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class SupervisorPrograms:
    session_launcher: str = "/usr/local/bin/qdistro-mm-remote-session-launcher"
    endpoint: str = "/usr/local/bin/qdistro-mm-remote-adapter"
    controller: str = "/usr/local/bin/qdistro-mm-remote-nested-controller"
    source_helper: str = "/usr/bin/qdistro-mm-remote-source-helper"
    viewer_helper: str = "/usr/bin/qdistro-mm-remote-viewer-helper"


class RemoteNestedSupervisor:
    """Own endpoint, controller and local helper as one lifetime unit."""

    def __init__(self, *, role: str, grant_fd: int, secret_fd: int,
                 tls_cert_fd: int, tls_key_fd: int, peer_cert_fd: int,
                 local_machine_id: str, session_id: str, stream_id: str,
                 host: str, port: int, source_config_fd: int | None = None,
                 source_close_fd: int | None = None,
                 programs: SupervisorPrograms = SupervisorPrograms()):
        if role not in {"source", "viewer"}:
            raise ValueError("nested supervisor role is invalid")
        inherited = [grant_fd, secret_fd, tls_cert_fd, tls_key_fd, peer_cert_fd]
        if any(not isinstance(fd, int) or isinstance(fd, bool) or fd < 0
               for fd in inherited) or len(set(inherited)) != len(inherited):
            raise ValueError("nested supervisor inherited fds must be distinct")
        if role == "source":
            if (not isinstance(source_config_fd, int)
                    or not isinstance(source_close_fd, int)
                    or source_config_fd < 0 or source_close_fd < 0
                    or source_config_fd in inherited
                    or source_close_fd in inherited
                    or source_config_fd == source_close_fd):
                raise ValueError("source supervisor fds are invalid")
        elif source_config_fd is not None or source_close_fd is not None:
            raise ValueError("viewer supervisor cannot receive source fds")
        if (not isinstance(port, int) or isinstance(port, bool)
                or port <= 0 or port > 65535):
            raise ValueError("nested supervisor port is invalid")
        self.role = role
        self.authority_fds = tuple(inherited)
        self.local_machine_id = local_machine_id
        self.session_id = session_id
        self.stream_id = stream_id
        self.host = host
        self.port = port
        self.source_config_fd = source_config_fd
        self.source_close_fd = source_close_fd
        self.programs = programs
        self._children: dict[int, subprocess.Popen] = {}
        self._stopping = False

    @property
    def owned_fds(self) -> tuple[int, ...]:
        source = (() if self.role == "viewer" else
                  (self.source_config_fd, self.source_close_fd))
        return self.authority_fds + source

    def _spawn(self, argv: list[str], pass_fds: tuple[int, ...]) -> None:
        process = subprocess.Popen(argv, pass_fds=pass_fds, close_fds=True)
        self._children[process.pid] = process

    def _launch(self) -> None:
        endpoint_side, controller_endpoint = socket.socketpair()
        controller_helper, helper_side = socket.socketpair()
        sockets = (endpoint_side, controller_endpoint, controller_helper, helper_side)
        try:
            helper_fd = helper_side.fileno()
            if self.role == "source":
                assert self.source_config_fd is not None
                assert self.source_close_fd is not None
                helper_argv = [
                    self.programs.source_helper,
                    "--controller-fd", str(helper_fd),
                    "--config-fd", str(self.source_config_fd),
                    "--close-fd", str(self.source_close_fd),
                ]
                helper_fds = (
                    helper_fd, self.source_config_fd, self.source_close_fd)
            else:
                helper_argv = [
                    self.programs.viewer_helper,
                    "--controller-fd", str(helper_fd),
                ]
                helper_fds = (helper_fd,)
            self._spawn(helper_argv, helper_fds)

            controller_endpoint_fd = controller_endpoint.fileno()
            controller_helper_fd = controller_helper.fileno()
            self._spawn([
                self.programs.controller,
                "--role", self.role,
                "--endpoint-fd", str(controller_endpoint_fd),
                "--helper-fd", str(controller_helper_fd),
            ], (controller_endpoint_fd, controller_helper_fd))

            endpoint_fd = endpoint_side.fileno()
            grant_fd, secret_fd, cert_fd, key_fd, peer_fd = self.authority_fds
            self._spawn([
                self.programs.session_launcher,
                "--grant-fd", str(grant_fd),
                "--secret-fd", str(secret_fd),
                "--tls-cert-fd", str(cert_fd),
                "--tls-key-fd", str(key_fd),
                "--peer-cert-fd", str(peer_fd),
                "--local-fd", str(endpoint_fd),
                "--role", self.role,
                "--local-machine-id", self.local_machine_id,
                "--session-id", self.session_id,
                "--stream-id", self.stream_id,
                "--host", self.host,
                "--port", str(self.port),
                "--endpoint-program", self.programs.endpoint,
            ], self.authority_fds + (endpoint_fd,))
        finally:
            for peer in sockets:
                peer.close()
            self._close_owned_fds()

    def _close_owned_fds(self) -> None:
        for fd in self.owned_fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def _signal_children(self, sig: int) -> None:
        for pid in tuple(self._children):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass

    def _reap_after_stop(self) -> None:
        self._signal_children(signal.SIGTERM)
        deadline = time.monotonic() + 3
        while self._children and time.monotonic() < deadline:
            for pid in tuple(self._children):
                waited, _ = os.waitpid(pid, os.WNOHANG)
                if waited:
                    self._children.pop(pid, None)
            if self._children:
                time.sleep(0.02)
        self._signal_children(signal.SIGKILL)
        for pid in tuple(self._children):
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            self._children.pop(pid, None)

    def stop(self) -> None:
        self._stopping = True
        self._signal_children(signal.SIGTERM)

    def run(self) -> int:
        try:
            self._launch()
            while self._children:
                try:
                    pid, status = os.waitpid(-1, 0)
                except InterruptedError:
                    if self._stopping:
                        break
                    continue
                if pid not in self._children:
                    continue
                self._children.pop(pid)
                result = os.waitstatus_to_exitcode(status)
                self._reap_after_stop()
                return result
            self._reap_after_stop()
            return 128 + signal.SIGTERM if self._stopping else 1
        except BaseException:
            self._reap_after_stop()
            self._close_owned_fds()
            raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qdistro-mm-remote-nested-session")
    ap.add_argument("--role", choices=("source", "viewer"), required=True)
    ap.add_argument("--grant-fd", type=int, required=True)
    ap.add_argument("--secret-fd", type=int, required=True)
    ap.add_argument("--tls-cert-fd", type=int, required=True)
    ap.add_argument("--tls-key-fd", type=int, required=True)
    ap.add_argument("--peer-cert-fd", type=int, required=True)
    ap.add_argument("--source-config-fd", type=int)
    ap.add_argument("--source-close-fd", type=int)
    ap.add_argument("--local-machine-id", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--stream-id", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--session-launcher-program",
                    default=SupervisorPrograms.session_launcher)
    ap.add_argument("--endpoint-program", default=SupervisorPrograms.endpoint)
    ap.add_argument("--controller-program", default=SupervisorPrograms.controller)
    ap.add_argument("--source-helper-program",
                    default=SupervisorPrograms.source_helper)
    ap.add_argument("--viewer-helper-program",
                    default=SupervisorPrograms.viewer_helper)
    args = ap.parse_args(argv)
    programs = SupervisorPrograms(
        session_launcher=args.session_launcher_program,
        endpoint=args.endpoint_program,
        controller=args.controller_program,
        source_helper=args.source_helper_program,
        viewer_helper=args.viewer_helper_program)
    supervisor = RemoteNestedSupervisor(
        role=args.role, grant_fd=args.grant_fd, secret_fd=args.secret_fd,
        tls_cert_fd=args.tls_cert_fd, tls_key_fd=args.tls_key_fd,
        peer_cert_fd=args.peer_cert_fd,
        source_config_fd=args.source_config_fd,
        source_close_fd=args.source_close_fd,
        local_machine_id=args.local_machine_id, session_id=args.session_id,
        stream_id=args.stream_id, host=args.host, port=args.port,
        programs=programs)
    previous_term = signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
    previous_int = signal.signal(signal.SIGINT, lambda *_: supervisor.stop())
    try:
        return supervisor.run()
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
