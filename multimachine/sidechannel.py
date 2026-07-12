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

Three transport channels, one window (codex impl-2): JSON **control** (this
module), RDP **pixels** (``qdistro-forward``), and **input**
(``qdwin_stream_input_v1``). They are correlated by an opaque ``stream_id``
minted per export — the join key that stops the viewer attaching metadata for
window A to pixels/input for stream B after reconnect/replay/rapid close-reopen.
``stream_id`` is *not* an RDP credential; it is a correlation token only.

Three teardown signals are distinct (codex impl-2):

- ``Closed`` — the *source toplevel* is gone; remove that one proxy.
- ``Disconnect`` — the dock/link/viewer transport ended; blank *all* proxies for
  that generation; the source toplevels may still exist (detach, not death).
- the shipped ``qdwin_view_stream_v1.torn_down`` — the *pixel stream* ended; the
  bridge maps it to ``Closed`` (when caused by source close) or ``Disconnect``
  (link/admin/transport). When ambiguous, prefer ``Disconnect`` + a later
  authoritative ``Closed`` (see :func:`map_torn_down`).
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
    toplevel.

    ``stream_id`` is the opaque join key (codex impl-2) correlating this control
    metadata with one ``qdwin_view_stream_v1.approved`` pixel stream and its
    input channel — minted per export, NOT an RDP credential.

    ``security_label`` is an **opaque display/audit label only** (codex impl-2):
    it is the qdistro security-context string (with the machine axis, 04/07) the
    *source-side* policy vouches for. The viewer treats it as vouched data, never
    kernel facts, and **must not** make authorization decisions from it in
    Phase 1 — empty is valid; it is unparsed and not an ACL input. Real
    enforcement is the broker's job, deferred per impl-1.
    """

    window_id: int
    source_machine: str
    stream_id: str = ""
    title: str = ""
    app_id: str = ""
    security_label: str = ""   # opaque display/audit label; NOT an ACL input
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
    stream_id: str = ""


@dataclass(frozen=True)
class Focus(ControlMessage):
    window_id: int = 0
    focused: bool = False
    stream_id: str = ""


@dataclass(frozen=True)
class TitleChanged(ControlMessage):
    window_id: int = 0
    title: str = ""
    stream_id: str = ""


@dataclass(frozen=True)
class CloseRequest(ControlMessage):
    """viewer → source: the user asked to close the remote window."""

    window_id: int = 0
    stream_id: str = ""


@dataclass(frozen=True)
class Closed(ControlMessage):
    """source → viewer: the toplevel is gone (maps to view_stream torn_down)."""

    window_id: int = 0
    reason: str = ""
    stream_id: str = ""


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
            stream_id=m.get("stream_id", ""),
            title=m.get("title", ""), app_id=m.get("app_id", ""),
            security_label=m.get("security_label", ""), req_w=m.get("req_w", 0),
            req_h=m.get("req_h", 0),
            sensitivity=Sensitivity(m.get("sensitivity", "normal")),
            pointer_policy=PointerPolicy(m.get("pointer_policy", "forward")))
        return Announce("announce", gen, meta)
    if cls is Configure:
        return Configure("configure", gen, d["window_id"], d["w"], d["h"],
                         d.get("stream_id", ""))
    if cls is Focus:
        return Focus("focus", gen, d["window_id"], d["focused"],
                     d.get("stream_id", ""))
    if cls is TitleChanged:
        return TitleChanged("title", gen, d["window_id"], d["title"],
                            d.get("stream_id", ""))
    if cls is CloseRequest:
        return CloseRequest("close_request", gen, d["window_id"],
                            d.get("stream_id", ""))
    if cls is Closed:
        return Closed("closed", gen, d["window_id"], d.get("reason", ""),
                      d.get("stream_id", ""))
    if cls is Disconnect:
        return Disconnect("disconnect", gen, d.get("reason", ""))
    raise AssertionError("unreachable")


def map_torn_down(reason: str, generation: int, window_id: int,
                  stream_id: str = "") -> ControlMessage:
    """Map a shipped ``qdwin_view_stream_v1.torn_down`` reason to a side-channel
    teardown message (codex impl-2). Source-close → ``Closed`` (that proxy);
    link/admin/transport → ``Disconnect`` (blank all). Ambiguous → ``Disconnect``
    (a later authoritative ``Closed`` may follow). ``stream_id`` must be the live
    export's key so the ``Closed`` only removes the matching proxy."""
    r = (reason or "").lower()
    if any(k in r for k in ("closed", "source", "toplevel", "exit", "destroyed")):
        return Closed("closed", generation, window_id, reason, stream_id)
    # link / admin-revoke / lock / subscriber-disconnect / unknown -> detach
    return Disconnect("disconnect", generation, reason or "stream torn down")


