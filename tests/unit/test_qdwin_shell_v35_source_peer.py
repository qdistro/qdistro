"""Lock the qdwin_shell_v1 v35 source-peer-identity surface and the
single-client-per-secctx-listener invariant.

ΔB10 / Sol-r2 remediation: the clipboard gate relays the selection
source's kernel-authenticated (pid, starttime) to
CheckClipboardTransfer. That relay is only sound when the relayed
identity belongs to the very wl_client that issued set_selection —
tag-tuple equality does not imply peer equality because qdwin stamps
the same (engine, app_id, instance) on every client accepted through
one secctx listener.

Two complementary product changes make it sound:

  * v35 event ``selection_set_source_peer_identity`` — carries the
    SOURCE client's own compositor-observed peer tuple, so the shell
    never has to borrow the focused toplevel's identity.
  * single-attach on engine ``qdistro.tier3s`` — qdwin consumes the
    context on the first accept and refuse-closes every later
    connection on the same listener, so one attested tuple provably
    names exactly one peer.

These are static assertions (XML shape + source-text invariants). The
live behaviour is driven in-VM by s126 step 5b (second connect gets a
live EOF + refusal log) and s127 (rule-driven live cross-silo
broker:allow through the relayed source pid).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
XML_PATH = ROOT / "qdwin" / "qdwin" / "qdwin-shell-v1.xml"
QDWIN_C = ROOT / "qdwin" / "qdwin" / "qdwin.c"
GATE_QML = ROOT / "qdshell" / "Services" / "Qdshell" / "ClipboardGate.qml"


@pytest.fixture(scope="module")
def shell_iface() -> ET.Element:
    tree = ET.parse(XML_PATH)
    iface = tree.getroot().find("./interface[@name='qdwin_shell_v1']")
    assert iface is not None, "qdwin_shell_v1 interface missing"
    return iface


def test_interface_version_at_least_35(shell_iface: ET.Element) -> None:
    # ensures: the v35 source-peer sidecar is actually advertised — a
    # shell bound at >= 35 must be able to rely on the event existing.
    assert int(shell_iface.attrib["version"]) >= 35


def test_source_peer_identity_event(shell_iface: ET.Element) -> None:
    # ensures: the wire contract the binding/QML consume cannot drift —
    # renaming an arg or dropping an arm silently breaks the broker
    # relay for every tagged selection source.
    ev = shell_iface.find(
        "./event[@name='selection_set_source_peer_identity']")
    assert ev is not None
    assert ev.attrib.get("since") == "35"
    args = [(a.attrib["name"], a.attrib["type"]) for a in ev.findall("arg")]
    assert args == [
        ("peer_pid", "uint"),
        ("peer_starttime", "uint"),
        ("peer_starttime_hi", "uint"),
        ("peer_uid", "uint"),
        ("peer_exe", "string"),
        ("peer_selinux_label", "string"),
    ]


def test_new_event_is_the_last_event(shell_iface: ET.Element) -> None:
    # ensures: wire compatibility — event opcodes are positional, so a
    # new-version event MUST be appended after every existing event.
    # Inserting mid-list renumbers all later opcodes and breaks any
    # pre-v35 build talking to this compositor (Sol r3 P1).
    events = shell_iface.findall("./event")
    assert events, "qdwin_shell_v1 has no events"
    assert events[-1].attrib["name"] == "selection_set_source_peer_identity"


def test_secctx_context_has_consumed_state() -> None:
    # ensures: the single-attach slot cannot be silently deleted — a
    # secctx listener without the consumed flag would accept every
    # extra client back into the equal-tag collision Sol flagged.
    src = QDWIN_C.read_text()
    assert re.search(r"int\s+consumed\s*;", src)


def test_secctx_accept_refuses_extra_client() -> None:
    # ensures: the consumed path actively refuse-closes (accept-then-
    # close) — silently skipping accept would strand the second client
    # in the listen backlog where it hangs instead of failing loudly.
    src = QDWIN_C.read_text()
    cb = src[src.index("qdwin_secctx_listen_cb(int fd"):]
    assert "sec->consumed" in cb
    assert "refused extra connection" in cb
    assert re.search(r"close\(client_fd\)", cb)


def test_secctx_consume_is_engine_scoped() -> None:
    # ensures: consumption is deliberate policy for the tier3s engine,
    # not a blanket multi-attach ban — the spec permits multi-attach
    # (Flatpak) and other engines must keep it.
    src = QDWIN_C.read_text()
    assert 'strcmp(sec->sandbox_engine, "qdistro.tier3s")' in src
    assert "sec->consumed = 1" in src


def test_compositor_emits_source_peer_identity() -> None:
    # ensures: qdwin actually sends the v35 event from the set_selection
    # path, version-gated like every other sidecar.
    src = QDWIN_C.read_text()
    assert "qdwin_shell_v1_send_selection_set_source_peer_identity" in src
    assert re.search(
        r"wl_resource_get_version\(qdwin->shell_resource\)\s*>=\s*35", src)


def test_compositor_advertises_v35() -> None:
    # ensures: wl_global_create advertises >= 35 — bumping the XML
    # without the advertised version leaves the negotiated bind version
    # below 35 on every resource and silently disables the sidecar
    # (exactly the b25 failure mode: v35 binary present, zero emits).
    src = QDWIN_C.read_text()
    m = re.search(
        r"wl_global_create\([^;]*qdwin_shell_v1_interface[^;]*\)",
        src, re.S)
    assert m, "qdwin_shell_v1 wl_global_create site not found"
    assert re.search(r",\s*(3[5-9]|[4-9]\d|[1-9]\d{2,})\s*,", m.group(0)), \
        "qdwin_shell_v1 advertised version must be >= 35"


def test_gate_relays_source_peer_identity() -> None:
    # ensures: the QML prefers the wire-attested source peer tuple over
    # the focused-handle identity — borrowing the destination's pid is
    # exactly the equal-tag confusion this closes.
    src = GATE_QML.read_text()
    assert "_pendingSrcPeer" in src
    assert "_onSelectionSetSourcePeerIdentity" in src
    assert "selectionSetSourcePeerIdentity.connect" in src
    assert "_ensureVerifiedIdentity" in src
