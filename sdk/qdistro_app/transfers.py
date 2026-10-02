"""Bounded, payload-free App1 transfer receipts.

Staging is an obligation owned by the application, not proof of application.
Only its completion callback may report editor insertion (``applied``).
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TRANSFER_VERSION = 1
MAX_TRANSFER_BYTES = 1024 * 1024
MAX_RECEIPTS = 256
RECEIPT_TTL_S = 600
TERMINAL_STATES = frozenset({"applied", "declined", "failed", "rejected"})
RECEIPT_STATES = TERMINAL_STATES | {"staged", "unknown"}


def load_metadata(raw: Any) -> Any:
    """Bound metadata before parsing it; never parse transfer payload envelopes."""
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="strict")) > 8192:
        raise ValueError("transfer metadata exceeds the wire bound")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate transfer metadata key")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError("invalid transfer metadata constant")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _token(value: Any, *, empty: bool = False, max_chars: int = 128) -> bool:
    if (not isinstance(value, str) or len(value) > max_chars or (not value and not empty)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return True


def short_reason(value: Any) -> str:
    return str(value or "")[:160]


def unknown_capabilities(reason: str = "receiver capabilities unavailable") -> dict:
    return {"version": 0, "state": "unknown", "reason": short_reason(reason)}


def normalize_capabilities(raw: Any, *, instance_id: str | None = None) -> dict:
    """Validate the external contract; malformed data is never a match."""
    if not isinstance(raw, dict):
        return unknown_capabilities()
    version = raw.get("version", TRANSFER_VERSION if instance_id is not None else None)
    identity = instance_id if instance_id is not None else raw.get("instance_id")
    kinds = raw.get("kinds")
    limit = raw.get("max_bytes")
    if (type(version) is not int or version != TRANSFER_VERSION
            or not _token(identity)
            or not isinstance(kinds, list) or not kinds or len(kinds) > 32
            or any(not _token(kind) or "*" in kind for kind in kinds)
            or type(limit) is not int or not 0 < limit <= MAX_TRANSFER_BYTES
            or raw.get("encoding") != "utf-8"
            or type(raw.get("confirmation_required")) is not bool
            or type(raw.get("available")) is not bool
            or not isinstance(raw.get("reason", ""), str)):
        return unknown_capabilities()
    return {"version": TRANSFER_VERSION, "instance_id": identity,
            "kinds": list(dict.fromkeys(kinds)), "max_bytes": limit,
            "encoding": "utf-8", "confirmation_required": raw["confirmation_required"],
            "available": raw["available"], "reason": short_reason(raw.get("reason", ""))}


def unknown_receipt(instance_id: str = "", transfer_id: str = "",
                    reason: str = "transfer outcome unavailable") -> dict:
    return {"version": TRANSFER_VERSION, "instance_id": instance_id,
            "transfer_id": transfer_id, "state": "unknown", "reason": short_reason(reason)}


def normalize_receipt(raw: Any) -> dict:
    if (not isinstance(raw, dict) or type(raw.get("version")) is not int
            or raw["version"] != TRANSFER_VERSION or not isinstance(raw.get("state"), str)
            or raw["state"] not in RECEIPT_STATES
            or not _token(raw.get("instance_id"), empty=True)
            or not _token(raw.get("transfer_id"), empty=True)
            or not isinstance(raw.get("reason", ""), str)
            or (raw["state"] in {"staged", "applied", "declined", "failed"}
                and (not raw["instance_id"] or not raw["transfer_id"]))):
        return unknown_receipt()
    return {"version": TRANSFER_VERSION, "instance_id": raw["instance_id"],
            "transfer_id": raw["transfer_id"], "state": raw["state"],
            "reason": short_reason(raw.get("reason", ""))}


@dataclass
class _Receipt:
    sender: str
    expires_at: float
    value: dict


class TransferController:
    """Thread-safe admission and receipts, independent of D-Bus/Qt dispatch."""

    def __init__(self, capabilities: Callable[[], dict] | None = None,
                 on_transfer: Callable | None = None, *,
                 clock: Callable[[], float] = time.monotonic):
        self.instance_id = secrets.token_hex(16)
        self._capabilities = capabilities
        self._on_transfer = on_transfer
        self._clock = clock
        self._lock = threading.Lock()
        self._receipts: OrderedDict[str, _Receipt] = OrderedDict()

    def capabilities(self) -> dict:
        if self._capabilities is None or self._on_transfer is None:
            return unknown_capabilities("receiver does not support transfer receipts")
        try:
            return normalize_capabilities(self._capabilities(), instance_id=self.instance_id)
        except Exception:  # noqa: BLE001 — callback details may contain payloads
            return unknown_capabilities("receiver capabilities unavailable")

    def _reply(self, state: str, reason: str, transfer_id: str = "") -> dict:
        return {"version": TRANSFER_VERSION, "instance_id": self.instance_id,
                "transfer_id": transfer_id, "state": state, "reason": short_reason(reason)}

    def _prune(self, now: float) -> None:
        # Expired staged work still belongs to the application. Keep capacity
        # reserved until completion, even though queries now report unknown.
        for transfer_id, record in list(self._receipts.items()):
            if record.expires_at <= now and record.value["state"] in TERMINAL_STATES:
                del self._receipts[transfer_id]

    def receive(self, expected_instance: str, kind: str, payload: str, sender: str) -> dict:
        if expected_instance != self.instance_id:
            return self._reply("unknown", "receiver instance changed")
        if not _token(sender, max_chars=255) or not sender.startswith(":"):
            return self._reply("rejected", "authenticated sender required")
        capabilities = self.capabilities()
        if capabilities["version"] != TRANSFER_VERSION:
            return self._reply("unknown", "receiver capabilities unavailable")
        if not capabilities["available"]:
            return self._reply("rejected", capabilities["reason"] or "receiver unavailable")
        if kind not in capabilities["kinds"]:
            return self._reply("rejected", "unsupported content kind")
        try:
            size = len(payload.encode("utf-8", errors="strict"))
        except UnicodeError:
            return self._reply("rejected", "payload is not valid UTF-8")
        if "\x00" in payload:
            return self._reply("rejected", "payload contains NUL")
        if size > capabilities["max_bytes"]:
            return self._reply("rejected", "payload exceeds receiver byte limit")
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._receipts) >= MAX_RECEIPTS:
                terminal = next((key for key, row in self._receipts.items()
                                 if row.value["state"] in TERMINAL_STATES), None)
                if terminal is None:
                    return self._reply("rejected", "receiver receipt capacity exhausted")
                del self._receipts[terminal]
            transfer_id = secrets.token_hex(16)
            record = _Receipt(sender, now + RECEIPT_TTL_S,
                              self._reply("staged", "", transfer_id))
            self._receipts[transfer_id] = record

        def complete(state: str, reason: str = "") -> bool:
            if not isinstance(state, str) or state not in {"applied", "declined", "failed"}:
                raise ValueError("completion state must be applied, declined or failed")
            with self._lock:
                if record.value["state"] != "staged":
                    return False
                record.value = self._reply(state, reason, transfer_id)
                return True

        # Invoke outside the lock: completion may run synchronously or on Qt's
        # event thread. A callback return must never overwrite that completion.
        try:
            result = self._on_transfer(kind, payload, complete)
        except Exception:  # noqa: BLE001
            complete("failed", "receiver callback failed")
        else:
            with self._lock:
                if record.value["state"] == "staged":
                    if (isinstance(result, dict) and isinstance(result.get("state"), str)
                            and result["state"] in {"staged", "rejected"}):
                        record.value = self._reply(result["state"], result.get("reason", ""), transfer_id)
                    else:
                        record.value = self._reply("failed", "invalid receiver admission result", transfer_id)
        with self._lock:
            return dict(record.value)

    def status(self, expected_instance: str, transfer_id: str, sender: str) -> dict:
        with self._lock:
            now = self._clock()
            self._prune(now)
            record = self._receipts.get(transfer_id)
            if (expected_instance != self.instance_id or record is None
                    or record.sender != sender or record.expires_at <= now):
                return unknown_receipt(self.instance_id, transfer_id)
            return dict(record.value)