# ---- viewer-side state (VM-B) --------------------------------------------
@dataclass
class ProxyWindow:
    meta: RemoteWindowMeta
    w: int
    h: int
    focused: bool = False
    title: str = ""

    @property
    def stream_id(self) -> str:
        return self.meta.stream_id


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
        """Redock / new dock session: bump to a STRICTLY NEWER generation and
        drop old proxies. ``RemoteViewerState`` is *not* the generator — the
        authoritative source is ``DockSession`` (generation.py); this only
        enforces monotonicity so a reused/old generation after disconnect stays
        invalid (codex impl-2 stale/teardown hole)."""
        if generation <= self.generation:
            raise ValueError(
                f"redock generation must be strictly newer than "
                f"{self.generation}, got {generation}")
        self.generation = generation
        self.windows.clear()
        self.connected = True

    def proxy_for_stream(self, stream_id: str) -> ProxyWindow | None:
        """Look up a proxy by its opaque stream_id join key (codex impl-2) so
        pixels/input for a stream attach to the right window's metadata."""
        for w in self.windows.values():
            if w.stream_id and w.stream_id == stream_id:
                return w
        return None

    def apply(self, msg: ControlMessage) -> bool:
        if not self.connected and not isinstance(msg, Disconnect):
            self.rejected.append((msg.generation, msg.type, "disconnected"))
            return False
        if msg.generation != self.generation:
            self.rejected.append((msg.generation, msg.type, "stale-generation"))
            return False
        if isinstance(msg, Announce):
            sid = msg.meta.stream_id
            # stream_id is the per-export join key: it must be present and not
            # collide with a live proxy (replay / cross-stream attachment).
            if not sid:
                self.rejected.append((msg.generation, msg.type, "empty-stream-id"))
                return False
            if any(w.stream_id == sid for w in self.windows.values()):
                self.rejected.append((msg.generation, msg.type, "duplicate-stream-id"))
                return False
            self.windows[msg.meta.window_id] = ProxyWindow(
                meta=msg.meta, w=msg.meta.req_w, h=msg.meta.req_h,
                title=msg.meta.title)
            return True
        # All per-window control messages carry stream_id and must match the
        # live proxy's stream_id — otherwise a delayed message from a prior
        # export that reused the same window_id (same generation) could mutate
        # or remove the new proxy.
        if isinstance(msg, (Configure, Focus, TitleChanged, Closed, CloseRequest)):
            w = self.windows.get(msg.window_id)
            if not w:
                return False
            if msg.stream_id != w.stream_id:
                self.rejected.append((msg.generation, msg.type, "stream-id-mismatch"))
                return False
        if isinstance(msg, Configure):
            w.w, w.h = msg.w, msg.h
            return True
        if isinstance(msg, Focus):
            # one focused proxy at a time (single seat, 04/05).
            if msg.focused:
                for other in self.windows.values():
                    other.focused = False
            w.focused = msg.focused
            return True
        if isinstance(msg, TitleChanged):
            w.title = msg.title
            return True
        if isinstance(msg, Closed):
            self.windows.pop(msg.window_id, None)
            return True
        if isinstance(msg, Disconnect):
            # blank + remove every proxy; the source app keeps running (detach).
            self.windows.clear()
            self.connected = False
            return True
        if isinstance(msg, CloseRequest):
            # viewer-originated; not applied to viewer state (sent upstream).
            return True
        return False
