"""R6 authenticated nested-over-network adapter contract tests."""
from __future__ import annotations

import hashlib
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
    RemoteSessionKey,
    RemoteSessionBinding,
    ReplayError,
    SOURCE_TO_VIEWER,
    VIEWER_TO_SOURCE,
    verify_tls_peer_certificate,
)


KEY = bytes(range(32))
NOW = 1_800_000_000
KEY_ID = "adapter-key-7f5c"
KEY_EXPIRES = NOW + 8 * 60 * 60
SOURCE_CERT = b"source TLS certificate DER fixture"
VIEWER_CERT = b"viewer TLS certificate DER fixture"
SOURCE_PIN = hashlib.sha256(SOURCE_CERT).hexdigest()
VIEWER_PIN = hashlib.sha256(VIEWER_CERT).hexdigest()


def binding(**changes) -> RemoteSessionBinding:
    values = {
        "source_machine": "vm-source",
        "viewer_machine": "vm-viewer",
        "trust_domain_id": "owner-machines",
        "generation": 51,
        "session_id": "viewer-session-7f5c",
        "stream_id": "stream_0123456789abcdef",
        "epoch": 1,
        "key_id": KEY_ID,
        "key_expires_at": KEY_EXPIRES,
        "source_tls_cert_sha256": SOURCE_PIN,
        "viewer_tls_cert_sha256": VIEWER_PIN,
    }
    values.update(changes)
    return RemoteSessionBinding(**values)


def session_key(**changes) -> RemoteSessionKey:
    values = {
        "key_id": KEY_ID,
        "secret": KEY,
        "issued_at": NOW,
        "expires_at": KEY_EXPIRES,
    }
    values.update(changes)
    return RemoteSessionKey(**values)


def codec(*, now=NOW, key=None, **changes) -> AuthenticatedEnvelopeCodec:
    return AuthenticatedEnvelopeCodec(
        binding(**changes), key or session_key(), now=lambda: now)


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
    ("key_id", "other-key-7f5c"),
    ("key_expires_at", KEY_EXPIRES + 1),
    ("source_tls_cert_sha256", "1" * 64),
    ("viewer_tls_cert_sha256", "2" * 64),
])
def test_cross_binding_replay_is_rejected(field, value) -> None:
    key_changes = {}
    if field == "key_id":
        key_changes["key_id"] = value
    elif field == "key_expires_at":
        key_changes["expires_at"] = value
    with pytest.raises(AuthenticationError, match="binding mismatch"):
        codec(key=session_key(**key_changes), **{field: value}).open(
            wire(), expected_direction=SOURCE_TO_VIEWER)


def test_tamper_and_wrong_key_fail_authentication() -> None:
    encoded = json.loads(wire())
    encoded["kind"] = "closed"
    with pytest.raises(AuthenticationError, match="authentication failed"):
        codec().open(json.dumps(encoded).encode(),
                     expected_direction=SOURCE_TO_VIEWER)
    other = AuthenticatedEnvelopeCodec(
        binding(), session_key(secret=b"x" * 32), now=lambda: NOW)
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


def test_tls_peer_certificate_must_match_authority_vouched_pin() -> None:
    verify_tls_peer_certificate(SOURCE_CERT, SOURCE_PIN)
    with pytest.raises(AuthenticationError, match="pin mismatch"):
        verify_tls_peer_certificate(b"attacker certificate", SOURCE_PIN)
    with pytest.raises(RemoteAdapterError, match="lowercase SHA-256"):
        verify_tls_peer_certificate(SOURCE_CERT, SOURCE_PIN.upper())


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
        session_key(secret=b"short")
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


def test_session_key_lifetime_binding_expiry_and_revocation_fail_closed() -> None:
    with pytest.raises(RemoteAdapterError, match="lifetime"):
        session_key(expires_at=NOW + 12 * 60 * 60 + 1)
    with pytest.raises(AuthenticationError, match="does not match"):
        AuthenticatedEnvelopeCodec(
            binding(key_id="other-key-7f5c"), session_key(), now=lambda: NOW)

    future = codec(now=NOW - 1)
    with pytest.raises(AuthenticationError, match="not valid yet"):
        wire(future)

    expired = codec(now=KEY_EXPIRES)
    with pytest.raises(AuthenticationError, match="expired"):
        wire(expired)

    clock = [NOW]
    expiring = AuthenticatedEnvelopeCodec(
        binding(), session_key(), now=lambda: clock[0])
    sealed = wire(expiring)
    clock[0] = KEY_EXPIRES
    with pytest.raises(AuthenticationError, match="expired"):
        expiring.open(sealed, expected_direction=SOURCE_TO_VIEWER)

    key = session_key()
    active = codec(key=key)
    sealed = wire(active)
    key.revoke()
    assert key.revoked
    with pytest.raises(AuthenticationError, match="revoked"):
        active.open(sealed, expected_direction=SOURCE_TO_VIEWER)


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


def test_transport_loss_releases_all_held_input_and_disables_injection() -> None:
    life = RemoteProxyLifecycle()
    life.announce(epoch=1, source_revision=10)
    life.record_input(epoch=1, kind="key", code=42, pressed=True)
    life.record_input(epoch=1, kind="key", code=29, pressed=True)
    life.record_input(epoch=1, kind="button", code=272, pressed=True)

    releases = life.transport_lost()
    assert [(item.kind, item.code) for item in releases] == [
        ("button", 272), ("key", 29), ("key", 42),
    ]
    assert not life.input.held_keys
    assert not life.input.held_buttons
    with pytest.raises(AuthenticationError, match="disabled"):
        life.record_input(epoch=1, kind="key", code=42, pressed=False)


def test_reconnect_rejects_old_epoch_input_and_cleans_attached_state() -> None:
    life = RemoteProxyLifecycle()
    life.announce(epoch=3, source_revision=10)
    life.record_input(epoch=3, kind="key", code=56, pressed=True)

    releases = life.reconcile(
        epoch=4, source_revision=11, source_alive=True)
    assert [(item.kind, item.code) for item in releases] == [("key", 56)]
    with pytest.raises(ReplayError, match="input epoch is stale"):
        life.record_input(epoch=3, kind="key", code=56, pressed=False)
    life.record_input(epoch=4, kind="key", code=30, pressed=True)
    assert life.source_closed(source_revision=12)[0].code == 30


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
