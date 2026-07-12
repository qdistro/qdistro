"""R6 authenticated nested-over-network adapter contract tests."""
from __future__ import annotations

import json

import pytest

from multimachine.remote_adapter import (
    AuthenticationError,
    AuthenticatedEnvelopeCodec,
    AuthenticatedReceiver,
    FlowControlError,
    MediaFlowController,
    ProxyAttachmentState,
    RemoteAdapterError,
    RemoteProxyLifecycle,
    RemoteSessionBinding,
    ReplayError,
    SOURCE_TO_VIEWER,
    VIEWER_TO_SOURCE,
)


KEY = bytes(range(32))


def binding(**changes) -> RemoteSessionBinding:
    values = {
        "source_machine": "vm-source",
        "viewer_machine": "vm-viewer",
        "trust_domain_id": "owner-machines",
        "generation": 51,
        "session_id": "viewer-session-7f5c",
        "stream_id": "stream_0123456789abcdef",
        "epoch": 1,
    }
    values.update(changes)
    return RemoteSessionBinding(**values)


def codec(**changes) -> AuthenticatedEnvelopeCodec:
    return AuthenticatedEnvelopeCodec(binding(**changes), KEY)


def wire(c=None, *, direction=SOURCE_TO_VIEWER, channel="control",
         kind="announce", seq=1, payload=b"{}", ack=0):
    c = c or codec()
    return c.seal(direction=direction, channel=channel, kind=kind, seq=seq,
                  payload=payload, ack=ack)


def test_round_trip_is_bound_to_all_remote_identity_axes() -> None:
    c = codec()
    msg = c.open(wire(c, payload=b'{"window":7}'),
                 expected_direction=SOURCE_TO_VIEWER)
    assert (msg.channel, msg.kind, msg.seq, msg.payload) == (
        "control", "announce", 1, b'{"window":7}')


@pytest.mark.parametrize(("field", "value"), [
    ("source_machine", "other-source"),
    ("viewer_machine", "other-viewer"),
    ("trust_domain_id", "other-domain"),
    ("generation", 52),
    ("session_id", "other-session"),
    ("stream_id", "other_0123456789abcdef"),
    ("epoch", 2),
])
def test_cross_binding_replay_is_rejected(field, value) -> None:
    with pytest.raises(AuthenticationError, match="binding mismatch"):
        codec(**{field: value}).open(
            wire(), expected_direction=SOURCE_TO_VIEWER)


def test_tamper_and_wrong_key_fail_authentication() -> None:
    encoded = json.loads(wire())
    encoded["kind"] = "closed"
    with pytest.raises(AuthenticationError, match="authentication failed"):
        codec().open(json.dumps(encoded).encode(),
                     expected_direction=SOURCE_TO_VIEWER)
    other = AuthenticatedEnvelopeCodec(binding(), b"x" * 32)
    with pytest.raises(AuthenticationError, match="authentication failed"):
        other.open(wire(), expected_direction=SOURCE_TO_VIEWER)


def test_direction_and_channel_are_cryptographically_separated() -> None:
    c = codec()
    with pytest.raises(AuthenticationError, match="direction mismatch"):
        c.open(wire(c, direction=VIEWER_TO_SOURCE),
               expected_direction=SOURCE_TO_VIEWER)
    encoded = json.loads(wire(c))
    encoded["channel"] = "input"
    with pytest.raises(AuthenticationError, match="authentication failed"):
        c.open(json.dumps(encoded).encode(),
               expected_direction=SOURCE_TO_VIEWER)


