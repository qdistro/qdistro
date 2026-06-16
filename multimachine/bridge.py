"""Bridge: shipped per-view RDP protocol  <->  Phase-1 viewer side-channel.

The glue codex impl-2's one-host slice needs (steps 3-4): take a
``qdwin_view_stream_v1.approved`` result + the source toplevel's metadata and
produce a side-channel :class:`~.sidechannel.Announce` (minting the opaque
``stream_id`` join key), and map ``torn_down`` to a teardown message. Plus the
FreeRDP-client argv builder for the viewer's decode side (the 09
"FreeRDP no-scaling/full-screen validation" component).

The side-channel deliberately does **not** carry the RDP credentials (codex
impl-2: "should not duplicate RDP credentials except as a correlation key"). The
``stream_id`` is the correlation key; the actual RDP endpoint (port/cert/
password) stays with the caller that launches the decode client.

Pure-Python + unit-tested. The live wiring (drive ``subscribe_view_stream`` via
``qdwin-bystander --subscribe``, launch ``sdl-freerdp``, capture, run the oracle)
is the VM-gated scenario; this module is its testable core.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable

from .sidechannel import (
    Announce, ControlMessage, PointerPolicy, RemoteWindowMeta, Sensitivity,
    map_torn_down,
)


@dataclass(frozen=True)
class ViewStreamApproved:
    """The 4 values from ``qdwin_view_stream_v1.approved`` (shell-v1.xml:1960)."""

    pipewire_node_name: str
    rdp_port: int
    rdp_cert_path: str
    rdp_password: str


@dataclass(frozen=True)
class SourceWindowInfo:
    """What the source side knows about the exported toplevel (from qdwin:
    handle, title, app_id, secctx, size) plus this machine's identity."""

    window_id: int
    source_machine: str
    title: str = ""
    app_id: str = ""
    security_label: str = ""
    req_w: int = 0
    req_h: int = 0
    sensitivity: Sensitivity = Sensitivity.NORMAL
    pointer_policy: PointerPolicy = PointerPolicy.FORWARD


def mint_stream_id(window_id: int,
                   nonce: Callable[[], str] = lambda: secrets.token_hex(8)) -> str:
    """A FRESH per-export correlation key (codex impl-3 finding 1): an
    unguessable nonce, NOT derived from reusable endpoint/window properties.
    Two exports of the same window on the same port/node get *different* ids, so
    a delayed message from a prior export cannot attach to the new one. The
    ``window_id`` prefix is for readability only; the nonce carries the identity.
    ``nonce`` is injectable for deterministic tests."""
    return f"vs-{window_id}-{nonce()}"


def bridge_approved(approved: ViewStreamApproved, source: SourceWindowInfo,
                    generation: int, stream_id: str | None = None,
                    nonce: Callable[[], str] = lambda: secrets.token_hex(8)
                    ) -> Announce:
    """Build the side-channel Announce for an approved export (no RDP creds).

    A fresh ``stream_id`` is minted per call unless one is supplied explicitly.
    """
    sid = stream_id or mint_stream_id(source.window_id, nonce)
    meta = RemoteWindowMeta(
        window_id=source.window_id, source_machine=source.source_machine,
        stream_id=sid, title=source.title, app_id=source.app_id,
        security_label=source.security_label, req_w=source.req_w,
        req_h=source.req_h, sensitivity=source.sensitivity,
        pointer_policy=source.pointer_policy)
    return Announce("announce", generation, meta)


def bridge_torn_down(reason: str, generation: int, window_id: int,
                     stream_id: str = "") -> ControlMessage:
    """Map the shipped ``torn_down(reason)`` to a side-channel teardown.

    ``stream_id`` is the live export's key so a ``Closed`` only removes the
    matching proxy (not a same-window_id successor)."""
    return map_torn_down(reason, generation, window_id, stream_id)


def rdp_client_argv(approved: ViewStreamApproved, host: str = "127.0.0.1",
                    width: int = 0, height: int = 0,
                    verify_cert: bool = False) -> list[str]:
    """Build an ``sdl-freerdp`` command for the viewer to decode the stream.

    Forces **no client-side scaling** and full-window decode so the captured
    pixels are the source's, 1:1 — the monitor-extension invariant (09: hidden
    scaling invalidates the proof).

    The one-time **password is NOT put on argv** (codex impl-3 finding 5: argv is
    visible via process listings / shell logs). Instead ``/from-stdin`` makes
    FreeRDP read credentials from stdin; the caller feeds :func:`rdp_password`
    on the child's stdin.

    Certificate handling (finding 5): ``verify_cert=False`` (default) is TOFU via
    ``/cert:ignore`` — explicitly **out of scope for the host slice**; the OTP
    authenticates the connection. ``verify_cert=True`` pins the
    ``approved.rdp_cert_path`` the compositor served (``/cert:name`` + the cert
    file) for the product path.
    """
    argv = [
        "sdl-freerdp",
        f"/v:{host}:{approved.rdp_port}",
        "/from-stdin",         # read the OTP from stdin, not argv
        "/scale:100",          # no client-side scaling
        "-grab-keyboard",      # do not steal the host's keyboard
    ]
    if verify_cert and approved.rdp_cert_path:
        argv.append(f"/cert:name:{approved.rdp_cert_path}")
    else:
        argv.append("/cert:ignore")   # TOFU; cert validation out of scope (v1)
    if width and height:
        argv.append(f"/size:{width}x{height}")
    return argv


def rdp_password(approved: ViewStreamApproved) -> str:
    """The OTP to feed on the FreeRDP child's **stdin** (paired with the
    ``/from-stdin`` argv), keeping it off the process command line."""
    return approved.rdp_password
