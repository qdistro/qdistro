"""R7 multi-stream registry over isolated R6 nested supervisors."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable

from .remote_nested_supervisor import SupervisorPrograms


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_REMOTE_STREAMS = 64


@dataclass(frozen=True)
class RemoteNestedStreamSpec:
    """Authority and local-boundary descriptors for one remote toplevel."""

    origin_machine_id: str
    stream_id: str
    generation: int
    host: str
    port: int
    grant_fd: int
    secret_fd: int
    tls_cert_fd: int
    tls_key_fd: int
    peer_cert_fd: int
    source_config_fd: int | None = None
    source_close_fd: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.origin_machine_id, self.stream_id

    @property
    def owned_fds(self) -> tuple[int, ...]:
        authority = (
            self.grant_fd, self.secret_fd, self.tls_cert_fd,
            self.tls_key_fd, self.peer_cert_fd,
        )
        source = (() if self.source_config_fd is None else
                  (self.source_config_fd, self.source_close_fd))
        return authority + source


@dataclass(frozen=True)
class RegistryPrograms:
    supervisor: str = "/usr/local/bin/qdistro-mm-remote-nested-session"
    children: SupervisorPrograms = SupervisorPrograms()


@dataclass(frozen=True)
class StreamExit:
    origin_machine_id: str
    stream_id: str
    generation: int
    returncode: int

    @property
    def failed(self) -> bool:
        return self.returncode != 0


@dataclass
class _RunningStream:
    spec: RemoteNestedStreamSpec
    process: subprocess.Popen


ProcessFactory = Callable[..., subprocess.Popen]


class RemoteNestedRegistry:
    """Own N independent one-stream supervisors for one dock session.

    Each child supervisor retains R6's fail-closed endpoint/controller/helper
    lifetime.  The registry deliberately does not merge those process trees:
    one stream exit becomes one :class:`StreamExit` and leaves every sibling
    running.
    """

    def __init__(self, *, role: str, local_machine_id: str, session_id: str,
                 programs: RegistryPrograms = RegistryPrograms(),
                 process_factory: ProcessFactory = subprocess.Popen):
        if role not in {"source", "viewer"}:
            raise ValueError("nested registry role is invalid")
        for label, value in (("local machine", local_machine_id),
                             ("session", session_id)):
            if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
                raise ValueError(f"nested registry {label} id is invalid")
        self.role = role
        self.local_machine_id = local_machine_id
        self.session_id = session_id
        self.programs = programs
        self._process_factory = process_factory
        self._running: dict[tuple[str, str], _RunningStream] = {}
        # Survives detach/poll within this dock-session owner. A remount is a
        # new authority generation; accepting the same/older generation after
        # its process tree disappeared would let a cached grant resurrect a
        # stale attachment. Transport reconnects within one generation remain
        # inside the R6 supervisor and do not pass through this registry seam.
        self._last_generation: dict[tuple[str, str], int] = {}

    @property
    def keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._running)

    def _validate_spec(self, spec: RemoteNestedStreamSpec) -> None:
        if not isinstance(spec, RemoteNestedStreamSpec):
            raise ValueError("nested stream spec is invalid")
        if not _IDENTITY.fullmatch(spec.origin_machine_id):
            raise ValueError("nested stream origin id is invalid")
        if not _IDENTITY.fullmatch(spec.stream_id):
            raise ValueError("nested stream id is invalid")
        if (not isinstance(spec.generation, int)
                or isinstance(spec.generation, bool)
                or spec.generation <= 0):
            raise ValueError("nested stream generation is invalid")
        if (not isinstance(spec.host, str) or not spec.host
                or len(spec.host) > 255 or any(ord(c) < 33 for c in spec.host)):
            raise ValueError("nested stream host is invalid")
        if (not isinstance(spec.port, int) or isinstance(spec.port, bool)
                or not 1 <= spec.port <= 65535):
            raise ValueError("nested stream port is invalid")
        fds = spec.owned_fds
        if (any(not isinstance(fd, int) or isinstance(fd, bool) or fd < 0
                for fd in fds) or len(fds) != len(set(fds))):
            raise ValueError("nested stream fds must be valid and distinct")
        if self.role == "source":
            if spec.source_config_fd is None or spec.source_close_fd is None:
                raise ValueError("source nested stream requires local fds")
        elif spec.source_config_fd is not None or spec.source_close_fd is not None:
            raise ValueError("viewer nested stream cannot receive source fds")

    @staticmethod
    def _close_fds(fds: Iterable[int]) -> None:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def _argv(self, spec: RemoteNestedStreamSpec) -> list[str]:
        child = self.programs.children
        argv = [
            self.programs.supervisor,
            "--role", self.role,
            "--grant-fd", str(spec.grant_fd),
            "--secret-fd", str(spec.secret_fd),
            "--tls-cert-fd", str(spec.tls_cert_fd),
            "--tls-key-fd", str(spec.tls_key_fd),
            "--peer-cert-fd", str(spec.peer_cert_fd),
            "--local-machine-id", self.local_machine_id,
            "--session-id", self.session_id,
            "--stream-id", spec.stream_id,
            "--host", spec.host,
            "--port", str(spec.port),
            "--session-launcher-program", child.session_launcher,
            "--endpoint-program", child.endpoint,
            "--controller-program", child.controller,
            "--source-helper-program", child.source_helper,
            "--viewer-helper-program", child.viewer_helper,
        ]
        if self.role == "source":
            assert spec.source_config_fd is not None
            assert spec.source_close_fd is not None
            argv.extend([
                "--source-config-fd", str(spec.source_config_fd),
                "--source-close-fd", str(spec.source_close_fd),
            ])
        return argv

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def add_many(self, specs: Iterable[RemoteNestedStreamSpec]) -> None:
        pending = tuple(specs)
        if not pending:
            return
        if len(self._running) + len(pending) > MAX_REMOTE_STREAMS:
            raise ValueError("nested stream limit exceeded")

        keys: set[tuple[str, str]] = set()
        fds: set[int] = set()
        try:
            for spec in pending:
                self._validate_spec(spec)
                if spec.key in self._running or spec.key in keys:
                    raise ValueError("duplicate nested stream identity")
                previous = self._last_generation.get(spec.key)
                if previous is not None and spec.generation <= previous:
                    raise ValueError(
                        "nested stream remount generation must increase")
                if fds.intersection(spec.owned_fds):
                    raise ValueError("nested streams cannot share inherited fds")
                keys.add(spec.key)
                fds.update(spec.owned_fds)
        except BaseException:
            for spec in pending:
                if isinstance(spec, RemoteNestedStreamSpec):
                    self._close_fds(spec.owned_fds)
            raise

        started: list[tuple[RemoteNestedStreamSpec, subprocess.Popen]] = []
        try:
            for spec in pending:
                process = self._process_factory(
                    self._argv(spec), pass_fds=spec.owned_fds,
                    close_fds=True)
                started.append((spec, process))
                self._close_fds(spec.owned_fds)
            for spec, process in started:
                self._running[spec.key] = _RunningStream(spec, process)
                self._last_generation[spec.key] = spec.generation
        except BaseException:
            started_keys = {spec.key for spec, _process in started}
            for spec in pending:
                if spec.key not in started_keys:
                    self._close_fds(spec.owned_fds)
            for _spec, process in started:
                self._stop_process(process)
            raise

    def add(self, spec: RemoteNestedStreamSpec) -> None:
        self.add_many((spec,))

    def poll(self) -> tuple[StreamExit, ...]:
        exits: list[StreamExit] = []
        for key, running in tuple(self._running.items()):
            returncode = running.process.poll()
            if returncode is None:
                continue
            self._running.pop(key)
            exits.append(StreamExit(
                origin_machine_id=running.spec.origin_machine_id,
                stream_id=running.spec.stream_id,
                generation=running.spec.generation,
                returncode=returncode))
        return tuple(exits)

    def remove(self, origin_machine_id: str, stream_id: str) -> bool:
        running = self._running.pop((origin_machine_id, stream_id), None)
        if running is None:
            return False
        self._stop_process(running.process)
        return True

    def detach_all(self) -> tuple[tuple[str, str], ...]:
        """Atomically remove every shared-GUI attachment.

        The active map is cleared before child termination begins, so callers
        and concurrent status polling can never observe a half-detached set.
        Source applications are not children of these supervisors and remain
        clients of their local qdwin. Returned keys are the exact attachment
        set which was revoked; a later add requires a newer generation.
        """
        running = tuple(self._running.values())
        self._running.clear()
        for stream in running:
            self._stop_process(stream.process)
        return tuple(stream.spec.key for stream in running)

    def stop_all(self) -> None:
        self.detach_all()

    def __enter__(self) -> "RemoteNestedRegistry":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop_all()