def test_receiver_rejects_replay_gap_and_input_without_separate_grant() -> None:
    c = codec()
    receiver = AuthenticatedReceiver(
        c, direction=VIEWER_TO_SOURCE, allow_input=True)
    first = wire(c, direction=VIEWER_TO_SOURCE, channel="input",
                 kind="button", seq=1, payload=b"down")
    assert receiver.receive(first).payload == b"down"
    with pytest.raises(ReplayError, match="expected 2"):
        receiver.receive(first)
    with pytest.raises(ReplayError, match="expected 2"):
        receiver.receive(wire(
            c, direction=VIEWER_TO_SOURCE, channel="input", kind="button",
            seq=3, payload=b"up"))

    read_only = AuthenticatedReceiver(
        c, direction=VIEWER_TO_SOURCE, allow_input=False)
    with pytest.raises(AuthenticationError, match="lacks input capability"):
        read_only.receive(first)


def test_each_channel_has_an_independent_sequence() -> None:
    c = codec()
    receiver = AuthenticatedReceiver(
        c, direction=SOURCE_TO_VIEWER, allow_input=False)
    assert receiver.receive(wire(c, channel="control", seq=1)).seq == 1
    assert receiver.receive(wire(
        c, channel="media", kind="frame", seq=1, payload=b"pixels")).seq == 1


def test_strict_schema_sizes_and_scalar_types_fail_closed() -> None:
    with pytest.raises(RemoteAdapterError, match="32 bytes"):
        AuthenticatedEnvelopeCodec(binding(), b"short")
    with pytest.raises(RemoteAdapterError, match="positive integer"):
        codec(generation=True)
    with pytest.raises(RemoteAdapterError, match="payload exceeds limit"):
        wire(channel="input", kind="key", payload=b"x" * 4097)
    doc = json.loads(wire())
    doc["unexpected"] = True
    with pytest.raises(RemoteAdapterError, match="fields"):
        codec().open(json.dumps(doc).encode(),
                     expected_direction=SOURCE_TO_VIEWER)

    # Python considers True == 1; reconstruct and validate the claimed binding
    # before equality so a bool cannot masquerade as a signed generation.
    doc = json.loads(wire())
    doc["binding"]["generation"] = True
    with pytest.raises(RemoteAdapterError, match="positive integer"):
        codec().open(json.dumps(doc).encode(),
                     expected_direction=SOURCE_TO_VIEWER)


def test_media_flow_is_bounded_and_acknowledged_cumulatively() -> None:
    flow = MediaFlowController(max_inflight=2, max_frame_bytes=16)
    assert flow.offer(b"frame-1") == 1
    assert flow.offer(b"frame-2") == 2
    with pytest.raises(FlowControlError, match="window is full"):
        flow.offer(b"frame-3")
    flow.acknowledge(1)
    assert flow.inflight == 1
    assert flow.offer(b"frame-3") == 3
    flow.acknowledge(3)
    assert flow.inflight == 0
    with pytest.raises(FlowControlError, match="stale or unsent"):
        flow.acknowledge(2)
    with pytest.raises(FlowControlError, match="negotiated size"):
        flow.offer(b"x" * 17)


def test_transport_loss_detaches_but_does_not_kill_source() -> None:
    life = RemoteProxyLifecycle()
    life.announce(epoch=1, source_revision=10)
    life.transport_lost()
    assert life.state is ProxyAttachmentState.DETACHED
    assert life.source_alive

    life.reconcile(epoch=2, source_revision=10, source_alive=True)
    assert life.state is ProxyAttachmentState.ATTACHED
    assert life.source_alive


def test_reconcile_and_close_reject_stale_epochs_and_revisions() -> None:
    life = RemoteProxyLifecycle()
    life.announce(epoch=3, source_revision=10)
    life.transport_lost()
    with pytest.raises(ReplayError, match="epoch is stale"):
        life.reconcile(epoch=3, source_revision=10, source_alive=True)
    with pytest.raises(ReplayError, match="revision is stale"):
        life.reconcile(epoch=4, source_revision=9, source_alive=True)

    life.reconcile(epoch=4, source_revision=11, source_alive=False)
    assert life.state is ProxyAttachmentState.CLOSED
    assert not life.source_alive
    with pytest.raises(ReplayError, match="close revision is stale"):
        life.source_closed(source_revision=11)
