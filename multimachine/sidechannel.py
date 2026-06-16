"""Remote whole-window viewer control side-channel (Phase 1, slice b).

The shipped per-view path (``qdwin_shell_v1.subscribe_view_stream`` →
``qdwin_view_stream_v1.approved`` → ``qdistro-forward`` RDP; input back via
``qdwin_stream_input_v1``) carries **pixels** and **input** but no window
metadata — a remote viewer over raw RDP is "a video stream, not a real
toplevel". 03's decision: add a metadata/control side-channel so the peer (VM-B)
renders the stream as a *managed* qdwin toplevel: window id, title, app id,
source machine, secctx, requested size, sensitivity, focus, close/disconnect.

This module is the **contract** for that side-channel — codex impl-1: "start
capturing the metadata/control side channel shape during Phase 1 … but avoid
building the full broker/security system until the viewer proves the interaction
model." It is the sidecar JSON control channel (codex B4), woven with the dock
**generation** (``generation.py``) so stale control from an old dock session is
rejected exactly like stale frames/input. Pure-Python + unit-tested; the eventual
qdwin/Python viewer mirrors it. No compositor C here.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum

SIDECHANNEL_VERSION = 1


class Sensitivity(str, Enum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"   # may be barred from remote outputs (05/07)


class PointerPolicy(str, Enum):
    FORWARD = "forward"       # inject pointer back to source
    LOCAL_ONLY = "local"      # viewer-local pointer only (read-only stream)


@dataclass(frozen=True)
class RemoteWindowMeta:
    """Identity + presentation hints for one exported toplevel.

    Carried alongside the RDP pixel stream so the viewer builds a managed
    toplevel. ``secctx`` is the opaque qdistro security-context string (with the
    machine axis, 04/07); the viewer treats it as vouched data, never kernel
    facts.
    """

    window_id: int
    source_machine: str
    title: str = ""
    app_id: str = ""
    secctx: str = ""
    req_w: int = 0
    req_h: int = 0
    sensitivity: Sensitivity = Sensitivity.NORMAL
    pointer_policy: PointerPolicy = PointerPolicy.FORWARD


# ---- control messages (generation-stamped) -------------------------------
@dataclass(frozen=True)
class ControlMessage:
    type: str
    generation: int


@dataclass(frozen=True)
class Announce(ControlMessage):
    meta: RemoteWindowMeta = field(default=None)  # type: ignore[assignment]


@dataclass(frozen=True)
class Configure(ControlMessage):
    window_id: int = 0
    w: int = 0
    h: int = 0


@dataclass(frozen=True)
class Focus(ControlMessage):
    window_id: int = 0
    focused: bool = False


@dataclass(frozen=True)
class TitleChanged(ControlMessage):
    window_id: int = 0
    title: str = ""


@dataclass(frozen=True)
class CloseRequest(ControlMessage):
    """viewer → source: the user asked to close the remote window."""

    window_id: int = 0


@dataclass(frozen=True)
class Closed(ControlMessage):
    """source → viewer: the toplevel is gone (maps to view_stream torn_down)."""

    window_id: int = 0
    reason: str = ""


@dataclass(frozen=True)
class Disconnect(ControlMessage):
    """The dock link / stream ended; viewer blanks + removes proxies."""

    reason: str = ""


_TYPES = {
    "announce": Announce, "configure": Configure, "focus": Focus,
    "title": TitleChanged, "close_request": CloseRequest, "closed": Closed,
    "disconnect": Disconnect,
}


def encode(msg: ControlMessage) -> str:
    d = asdict(msg)
    d["v"] = SIDECHANNEL_VERSION
    if "meta" in d and d["meta"] is not None:
        # enums -> values
        d["meta"]["sensitivity"] = msg.meta.sensitivity.value  # type: ignore[attr-defined]
        d["meta"]["pointer_policy"] = msg.meta.pointer_policy.value  # type: ignore[attr-defined]
    return json.dumps(d)


def decode(raw: str) -> ControlMessage:
    d = json.loads(raw)
    if d.get("v") != SIDECHANNEL_VERSION:
        raise ValueError(f"unsupported sidechannel version {d.get('v')}")
    typ = d.get("type")
    cls = _TYPES.get(typ)
    if cls is None:
        raise ValueError(f"unknown control message type {typ!r}")
    gen = d["generation"]
    if cls is Announce:
        m = d["meta"]
        meta = RemoteWindowMeta(
            window_id=m["window_id"], source_machine=m["source_machine"],
            title=m.get("title", ""), app_id=m.get("app_id", ""),
            secctx=m.get("secctx", ""), req_w=m.get("req_w", 0),
            req_h=m.get("req_h", 0),
            sensitivity=Sensitivity(m.get("sensitivity", "normal")),
            pointer_policy=PointerPolicy(m.get("pointer_policy", "forward")))
        return Announce("announce", gen, meta)
    if cls is Configure:
        return Configure("configure", gen, d["window_id"], d["w"], d["h"])
    if cls is Focus:
        return Focus("focus", gen, d["window_id"], d["focused"])
    if cls is TitleChanged:
        return TitleChanged("title", gen, d["window_id"], d["title"])
    if cls is CloseRequest:
        return CloseRequest("close_request", gen, d["window_id"])
    if cls is Closed:
        return Closed("closed", gen, d["window_id"], d.get("reason", ""))
    if cls is Disconnect:
        return Disconnect("disconnect", gen, d.get("reason", ""))
    raise AssertionError("unreachable")


# ---- viewer-side state (VM-B) --------------------------------------------
@dataclass
class ProxyWindow:
    meta: RemoteWindowMeta
    w: int
    h: int
    focused: bool = False
    title: str = ""


class RemoteViewerState:
    """Tracks the live remote proxy windows on the peer (VM-B).

    Applies control messages with generation guarding: control from a
    non-current dock generation is rejected (stale-rejection, D3), and a
    Disconnect blanks + removes every proxy of that generation. The viewer is a
    client of the dock session's current generation.
    """

    def __init__(self, generation: int):
        self.generation = generation
        self.windows: dict[int, ProxyWindow] = {}
        self.rejected: list[tuple[int, str, str]] = []  # (gen, type, reason)
        self.connected = True

    def set_generation(self, generation: int) -> None:
        """Redock / new dock session: bump generation, drop old proxies."""
        self.generation = generation
        self.windows.clear()
        self.connected = True

    def apply(self, msg: ControlMessage) -> bool:
        if not self.connected and not isinstance(msg, Disconnect):
            self.rejected.append((msg.generation, msg.type, "disconnected"))
            return False
        if msg.generation != self.generation:
            self.rejected.append((msg.generation, msg.type, "stale-generation"))
            return False
        if isinstance(msg, Announce):
            self.windows[msg.meta.window_id] = ProxyWindow(
                meta=msg.meta, w=msg.meta.req_w, h=msg.meta.req_h,
                title=msg.meta.title)
            return True
        if isinstance(msg, Configure):
            w = self.windows.get(msg.window_id)
            if not w:
                return False
            w.w, w.h = msg.w, msg.h
            return True
        if isinstance(msg, Focus):
            w = self.windows.get(msg.window_id)
            if not w:
                return False
            # one focused proxy at a time (single seat, 04/05).
            if msg.focused:
                for other in self.windows.values():
                    other.focused = False
            w.focused = msg.focused
            return True
        if isinstance(msg, TitleChanged):
            w = self.windows.get(msg.window_id)
            if not w:
                return False
            w.title = msg.title
            return True
        if isinstance(msg, Closed):
            return self.windows.pop(msg.window_id, None) is not None
        if isinstance(msg, Disconnect):
            # blank + remove every proxy; the source app keeps running (detach).
            self.windows.clear()
            self.connected = False
            return True
        if isinstance(msg, CloseRequest):
            # viewer-originated; not applied to viewer state (sent upstream).
            return True
        return False
