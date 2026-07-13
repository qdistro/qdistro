"""Strict local pixel/input contract for the R6 nested remote adapter.

This protocol terminates on each machine.  PipeWire node names, QDNI Unix
paths, Wayland objects, and file descriptors are never represented here or on
the network.  A source capture helper emits one announcement and BGRx packets;
the viewer decoder owns new local buffers and acknowledges only frames it has
accepted.  Input is converted between local QDNI packets and these bounded
semantic events.
"""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from typing import Mapping

from .remote_adapter import CHANNEL_PAYLOAD_LIMITS, RemoteAdapterError


STREAM_SCHEMA = "qdistro-mm-nested-stream-v1"
PIXEL_FORMAT = "BGRx"
MAX_DIMENSION = 8192
MAX_TEXT_BYTES = 4096
FRAME_MAGIC = b"QDMF"
FRAME_VERSION = 1
FRAME_FORMAT_BGRX = 1
FRAME_HEADER = struct.Struct("!4sBBHIIII")
LOCAL_MAGIC = b"QDML"
LOCAL_VERSION = 1
LOCAL_HEADER = struct.Struct("!4sBBHQ")
LOCAL_ANNOUNCE_MAGIC = b"QDMA"
LOCAL_ANNOUNCE_HEADER = struct.Struct("!4sBBHQIIIHH")
LOCAL_IDENTITY_MAGIC = b"QDMI"
LOCAL_IDENTITY_HEADER = struct.Struct("!4sBBQHHH")
SOURCE_CONFIG_MAGIC = b"QDMS"
SOURCE_CONFIG_HEADER = struct.Struct("!4sBBHQHHHH")
LOCAL_INPUT_STRUCTS = {
    "motion": struct.Struct("!ii"),
    "button": struct.Struct("!IB3x"),
    "key": struct.Struct("!IB3x"),
    "axis": struct.Struct("!Ii"),
    "focus": struct.Struct("!B3x"),
}
INPUT_KINDS = frozenset({
    "motion", "button", "key", "axis", "focus",
})
_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_PW_NODE = re.compile(
    r"weston\.pipewire:[1-9][0-9]{0,9}:[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_INPUT_SINK = re.compile(
    r"/.*/qdwin-nested-input-[1-9][0-9]*-[1-9][0-9]*\.sock\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_STREAM_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
LOCAL_MESSAGE_TYPES = {
    "connected": 1,
    "announce": 2,
    "frame": 3,
    "source_closed": 4,
    "decoder_ack": 5,
    "media_ack": 6,
    "motion": 7,
    "button": 8,
    "key": 9,
    "axis": 10,
    "focus": 11,
    "detached": 12,
    "drop_transport": 13,
    "shutdown": 14,
    "close_request": 15,
    "identity": 16,
}
_LOCAL_MESSAGE_NAMES = {
    value: key for key, value in LOCAL_MESSAGE_TYPES.items()
}


def _integer(value: object, *, label: str, minimum: int,
             maximum: int) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < minimum or value > maximum):
        raise RemoteAdapterError(
            f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _text(value: object, *, label: str, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise RemoteAdapterError(f"{label} must be a string")
    encoded = value.encode("utf-8")
    if (not allow_empty and not encoded) or len(encoded) > MAX_TEXT_BYTES:
        raise RemoteAdapterError(f"{label} length is out of bounds")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise RemoteAdapterError(f"{label} contains control characters")
    return value


def _json_object(payload: bytes, *, label: str) -> Mapping:
    if not isinstance(payload, bytes):
        raise RemoteAdapterError(f"{label} must be bytes")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteAdapterError(f"{label} is not JSON") from exc
    if not isinstance(value, Mapping):
        raise RemoteAdapterError(f"{label} must be a JSON object")
    return value


@dataclass(frozen=True)
class StreamAnnouncement:
    source_revision: int
    width: int
    height: int
    stride: int
    app_id: str
    title: str

    def validate(self) -> None:
        _integer(
            self.source_revision, label="source_revision",
            minimum=1, maximum=2**63 - 1)
        width = _integer(
            self.width, label="width", minimum=1, maximum=MAX_DIMENSION)
        height = _integer(
            self.height, label="height", minimum=1, maximum=MAX_DIMENSION)
        stride = _integer(
            self.stride, label="stride", minimum=4,
            maximum=MAX_DIMENSION * 4)
        if stride != width * 4:
            raise RemoteAdapterError("BGRx stride must equal width * 4")
        if (height * stride + FRAME_HEADER.size
                > CHANNEL_PAYLOAD_LIMITS["media"]):
            raise RemoteAdapterError("announced frame exceeds media limit")
        _text(self.app_id, label="app_id", allow_empty=False)
        if _APP_ID.fullmatch(self.app_id) is None:
            raise RemoteAdapterError("app_id has invalid syntax")
        _text(self.title, label="title", allow_empty=True)

    def encode(self) -> bytes:
        self.validate()
        return json.dumps({
            "schema": STREAM_SCHEMA,
            "source_revision": self.source_revision,
            "width": self.width,
            "height": self.height,
            "stride": self.stride,
            "pixel_format": PIXEL_FORMAT,
            "app_id": self.app_id,
            "title": self.title,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def decode(cls, payload: bytes) -> StreamAnnouncement:
        value = _json_object(payload, label="stream announcement")
        fields = {
            "schema", "source_revision", "width", "height", "stride",
            "pixel_format", "app_id", "title",
        }
        if set(value) != fields:
            raise RemoteAdapterError(
                "stream announcement fields do not match schema")
        if value["schema"] != STREAM_SCHEMA:
            raise RemoteAdapterError("unsupported stream announcement schema")
        if value["pixel_format"] != PIXEL_FORMAT:
            raise RemoteAdapterError("unsupported stream pixel format")
        announcement = cls(
            source_revision=value["source_revision"],
            width=value["width"], height=value["height"],
            stride=value["stride"], app_id=value["app_id"],
            title=value["title"])
        announcement.validate()
        return announcement


@dataclass(frozen=True)
class SourceHelperConfig:
    source_revision: int
    pw_node: str
    input_sink: str
    app_id: str
    title: str

    def validate(self) -> None:
        _integer(
            self.source_revision, label="source_revision",
            minimum=1, maximum=2**63 - 1)
        pw_node = _text(self.pw_node, label="pw_node", allow_empty=False)
        if len(pw_node.encode("ascii", errors="ignore")) != len(pw_node):
            raise RemoteAdapterError("pw_node must be ASCII")
        if len(pw_node) > 128 or _PW_NODE.fullmatch(pw_node) is None:
            raise RemoteAdapterError("pw_node has invalid syntax")
        input_sink = _text(
            self.input_sink, label="input_sink", allow_empty=False)
        if (len(input_sink.encode("ascii", errors="ignore")) != len(input_sink)
                or len(input_sink) > 107
                or _INPUT_SINK.fullmatch(input_sink) is None
                or "/../" in input_sink or input_sink.endswith("/..")):
            raise RemoteAdapterError("input_sink has invalid syntax")
        _text(self.app_id, label="app_id", allow_empty=False)
        if _APP_ID.fullmatch(self.app_id) is None:
            raise RemoteAdapterError("app_id has invalid syntax")
        _text(self.title, label="title", allow_empty=True)

    def encode(self) -> bytes:
        self.validate()
        fields = [
            self.pw_node.encode("ascii"), self.input_sink.encode("ascii"),
            self.app_id.encode("utf-8"), self.title.encode("utf-8"),
        ]
        return SOURCE_CONFIG_HEADER.pack(
            SOURCE_CONFIG_MAGIC, LOCAL_VERSION, 0, 0,
            self.source_revision, *(len(field) for field in fields),
        ) + b"".join(fields)

    @classmethod
    def decode(cls, payload: bytes) -> SourceHelperConfig:
        if not isinstance(payload, bytes) or len(payload) < SOURCE_CONFIG_HEADER.size:
            raise RemoteAdapterError("source helper config is truncated")
        magic, version, kind, flags, revision, *lengths = (
            SOURCE_CONFIG_HEADER.unpack(payload[:SOURCE_CONFIG_HEADER.size]))
        if magic != SOURCE_CONFIG_MAGIC or version != LOCAL_VERSION:
            raise RemoteAdapterError("unsupported source helper config")
        if kind != 0 or flags != 0 or sum(lengths) != (
                len(payload) - SOURCE_CONFIG_HEADER.size):
            raise RemoteAdapterError("source helper config header is invalid")
        offset = SOURCE_CONFIG_HEADER.size
        values = []
        try:
            for index, length in enumerate(lengths):
                raw = payload[offset:offset + length]
                values.append(raw.decode("ascii" if index < 2 else "utf-8"))
                offset += length
        except UnicodeDecodeError as exc:
            raise RemoteAdapterError(
                "source helper config text encoding is invalid") from exc
        config = cls(revision, *values)
        config.validate()
        return config


def encode_local_announcement(announcement: StreamAnnouncement) -> bytes:
    """Binary C-helper form of a validated network announcement."""
    announcement.validate()
    app_id = announcement.app_id.encode("utf-8")
    title = announcement.title.encode("utf-8")
    return LOCAL_ANNOUNCE_HEADER.pack(
        LOCAL_ANNOUNCE_MAGIC, LOCAL_VERSION, FRAME_FORMAT_BGRX, 0,
        announcement.source_revision, announcement.width,
        announcement.height, announcement.stride,
        len(app_id), len(title),
    ) + app_id + title


def decode_local_announcement(payload: bytes) -> StreamAnnouncement:
    if (not isinstance(payload, bytes)
            or len(payload) < LOCAL_ANNOUNCE_HEADER.size):
        raise RemoteAdapterError("local announcement is truncated")
    (magic, version, pixel_format, flags, source_revision, width, height,
     stride, app_len, title_len) = LOCAL_ANNOUNCE_HEADER.unpack(
        payload[:LOCAL_ANNOUNCE_HEADER.size])
    if (magic != LOCAL_ANNOUNCE_MAGIC or version != LOCAL_VERSION
            or pixel_format != FRAME_FORMAT_BGRX or flags != 0):
        raise RemoteAdapterError("unsupported local announcement")
    if app_len + title_len != len(payload) - LOCAL_ANNOUNCE_HEADER.size:
        raise RemoteAdapterError("local announcement length does not match header")
    app_start = LOCAL_ANNOUNCE_HEADER.size
    title_start = app_start + app_len
    try:
        app_id = payload[app_start:title_start].decode("utf-8")
        title = payload[title_start:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RemoteAdapterError(
            "local announcement text is not UTF-8") from exc
    announcement = StreamAnnouncement(
        source_revision=source_revision, width=width, height=height,
        stride=stride, app_id=app_id, title=title)
    announcement.validate()
    return announcement


@dataclass(frozen=True)
class RawFrame:
    width: int
    height: int
    stride: int
    pixels: bytes

    def validate(self) -> None:
        width = _integer(
            self.width, label="frame width", minimum=1,
            maximum=MAX_DIMENSION)
        height = _integer(
            self.height, label="frame height", minimum=1,
            maximum=MAX_DIMENSION)
        stride = _integer(
            self.stride, label="frame stride", minimum=4,
            maximum=MAX_DIMENSION * 4)
        if stride != width * 4:
            raise RemoteAdapterError("BGRx frame stride must equal width * 4")
        if not isinstance(self.pixels, bytes) or len(self.pixels) != height * stride:
            raise RemoteAdapterError("BGRx frame payload size does not match geometry")
        if (FRAME_HEADER.size + len(self.pixels)
                > CHANNEL_PAYLOAD_LIMITS["media"]):
            raise RemoteAdapterError("frame packet exceeds media limit")

    def encode(self) -> bytes:
        self.validate()
        return FRAME_HEADER.pack(
            FRAME_MAGIC, FRAME_VERSION, FRAME_FORMAT_BGRX, 0,
            self.width, self.height, self.stride, len(self.pixels),
        ) + self.pixels

    @classmethod
    def decode(cls, payload: bytes) -> RawFrame:
        if not isinstance(payload, bytes) or len(payload) < FRAME_HEADER.size:
            raise RemoteAdapterError("frame packet is truncated")
        (magic, version, pixel_format, flags, width, height, stride,
         payload_len) = FRAME_HEADER.unpack(payload[:FRAME_HEADER.size])
        if magic != FRAME_MAGIC or version != FRAME_VERSION:
            raise RemoteAdapterError("unsupported frame packet")
        if pixel_format != FRAME_FORMAT_BGRX or flags != 0:
            raise RemoteAdapterError("unsupported frame packet format or flags")
        if payload_len != len(payload) - FRAME_HEADER.size:
            raise RemoteAdapterError("frame packet length does not match header")
        frame = cls(
            width=width, height=height, stride=stride,
            pixels=payload[FRAME_HEADER.size:])
        frame.validate()
        return frame

    def matches(self, announcement: StreamAnnouncement) -> bool:
        return (self.width, self.height, self.stride) == (
            announcement.width, announcement.height, announcement.stride)


@dataclass(frozen=True)
class RemoteDisplayIdentity:
    """Authority-bound identity delivered only over the local helper socket."""

    source_machine: str
    trust_domain_id: str
    stream_id: str
    generation: int

    def validate(self) -> None:
        for name in ("source_machine", "trust_domain_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
                raise RemoteAdapterError(f"invalid display {name} {value!r}")
        if (not isinstance(self.stream_id, str)
                or _STREAM_ID.fullmatch(self.stream_id) is None):
            raise RemoteAdapterError(f"invalid display stream_id {self.stream_id!r}")
        _integer(self.generation, label="display generation",
                 minimum=1, maximum=2**64 - 1)

    def encode(self) -> bytes:
        self.validate()
        source = self.source_machine.encode("ascii")
        trust = self.trust_domain_id.encode("ascii")
        stream = self.stream_id.encode("ascii")
        return LOCAL_IDENTITY_HEADER.pack(
            LOCAL_IDENTITY_MAGIC, LOCAL_VERSION, 0, self.generation,
            len(source), len(trust), len(stream)) + source + trust + stream

    @classmethod
    def decode(cls, payload: bytes) -> RemoteDisplayIdentity:
        if (not isinstance(payload, bytes)
                or len(payload) < LOCAL_IDENTITY_HEADER.size):
            raise RemoteAdapterError("display identity is truncated")
        magic, version, flags, generation, source_len, trust_len, stream_len = (
            LOCAL_IDENTITY_HEADER.unpack(payload[:LOCAL_IDENTITY_HEADER.size]))
        if magic != LOCAL_IDENTITY_MAGIC or version != LOCAL_VERSION or flags != 0:
            raise RemoteAdapterError("unsupported display identity")
        lengths = (source_len, trust_len, stream_len)
        if any(length == 0 for length in lengths):
            raise RemoteAdapterError("display identity field is empty")
        if len(payload) != LOCAL_IDENTITY_HEADER.size + sum(lengths):
            raise RemoteAdapterError("display identity length does not match header")
        offset = LOCAL_IDENTITY_HEADER.size
        fields = []
        try:
            for length in lengths:
                fields.append(payload[offset:offset + length].decode("ascii"))
                offset += length
        except UnicodeDecodeError as exc:
            raise RemoteAdapterError("display identity is not ASCII") from exc
        identity = cls(fields[0], fields[1], fields[2], generation)
        identity.validate()
        return identity


@dataclass(frozen=True)
class LocalBoundaryMessage:
    """One C-helper/controller message inside an outer uint32 frame."""

    kind: str
    seq: int = 0
    payload: bytes = b""

    def encode(self) -> bytes:
        if self.kind not in LOCAL_MESSAGE_TYPES:
            raise RemoteAdapterError("unsupported local boundary message kind")
        if (not isinstance(self.seq, int) or isinstance(self.seq, bool)
                or self.seq < 0 or self.seq > 2**64 - 1):
            raise RemoteAdapterError("local boundary sequence is out of bounds")
        if (not isinstance(self.payload, bytes)
                or len(self.payload) > CHANNEL_PAYLOAD_LIMITS["media"]):
            raise RemoteAdapterError("local boundary payload exceeds limit")
        return LOCAL_HEADER.pack(
            LOCAL_MAGIC, LOCAL_VERSION, LOCAL_MESSAGE_TYPES[self.kind], 0,
            self.seq) + self.payload

    @classmethod
    def decode(cls, payload: bytes) -> LocalBoundaryMessage:
        if not isinstance(payload, bytes) or len(payload) < LOCAL_HEADER.size:
            raise RemoteAdapterError("local boundary message is truncated")
        magic, version, kind_id, flags, seq = LOCAL_HEADER.unpack(
            payload[:LOCAL_HEADER.size])
        if magic != LOCAL_MAGIC or version != LOCAL_VERSION or flags != 0:
            raise RemoteAdapterError("unsupported local boundary message")
        kind = _LOCAL_MESSAGE_NAMES.get(kind_id)
        if kind is None:
            raise RemoteAdapterError("unknown local boundary message kind")
        message = cls(kind=kind, seq=seq, payload=payload[LOCAL_HEADER.size:])
        # Reuse encode validation for sequence/payload bounds.
        message.encode()
        return message


def encode_input_event(kind: str, event: Mapping) -> bytes:
    normalized = validate_input_event(kind, event)
    return json.dumps(
        normalized, sort_keys=True, separators=(",", ":")).encode("ascii")


def decode_input_event(kind: str, payload: bytes) -> dict:
    return validate_input_event(
        kind, _json_object(payload, label="input event"))


def encode_local_input_event(kind: str, event: Mapping) -> bytes:
    """Fixed-width C-helper form of a validated semantic input event."""
    normalized = validate_input_event(kind, event)
    if kind == "motion":
        return LOCAL_INPUT_STRUCTS[kind].pack(
            normalized["x_fixed"], normalized["y_fixed"])
    if kind in {"button", "key"}:
        return LOCAL_INPUT_STRUCTS[kind].pack(
            normalized["code"], int(normalized["pressed"]))
    if kind == "axis":
        return LOCAL_INPUT_STRUCTS[kind].pack(
            normalized["axis"], normalized["value_fixed"])
    assert kind == "focus"
    return LOCAL_INPUT_STRUCTS[kind].pack(int(normalized["focused"]))


def decode_local_input_event(kind: str, payload: bytes) -> dict:
    """Decode only the exact fixed-width record for ``kind``."""
    record = LOCAL_INPUT_STRUCTS.get(kind)
    if record is None:
        raise RemoteAdapterError("unsupported nested input kind")
    if not isinstance(payload, bytes) or len(payload) != record.size:
        raise RemoteAdapterError("local input payload length is invalid")
    values = record.unpack(payload)
    if kind == "motion":
        event = {"x_fixed": values[0], "y_fixed": values[1]}
    elif kind in {"button", "key"}:
        if values[1] not in {0, 1}:
            raise RemoteAdapterError("local input pressed flag is invalid")
        event = {"code": values[0], "pressed": bool(values[1])}
    elif kind == "axis":
        event = {"axis": values[0], "value_fixed": values[1]}
    else:
        if values[0] not in {0, 1}:
            raise RemoteAdapterError("local input focus flag is invalid")
        event = {"focused": bool(values[0])}
    return validate_input_event(kind, event)


def validate_input_event(kind: str, event: Mapping) -> dict:
    if kind not in INPUT_KINDS:
        raise RemoteAdapterError("unsupported nested input kind")
    if not isinstance(event, Mapping):
        raise RemoteAdapterError("input event must be a mapping")
    if kind in {"key", "button"}:
        if set(event) != {"code", "pressed"}:
            raise RemoteAdapterError(
                f"{kind} input fields do not match schema")
        code = _integer(
            event["code"], label=f"{kind} code", minimum=1,
            maximum=0xffff)
        if not isinstance(event["pressed"], bool):
            raise RemoteAdapterError(f"{kind} pressed must be boolean")
        return {"code": code, "pressed": event["pressed"]}
    if kind == "motion":
        if set(event) != {"x_fixed", "y_fixed"}:
            raise RemoteAdapterError("motion input fields do not match schema")
        return {
            "x_fixed": _integer(
                event["x_fixed"], label="motion x_fixed",
                minimum=-(2**31), maximum=2**31 - 1),
            "y_fixed": _integer(
                event["y_fixed"], label="motion y_fixed",
                minimum=-(2**31), maximum=2**31 - 1),
        }
    if kind == "axis":
        if set(event) != {"axis", "value_fixed"}:
            raise RemoteAdapterError("axis input fields do not match schema")
        return {
            "axis": _integer(
                event["axis"], label="axis", minimum=0, maximum=1),
            "value_fixed": _integer(
                event["value_fixed"], label="axis value_fixed",
                minimum=-(2**31), maximum=2**31 - 1),
        }
    assert kind == "focus"
    if set(event) != {"focused"} or not isinstance(event.get("focused"), bool):
        raise RemoteAdapterError("focus input fields do not match schema")
    return {"focused": event["focused"]}
