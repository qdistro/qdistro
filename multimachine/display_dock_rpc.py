"""Exact Unix RPC adapters below the display dock-session state machine.

These adapters are deliberately role-specific.  The dock daemon chooses fixed
peer-panel and carrier-supervisor sockets; callers cannot supply a socket or
reinterpret an action in an attach request.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
from collections.abc import Mapping
from pathlib import Path

from .remote_display_slot import ActionKind, SlotAction


SCHEMA = "qdistro-mm-display-dock-endpoint-v1"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 64 * 1024
_REQUEST_FIELDS = frozenset({
    "schema", "role", "action", "generation", "session_id", "slot_name",
    "releases",
})


class DisplayDockRpcError(RuntimeError):
    """A fixed endpoint did not complete the exact requested transition."""


def peer_uid(client: socket.socket) -> int:
    credentials = client.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def prepare_endpoint_listener(path: str, *, uid: int,
                              gid: int) -> socket.socket:
    """Bind a fixed role socket without replacing another live/stale owner."""
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in (uid, gid)):
        raise ValueError("display dock endpoint owner is invalid")
    endpoint = Path(validate_endpoint_path(path))
    parent = endpoint.parent
    parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    info = parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022):
        raise DisplayDockRpcError("display dock endpoint directory is unsafe")
    if endpoint.exists() or endpoint.is_symlink():
        raise DisplayDockRpcError("display dock endpoint socket already exists")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
        os.chown(path, uid, gid)
        os.chmod(path, 0o660)
        listener.listen(8)
        return listener
    except Exception:
        listener.close()
        endpoint.unlink(missing_ok=True)
        raise


def receive_endpoint_request(client: socket.socket, *, role: str,
                             generation: int, session_id: str,
                             slot_name: str,
                             actions: frozenset[str]) -> str:
    """Parse one exact request and bind it to this sealed generation."""
    data = bytearray()
    while b"\n" not in data and len(data) <= MAX_REQUEST_BYTES:
        chunk = client.recv(min(4096, MAX_REQUEST_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if b"\n" not in data or len(data) > MAX_REQUEST_BYTES:
        raise DisplayDockRpcError("display dock endpoint request is incomplete")
    try:
        request = json.loads(bytes(data.split(b"\n", 1)[0]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DisplayDockRpcError(
            "display dock endpoint request is not JSON") from exc
    if (not isinstance(request, dict) or set(request) != _REQUEST_FIELDS
            or request.get("schema") != SCHEMA
            or request.get("role") != role
            or request.get("generation") != generation
            or request.get("session_id") != session_id
            or request.get("slot_name") != slot_name
            or request.get("action") not in actions
            or request.get("releases") != []):
        raise DisplayDockRpcError(
            "display dock endpoint request does not match sealed authority")
    return request["action"]


def send_endpoint_response(client: socket.socket, *, ok: bool,
                           result: str) -> None:
    if not isinstance(result, str) or len(result) > 128:
        raise ValueError("display dock endpoint result is invalid")
    client.sendall((json.dumps(
        {"ok": ok, "result": result}, sort_keys=True,
        separators=(",", ":")) + "\n").encode())


def validate_endpoint_path(path: str, *, prefix: str = "/run/qdistro/") -> str:
    if (not isinstance(path, str) or not path.startswith(prefix)
            or len(path.encode()) > 107 or not path.endswith(".sock")
            or "/../" in path or path.endswith("/..")):
        raise ValueError("display dock endpoint socket path is invalid")
    return path


class JsonEndpointClient:
    """Bounded one-request client for a preconfigured privileged endpoint."""

    def __init__(self, path: str, *, role: str, timeout: float = 10.0,
                 path_prefix: str = "/run/qdistro/"):
        self.path = validate_endpoint_path(path, prefix=path_prefix)
        if role not in {"peer-panel", "carrier"}:
            raise ValueError("display dock endpoint role is invalid")
        if timeout <= 0 or timeout > 30:
            raise ValueError("display dock endpoint timeout is invalid")
        self.role = role
        self.timeout = timeout

    def request(self, action: str, grant: Mapping) -> str:
        request = {
            "schema": SCHEMA,
            "role": self.role,
            "action": action,
            "generation": grant["generation"],
            "session_id": grant["session_id"],
            "slot_name": grant["slot_name"],
            "releases": [],
        }
        encoded = (json.dumps(
            request, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(self.path)
            client.sendall(encoded)
            data = bytearray()
            while b"\n" not in data and len(data) <= MAX_RESPONSE_BYTES:
                chunk = client.recv(
                    min(4096, MAX_RESPONSE_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        if b"\n" not in data or len(data) > MAX_RESPONSE_BYTES:
            raise DisplayDockRpcError(
                f"{self.role} endpoint response is incomplete or oversized")
        try:
            response = json.loads(bytes(data.split(b"\n", 1)[0]))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DisplayDockRpcError(
                f"{self.role} endpoint response is not JSON") from exc
        if (not isinstance(response, dict)
                or set(response) != {"ok", "result"}
                or not isinstance(response["ok"], bool)
                or not isinstance(response["result"], str)
                or len(response["result"]) > 128):
            raise DisplayDockRpcError(
                f"{self.role} endpoint response is invalid")
        if not response["ok"]:
            raise DisplayDockRpcError(
                f"{self.role} endpoint rejected {action}: {response['result']}")
        return response["result"]


class RpcPanelEndpoint:
    def __init__(self, client: JsonEndpointClient):
        if client.role != "peer-panel":
            raise ValueError("panel RPC has the wrong endpoint role")
        self.client = client
        self._grant: dict | None = None

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        expected = {
            ActionKind.PEER_BLANK_PANEL: "reserved",
            ActionKind.PEER_UNBLANK_PANEL: "restored",
        }.get(action.kind)
        if expected is None:
            raise DisplayDockRpcError("panel RPC received a non-panel action")
        result = self.client.request(action.kind.value, grant)
        if result != expected:
            raise DisplayDockRpcError(
                f"panel {action.kind.value} returned {result}")
        self._grant = dict(grant)

    def heartbeat(self, generation: int, grant: Mapping) -> None:
        if generation != grant["generation"]:
            raise DisplayDockRpcError("panel heartbeat generation is stale")
        if self.client.request("heartbeat", grant) != "renewed":
            raise DisplayDockRpcError("panel heartbeat was not renewed")

    def safe_state_confirmed(self, _slot_name: str) -> bool:
        if self._grant is None:
            return True
        return self.client.request("safe", self._grant) == "safe"


class RpcCarrierSession:
    def __init__(self, client: JsonEndpointClient, grant: Mapping):
        self.client = client
        self.grant = dict(grant)
        self.closed = False
        self.open_result = self.client.request("open", self.grant)

    def ready(self) -> bool:
        return not self.closed and self.open_result == "ready"

    def alive(self) -> bool:
        return (not self.closed
                and self.client.request("alive", self.grant) == "alive")

    def close(self) -> None:
        if self.closed:
            return
        try:
            result = self.client.request("close", self.grant)
            if result != "closed":
                raise DisplayDockRpcError(
                    f"carrier close returned {result}")
        finally:
            self.closed = True


class RpcCarrierFactory:
    def __init__(self, client: JsonEndpointClient):
        if client.role != "carrier":
            raise ValueError("carrier RPC has the wrong endpoint role")
        self.client = client
        self._last_grant: dict | None = None

    def start(self, grant: Mapping) -> RpcCarrierSession:
        self._last_grant = dict(grant)
        return RpcCarrierSession(self.client, grant)

    def safe(self, _slot_name: str) -> bool:
        if self._last_grant is None:
            return True
        return self.client.request("safe", self._last_grant) == "safe"
