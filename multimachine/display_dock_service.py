"""Durable control boundary for the primary display dock-session owner.

This module intentionally does not expose individual display actions.  A
trusted local controller may submit one authority receipt, detach its exact
generation, reset an independently-confirmed failed-safe state, inspect public
status, or request shutdown.  The wrapped :class:`DisplayDockSession` remains
the only component allowed to order qdshell, input, carrier, and panel actions.

The socket server is small enough to test without systemd.  Concrete endpoint
wiring belongs in the installed daemon entry point; keeping the protocol and
durability rules here prevents that deployment code from becoming a second
state machine.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import socket
import stat
import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .display_dock_session import DockSessionStatus
from .remote_display_slot import ActiveDisplayGrant, DisplaySlotError


REQUEST_SCHEMA = "qdistro-mm-display-dock-control-v1"
STATUS_SCHEMA = "qdistro-mm-display-dock-status-v1"
MAX_CONTROL_BYTES = 256 * 1024
MAX_REQUEST_IDS = 4096
_REQUEST_ID_BYTES = 16
_COMMAND_FIELDS = {
    "attach": frozenset({
        "schema", "request_id", "command", "receipt", "session_id",
        "carrier_secret",
    }),
    "detach": frozenset({
        "schema", "request_id", "command", "generation",
    }),
    "reset": frozenset({"schema", "request_id", "command"}),
    "status": frozenset({"schema", "request_id", "command"}),
    "shutdown": frozenset({"schema", "request_id", "command"}),
}
_STATUS_FIELDS = frozenset({
    "schema", "slot_name", "phase", "generation", "session_id",
    "next_heartbeat", "accepting", "stop_requested", "last_error",
    "updated_at", "recovery_grant",
})
_PHASES = frozenset({
    "disabled", "enabling", "active", "draining", "failed-safe",
})


class DockServiceError(RuntimeError):
    """A control request or durable service transition failed closed."""


class DockOwner(Protocol):
    def status(self) -> DockSessionStatus: ...
    def attach(self, verified_payload: Mapping) -> None: ...
    def detach(self, generation: int) -> None: ...
    def reset_failed_safe(self) -> None: ...
    def restore_last_generation(self, generation: int) -> None: ...
    def poll(self) -> bool: ...
    def shutdown(self) -> None: ...


VerifyAttach = Callable[[object, str, bytes], Mapping]
RecoverStatus = Callable[[Mapping | None], bool]


def _request_id(value: object) -> str:
    if (not isinstance(value, str) or len(value) != _REQUEST_ID_BYTES * 2
            or any(char not in "0123456789abcdef" for char in value)):
        raise DockServiceError("display dock request id is invalid")
    return value


def _positive_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DockServiceError("display dock generation is invalid")
    return value


def _session_id(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128
            or not value[0].isalnum()
            or any(not (char.isalnum() or char in "._:-") for char in value)):
        raise DockServiceError("display dock session id is invalid")
    return value


def _secret(value: object) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise DockServiceError("display dock carrier secret is invalid")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DockServiceError(
            "display dock carrier secret is invalid") from exc
    if len(raw) != 32:
        raise DockServiceError("display dock carrier secret is invalid")
    return raw


def encode_secret(secret: bytes) -> str:
    """Encode a 32-byte handoff secret for one root-only control request."""
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise ValueError("display dock carrier secret must be 32 bytes")
    return base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")


class SecureStatusStore:
    """Atomic, owner-only durable public status (never receipt/key material)."""

    def __init__(self, path: Path, *, owner_uid: int | None = None):
        self.path = path
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid

    def _validate_parent(self) -> None:
        info = self.path.parent.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid
                or info.st_mode & 0o022):
            raise DockServiceError("display dock state directory is unsafe")

    def load(self) -> dict | None:
        self._validate_parent()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                or info.st_mode & 0o077):
            raise DockServiceError("display dock state file is unsafe")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DockServiceError("display dock state file is invalid") from exc
        if (not isinstance(value, dict) or set(value) != _STATUS_FIELDS
                or value.get("schema") != STATUS_SCHEMA
                or value.get("phase") not in _PHASES
                or not isinstance(value.get("generation"), int)
                or isinstance(value.get("generation"), bool)
                or value["generation"] < 0):
            raise DockServiceError("display dock state schema is invalid")
        recovery_grant = value["recovery_grant"]
        if recovery_grant is not None:
            try:
                grant = ActiveDisplayGrant.from_verified_payload(recovery_grant)
            except (DisplaySlotError, TypeError, ValueError) as exc:
                raise DockServiceError(
                    "display dock recovery grant is invalid") from exc
            if (grant.generation != value["generation"]
                    or grant.session_id != value["session_id"]
                    or value["phase"] == "disabled"):
                raise DockServiceError(
                    "display dock recovery grant does not match status")
        elif value["phase"] != "disabled":
            raise DockServiceError(
                "display dock unsafe status lacks recovery grant")
        return value

    def save(self, value: Mapping) -> None:
        self._validate_parent()
        encoded = (json.dumps(
            dict(value), sort_keys=True, separators=(",", ":")) + "\n").encode()
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{secrets.token_hex(8)}")
        fd = -1
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.write(fd, encoded)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary.unlink(missing_ok=True)


class DisplayDockServiceCore:
    """Serialize authenticated lifecycle requests around one dock owner."""

    def __init__(self, *, owner: DockOwner, controller_uid: int,
                 verify_attach: VerifyAttach, store: SecureStatusStore,
                 recover: RecoverStatus, clock: Callable[[], float] = time.time):
        if (not isinstance(controller_uid, int) or isinstance(controller_uid, bool)
                or controller_uid < 0):
            raise ValueError("display dock controller uid is invalid")
        self.owner = owner
        self.controller_uid = controller_uid
        self.verify_attach = verify_attach
        self.store = store
        self.recover = recover
        self.clock = clock
        self._lock = threading.RLock()
        self._consumed_order: deque[str] = deque()
        self._consumed_ids: set[str] = set()
        self._last_error: str | None = None
        self._recovery_grant: dict | None = None
        self._accepting = False
        self.stop_requested = False

    def _consume(self, request_id: str) -> None:
        if request_id in self._consumed_ids:
            return
        self._consumed_ids.add(request_id)
        self._consumed_order.append(request_id)
        while len(self._consumed_order) > MAX_REQUEST_IDS:
            self._consumed_ids.remove(self._consumed_order.popleft())

    def public_status(self) -> dict:
        status = self.owner.status()
        return {
            "schema": STATUS_SCHEMA,
            **asdict(status),
            "accepting": self._accepting,
            "stop_requested": self.stop_requested,
            "last_error": self._last_error,
            "recovery_grant": self._recovery_grant,
            "updated_at": self.clock(),
        }

    def _persist(self) -> None:
        self.store.save(self.public_status())

    def start(self) -> None:
        """Recover any prior non-disabled record before accepting commands."""
        with self._lock:
            previous = self.store.load()
            # Reconcile even without a prior status file. A missing file must
            # not be interpreted as proof that qdwin output/input and the peer
            # panel are safe after an unclean upgrade or manual deletion.
            if self.recover(previous) is not True:
                self._last_error = "startup-recovery-unconfirmed"
                raise DockServiceError(
                    "display dock startup recovery was not confirmed")
            if self.owner.status().phase != "disabled":
                self._last_error = "startup-recovery-not-disabled"
                raise DockServiceError(
                    "display dock startup recovery did not reach disabled")
            if previous is not None:
                self.owner.restore_last_generation(
                    _positive_generation(previous["generation"])
                    if previous["generation"] else 0)
            self._recovery_grant = None
            self._accepting = True
            self._last_error = None
            self._persist()

    def _validate(self, request: object) -> tuple[str, str, Mapping]:
        if not isinstance(request, Mapping):
            raise DockServiceError("display dock request must be an object")
        command = request.get("command")
        if command not in _COMMAND_FIELDS:
            raise DockServiceError("display dock command is invalid")
        if set(request) != _COMMAND_FIELDS[command]:
            raise DockServiceError("display dock request fields are invalid")
        if request["schema"] != REQUEST_SCHEMA:
            raise DockServiceError("display dock request schema is invalid")
        return command, _request_id(request["request_id"]), request

    def handle(self, request: object, *, peer_uid: int) -> dict:
        with self._lock:
            if peer_uid != self.controller_uid:
                return {"ok": False, "error": "unauthorized-controller"}
            if not self._accepting:
                return {"ok": False, "error": "service-not-accepting"}
            request_id: str | None = None
            try:
                command, request_id, parsed = self._validate(request)
                if request_id in self._consumed_ids:
                    raise DockServiceError("display dock request was replayed")
                if command == "attach":
                    session_id = _session_id(parsed["session_id"])
                    verified = self.verify_attach(
                        parsed["receipt"], session_id,
                        _secret(parsed["carrier_secret"]))
                    self._recovery_grant = {
                        name: verified[name]
                        for name in (
                            "primary_machine", "peer_machine",
                            "trust_domain_id", "generation", "session_id",
                            "slot_name", "logical_x", "logical_y", "width",
                            "height", "scale", "allow_input",
                            "lease_expires_at", "heartbeat_ms",
                        )
                    }
                    self.owner.attach(verified)
                elif command == "detach":
                    self.owner.detach(_positive_generation(parsed["generation"]))
                    self._recovery_grant = None
                elif command == "reset":
                    self.owner.reset_failed_safe()
                    self._recovery_grant = None
                elif command == "shutdown":
                    self._accepting = False
                    self.stop_requested = True
                    self.owner.shutdown()
                    if self.owner.status().phase == "disabled":
                        self._recovery_grant = None
                self._last_error = None
                response = {"ok": True, "status": self.public_status()}
            except Exception as exc:
                if self.owner.status().phase == "disabled":
                    self._recovery_grant = None
                self._last_error = type(exc).__name__
                response = {
                    "ok": False,
                    "error": f"{type(exc).__name__}:{exc}",
                    "status": self.public_status(),
                }
            finally:
                if request_id is not None:
                    self._consume(request_id)
                self._persist()
            return response

    def poll(self) -> bool:
        with self._lock:
            try:
                changed = self.owner.poll()
            except Exception as exc:
                self._last_error = type(exc).__name__
                self._persist()
                raise
            if changed:
                self._persist()
            return changed

    def converge_shutdown(self) -> None:
        """Stop admission and require the owner to reach disabled safely."""
        with self._lock:
            self._accepting = False
            self.stop_requested = True
            try:
                self.owner.shutdown()
                if self.owner.status().phase == "failed-safe":
                    self.owner.reset_failed_safe()
                if self.owner.status().phase != "disabled":
                    raise DockServiceError(
                        "display dock shutdown did not reach disabled")
                self._last_error = None
                self._recovery_grant = None
            except Exception as exc:
                self._last_error = type(exc).__name__
                self._persist()
                raise
            self._persist()


def _peer_uid(client: socket.socket) -> int:
    credentials = client.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def _read_request(client: socket.socket) -> object:
    data = bytearray()
    while b"\n" not in data and len(data) <= MAX_CONTROL_BYTES:
        chunk = client.recv(min(8192, MAX_CONTROL_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if b"\n" not in data or len(data) > MAX_CONTROL_BYTES:
        raise DockServiceError("display dock request is incomplete or oversized")
    try:
        return json.loads(bytes(data.split(b"\n", 1)[0]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockServiceError("display dock request is not JSON") from exc


def serve_control_socket(core: DisplayDockServiceCore, listener: socket.socket,
                         stop: threading.Event, *, poll_interval: float = 0.05,
                         ) -> None:
    """Serve bounded one-request connections and poll lifecycle health."""
    if poll_interval <= 0 or poll_interval > 1:
        raise ValueError("display dock service poll interval is out of bounds")
    listener.settimeout(poll_interval)
    core.start()
    try:
        while not stop.is_set() and not core.stop_requested:
            try:
                client, _address = listener.accept()
            except TimeoutError:
                core.poll()
                continue
            with client:
                try:
                    request = _read_request(client)
                    response = core.handle(request, peer_uid=_peer_uid(client))
                except Exception as exc:
                    response = {"ok": False,
                                "error": f"{type(exc).__name__}:{exc}"}
                client.sendall((json.dumps(
                    response, sort_keys=True, separators=(",", ":"))
                    + "\n").encode())
            core.poll()
    finally:
        core.converge_shutdown()
