"""Tests for the artifact-adjacent receipt surfaces (broker/qdistro_lineage_receipts.py).

Covers the codex GO-IF matrix: canonical bytes / idempotency, strict fail-closed
parsing (schema, missing fields, kind mismatch, wrong types, duplicate keys,
oversize, symlink refusal), the compact xattr pointer + soft-fail, the batch
export manifest with per-child chain_head equality, and verify-against-store.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os

import pytest
import qdistro_lineage_receipts as r
from qdistro_lineage_store import RECEIPT_NAMES, Entity, LineageStore


def _digest(b: bytes = b"hello") -> str:
    return hashlib.sha256(b).hexdigest()


def _env(**kw):
    base = dict(entity="file:secret", kind="sidecar", chain_head="deadbeef",
                created_at=1000, artifact_digest=_digest(), locator="a.txt")
    base.update(kw)
    return r.build_envelope(**base)


@pytest.fixture
def store(tmp_path):
    s = LineageStore(str(tmp_path / "lineage.db"), now=lambda: 1000)
    yield s
    s.close()


# --- build_envelope / validate --------------------------------------------


def test_build_envelope_has_reserved_signature_fields_null():
    env = _env()
    assert env["schema"] == r.RECEIPT_SCHEMA
    assert env["chain_algo"] == r.CHAIN_ALGO
    # signature trust is SEPARATE from the chain seal and reserved-null for now.
    assert env["signature_algo"] is None
    assert env["key_id"] is None
    assert env["signature"] is None


def test_build_envelope_rejects_bad_inputs():
    with pytest.raises(r.UnsupportedReceipt):
        _env(kind="not-a-kind")
    with pytest.raises(r.MalformedReceipt):
        _env(entity="")
    with pytest.raises(r.MalformedReceipt):
        _env(created_at="1000")
    with pytest.raises(r.MalformedReceipt):
        _env(created_at=True)  # bool is not an accepted int
    with pytest.raises(r.MalformedReceipt):
        _env(artifact_digest="nothex!!")


def test_build_envelope_allows_null_artifact_digest():
    env = _env(artifact_digest=None)
    assert env["artifact_digest"] is None


def test_build_envelope_rejects_non_json_extra():
    with pytest.raises(r.MalformedReceipt):
        _env(extra={"bad": {1, 2, 3}})  # a set is not JSON-native
    with pytest.raises(r.MalformedReceipt):
        _env(extra=["not", "a", "dict"])


def test_canonical_bytes_idempotent_and_sorted():
    env = _env()
    b1 = r.canonical_bytes(env)
    b2 = r.canonical_bytes(dict(reversed(list(env.items()))))
    assert b1 == b2  # key order does not change canonical bytes
    assert b"\n" not in b1 and b": " not in b1  # no insignificant whitespace
    assert b1.decode("utf-8")[0] == "{"


def test_validate_envelope_strict():
    good = _env()
    assert r.validate_envelope(good) is good
    with pytest.raises(r.MalformedReceipt):
        r.validate_envelope({"schema": r.RECEIPT_SCHEMA})  # missing fields
    bad_schema = {**good, "schema": "evil/v9"}
    with pytest.raises(r.MalformedReceipt):
        r.validate_envelope(bad_schema)
    bad_chain = {**good, "chain_algo": "rot13"}
    with pytest.raises(r.MalformedReceipt):
        r.validate_envelope(bad_chain)
    with pytest.raises(r.MalformedReceipt):
        r.validate_envelope(good, expected_kind="xattr")  # kind mismatch
    with pytest.raises(r.MalformedReceipt):
        r.validate_envelope("not a dict")


# --- sidecar ---------------------------------------------------------------


def test_sidecar_round_trip(tmp_path):
    art = tmp_path / "report.txt"
    art.write_bytes(b"hello")
    env = _env(locator=str(art))
    name = r.write_sidecar(str(art), env)
    assert name == str(art) + RECEIPT_NAMES["sidecar"]
    assert os.path.exists(name)
    got = r.read_sidecar(str(art))
    assert got == env


def test_sidecar_round_trip_via_dir_fd(tmp_path):
    art = tmp_path / "doc.bin"
    art.write_bytes(b"x")
    env = _env()
    dfd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        name = r.write_sidecar("doc.bin", env, dir_fd=dfd)
        assert name == "doc.bin" + RECEIPT_NAMES["sidecar"]
        got = r.read_sidecar("doc.bin", dir_fd=dfd)
    finally:
        os.close(dfd)
    assert got == env
    # the file actually landed in tmp_path
    assert (tmp_path / ("doc.bin" + RECEIPT_NAMES["sidecar"])).exists()


def test_sidecar_write_is_byte_canonical(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"")
    env = _env()
    name = r.write_sidecar(str(art), env)
    with open(name, "rb") as fh:
        on_disk = fh.read()
    assert on_disk == r.canonical_bytes(env)
    assert not on_disk.endswith(b"\n")


def test_sidecar_read_refuses_symlink(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"")
    side = str(art) + RECEIPT_NAMES["sidecar"]
    os.symlink(str(tmp_path / "elsewhere"), side)
    with pytest.raises(OSError):
        r.read_sidecar(str(art))  # O_NOFOLLOW -> ELOOP


def test_sidecar_write_over_symlink_replaces_not_follows(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"")
    target = tmp_path / "victim"
    target.write_bytes(b"original")
    side = str(art) + RECEIPT_NAMES["sidecar"]
    os.symlink(str(target), side)
    r.write_sidecar(str(art), _env())
    # the symlink was replaced by a real file; the victim is untouched
    assert not os.path.islink(side)
    assert target.read_bytes() == b"original"


def test_sidecar_read_rejects_oversize(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"")
    side = str(art) + RECEIPT_NAMES["sidecar"]
    with open(side, "wb") as fh:
        fh.write(b"{}" + b" " * (r.SIDECAR_MAX_BYTES + 10))
    with pytest.raises(r.MalformedReceipt):
        r.read_sidecar(str(art))


def test_sidecar_rejects_duplicate_keys(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"")
    side = str(art) + RECEIPT_NAMES["sidecar"]
    with open(side, "wb") as fh:
        fh.write(b'{"schema": "x", "schema": "y"}')
    with pytest.raises(r.MalformedReceipt):
        r.read_sidecar(str(art))


def test_sidecar_rejects_non_json(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"")
    side = str(art) + RECEIPT_NAMES["sidecar"]
    with open(side, "wb") as fh:
        fh.write(b"not json {")
    with pytest.raises(r.MalformedReceipt):
        r.read_sidecar(str(art))


# --- xattr -----------------------------------------------------------------


def _supports_xattr(path) -> bool:
    try:
        os.setxattr(path, "user.qdistro.probe", b"1", follow_symlinks=False)
        os.removexattr(path, "user.qdistro.probe", follow_symlinks=False)
        return True
    except OSError:
        return False


def test_xattr_pointer_is_compact_and_round_trips(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"hi")
    if not _supports_xattr(str(art)):
        pytest.skip("filesystem does not support user xattrs")
    env = _env()
    name = r.set_xattr(str(art), env)
    assert name == RECEIPT_NAMES["xattr"]
    ptr = r.read_xattr(str(art))
    assert ptr is not None
    assert set(ptr) == set(r._XATTR_KEYS)  # compact pointer only, no `extra`
    assert ptr["entity"] == env["entity"]
    assert ptr["artifact_digest"] == env["artifact_digest"]


def test_xattr_absent_returns_none(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"hi")
    if not _supports_xattr(str(art)):
        pytest.skip("filesystem does not support user xattrs")
    assert r.read_xattr(str(art)) is None


def test_xattr_set_soft_fails_on_oserror(tmp_path, monkeypatch):
    art = tmp_path / "a"
    art.write_bytes(b"hi")

    def boom(*a, **k):
        raise OSError(errno.E2BIG, "too big")

    monkeypatch.setattr(os, "setxattr", boom)
    assert r.set_xattr(str(art), _env()) is None  # soft-fail, no raise


def test_xattr_set_still_raises_on_malformed_envelope(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"hi")
    with pytest.raises(r.MalformedReceipt):
        r.set_xattr(str(art), {"schema": "bad"})


def test_xattr_present_but_malformed_raises(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"hi")
    if not _supports_xattr(str(art)):
        pytest.skip("filesystem does not support user xattrs")
    os.setxattr(str(art), RECEIPT_NAMES["xattr"], b'{"schema":"bad"}',
                follow_symlinks=False)
    with pytest.raises(r.MalformedReceipt):
        r.read_xattr(str(art))


def test_xattr_dir_fd_mode_round_trips(tmp_path):
    art = tmp_path / "a"
    art.write_bytes(b"hi")
    if not _supports_xattr(str(art)):
        pytest.skip("filesystem does not support user xattrs")
    dfd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        name = r.set_xattr("a", _env(), dir_fd=dfd)  # basename resolved at dir_fd
    finally:
        os.close(dfd)
    assert name == RECEIPT_NAMES["xattr"]
    ptr = r.read_xattr(str(art))
    assert ptr is not None and ptr["entity"] == _env()["entity"]


def test_xattr_dir_fd_mode_rejects_non_basename(tmp_path):
    dfd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError):
            r.set_xattr("sub/a", _env(), dir_fd=dfd)
    finally:
        os.close(dfd)


def test_xattr_dir_fd_mode_soft_fails_on_missing(tmp_path):
    # a missing target under the dir fd is opportunistic -> None, never raises
    dfd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert r.set_xattr("gone", _env(), dir_fd=dfd) is None
    finally:
        os.close(dfd)


# --- export manifest -------------------------------------------------------


def _child(eid, digest):
    return r.build_envelope(entity=eid, kind="export-manifest",
                            chain_head="head1", created_at=1000,
                            artifact_digest=digest, locator=eid)


def test_export_manifest_round_trip(tmp_path):
    envs = [_child("file:a", _digest(b"a")), _child("file:b", _digest(b"b"))]
    man = r.build_export_manifest(envs, chain_head="head1", created_at=1000)
    name = r.write_export_manifest(str(tmp_path), man)
    assert name.endswith(RECEIPT_NAMES["export-manifest"])
    got = r.read_export_manifest(str(tmp_path))
    assert got == man
    assert [c["entity"] for c in got["receipts"]] == ["file:a", "file:b"]


def test_export_manifest_round_trip_via_dir_fd(tmp_path):
    envs = [_child("file:a", _digest(b"a"))]
    man = r.build_export_manifest(envs, chain_head="head1", created_at=1000)
    dfd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        r.write_export_manifest("", man, dir_fd=dfd)
        got = r.read_export_manifest("", dir_fd=dfd)
    finally:
        os.close(dfd)
    assert got == man


def test_export_manifest_child_chain_head_must_match():
    bad = _child("file:a", _digest())
    bad = {**bad, "chain_head": "different"}
    with pytest.raises(r.MalformedReceipt):
        r.build_export_manifest([bad], chain_head="head1", created_at=1000)


def test_export_manifest_read_rejects_drifted_child(tmp_path):
    envs = [_child("file:a", _digest())]
    man = r.build_export_manifest(envs, chain_head="head1", created_at=1000)
    # tamper a child's chain_head after building, write raw, then read
    man["receipts"][0] = {**man["receipts"][0], "chain_head": "evil"}
    raw = json.dumps(man).encode("utf-8")
    path = tmp_path / RECEIPT_NAMES["export-manifest"]
    path.write_bytes(raw)
    with pytest.raises(r.MalformedReceipt):
        r.read_export_manifest(str(tmp_path))


def test_export_manifest_rejects_wrong_schema(tmp_path):
    path = tmp_path / RECEIPT_NAMES["export-manifest"]
    path.write_bytes(json.dumps({"schema": "evil", "receipts": []}).encode())
    with pytest.raises(r.MalformedReceipt):
        r.read_export_manifest(str(tmp_path))


def test_export_manifest_empty_is_valid(tmp_path):
    man = r.build_export_manifest([], chain_head="head1", created_at=1000)
    r.write_export_manifest(str(tmp_path), man)
    got = r.read_export_manifest(str(tmp_path))
    assert got["receipts"] == []


# --- verify_against_store --------------------------------------------------
#
# A receipt surface is attacker-writable in the Task-2 export model, so verify
# MUST require a SEALED receipt row, not merely a matching entity. The broker
# records the full envelope as the row payload, so the strong binding is a
# canonical-payload match.


def _record_entity_and_receipt(store, eid="file:x", digest=None, kind="sidecar"):
    """Record an entity + a sealed receipt row whose payload is the envelope, the
    way Task 2's broker will. Returns the matching envelope."""
    store.record_entity(Entity(eid=eid, kind="artifact", digest=digest,
                               created_at=1000))
    env = _env(entity=eid, kind=kind, artifact_digest=digest)
    store.record_receipt(entity=eid, kind=kind, digest=digest, payload=env)
    return env


def test_verify_true_on_matching_sealed_row(store):
    d = _digest(b"payload")
    env = _record_entity_and_receipt(store, digest=d)
    assert r.verify_against_store(store, env) is True


def test_verify_false_when_no_receipt_row(store):
    # entity exists, digest matches, but NO sealed receipt row -> not verified.
    d = _digest(b"payload")
    store.record_entity(Entity(eid="file:x", kind="artifact", digest=d,
                               created_at=1000))
    env = _env(entity="file:x", artifact_digest=d)
    assert r.verify_against_store(store, env) is False


def test_verify_false_when_entity_absent(store):
    env = _env(entity="file:missing", artifact_digest=_digest())
    assert r.verify_against_store(store, env) is False


def test_verify_false_on_forged_envelope_field(store):
    # A sealed row exists, but the attacker rewrote a field (locator) in the
    # surface -> the canonical payload no longer matches the sealed row.
    d = _digest(b"real")
    env = _record_entity_and_receipt(store, digest=d)
    forged = {**env, "locator": "evil.txt"}
    assert r.verify_against_store(store, forged) is False


def test_verify_false_on_digest_mismatch(store):
    d = _digest(b"real")
    _record_entity_and_receipt(store, digest=d)
    forged = _env(entity="file:x", artifact_digest=_digest(b"forged"))
    assert r.verify_against_store(store, forged) is False


def test_verify_false_on_digest_only_row(store):
    # A digest-only sealed row (no payload) does NOT authenticate the envelope's
    # metadata, so it must NOT verify as a full receipt (codex round-2 MAJOR).
    store.record_entity(Entity(eid="file:x", kind="artifact", digest=None,
                               created_at=1000))
    d = _digest(b"viareceipt")
    store.record_receipt(entity="file:x", kind="sidecar", digest=d)
    env = _env(entity="file:x", artifact_digest=d)
    assert r.verify_against_store(store, env) is False


def test_verify_false_when_digest_only_row_and_metadata_forged(store):
    # The exact attack codex flagged: a reachable digest-only row + an envelope
    # with the same (entity, kind, digest) but forged locator/chain_head must NOT
    # verify, because the metadata was never sealed.
    d = _digest(b"real")
    store.record_entity(Entity(eid="file:x", kind="artifact", digest=d,
                               created_at=1000))
    store.record_receipt(entity="file:x", kind="sidecar", digest=d)  # no payload
    forged = _env(entity="file:x", artifact_digest=d, locator="evil.txt",
                  chain_head="forged-head")
    assert r.verify_against_store(store, forged) is False


def test_verify_requires_kind_match_even_with_payload(store):
    # A sealed sidecar row must not authenticate an export-manifest envelope.
    d = _digest(b"k")
    env = _record_entity_and_receipt(store, digest=d, kind="sidecar")
    other = {**env, "kind": "export-manifest"}
    assert r.verify_against_store(store, other) is False


def test_verify_null_digest_needs_a_null_digest_sealed_row(store):
    # A content-free receipt verifies ONLY against a sealed row that also
    # recorded a null digest; stripping the digest does not fail open.
    env = _record_entity_and_receipt(store, digest=None)
    assert r.verify_against_store(store, env) is True


def test_verify_false_when_null_digest_stripped_from_digested_entity(store):
    # entity has a real digest + a sealed digested receipt; an attacker strips
    # the surface digest to null hoping to bypass content binding -> no matching
    # null-digest row, so it fails closed.
    d = _digest(b"real")
    _record_entity_and_receipt(store, digest=d)
    stripped = _env(entity="file:x", artifact_digest=None)
    assert r.verify_against_store(store, stripped) is False


def test_verify_checks_expected_chain_head(store):
    d = _digest(b"p")
    env = _record_entity_and_receipt(store, digest=d)
    head = store.chain_head()
    assert r.verify_against_store(store, env, expected_chain_head=head) is True
    assert r.verify_against_store(store, env,
                                  expected_chain_head="wrong-head") is False


# --- dir_fd traversal confinement -----------------------------------------


@pytest.mark.parametrize("evil", ["../outside", "sub/x", "..", ".", "",
                                  "a/../../b", "/abs"])
def test_dir_fd_rejects_non_basename(tmp_path, evil):
    dfd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError):
            r.write_sidecar(evil, _env(), dir_fd=dfd)
        with pytest.raises(ValueError):
            r.read_sidecar(evil, dir_fd=dfd)
    finally:
        os.close(dfd)


def test_dir_fd_read_does_not_follow_intermediate_symlink(tmp_path):
    # plant an attacker symlink dir and a secret outside the rooted tree
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"top secret")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(str(outside), str(root / "link"))
    dfd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        # "link/secret" must be rejected as a non-basename, never traversed
        with pytest.raises(ValueError):
            r.read_sidecar("link/secret", dir_fd=dfd)
    finally:
        os.close(dfd)


# --- signature fields must be null in v1 ----------------------------------


def test_validate_rejects_non_null_signature_fields():
    env = _env()
    for field in ("signature_algo", "key_id", "signature"):
        forged = {**env, field: "forged"}
        with pytest.raises(r.MalformedReceipt):
            r.validate_envelope(forged)


# --- digest grammar (single canonical spelling) ---------------------------


def test_digest_grammar_is_strict():
    assert r._is_hex_digest(hashlib.sha256(b"x").hexdigest()) is True
    assert r._is_hex_digest(hashlib.sha256(b"x").hexdigest().upper()) is False
    assert r._is_hex_digest("sha256:" + hashlib.sha256(b"x").hexdigest()) is False
    assert r._is_hex_digest("deadbeef") is False  # too short
    assert r._is_hex_digest("g" * 64) is False    # non-hex


# --- manifest oversize on read --------------------------------------------


def test_manifest_read_rejects_oversize(tmp_path):
    path = tmp_path / RECEIPT_NAMES["export-manifest"]
    # a valid empty manifest declares 0 children, so the proportional budget is
    # BASE + 1*PER; pad well past it but under the hard ceiling.
    man = r.build_export_manifest([], chain_head="head1", created_at=1000)
    blob = json.dumps(man)
    pad = " " * (r.MANIFEST_BASE_BYTES + r.MANIFEST_PER_RECEIPT_BYTES + 1000)
    path.write_bytes((blob[:-1] + ',"_pad":"' + pad + '"}').encode("utf-8"))
    with pytest.raises(r.MalformedReceipt):
        r.read_export_manifest(str(tmp_path))


# --- xattr edge cases ------------------------------------------------------


def test_read_xattr_rejects_oversize(tmp_path, monkeypatch):
    art = tmp_path / "a"
    art.write_bytes(b"hi")

    def big(*a, **k):
        return b"{" + b"x" * (r.XATTR_MAX_BYTES + 1)

    monkeypatch.setattr(os, "getxattr", big)
    with pytest.raises(r.MalformedReceipt):
        r.read_xattr(str(art))


def test_read_xattr_rejects_type_malformed(tmp_path, monkeypatch):
    art = tmp_path / "a"
    art.write_bytes(b"hi")
    bad = {"schema": r.RECEIPT_SCHEMA, "kind": "sidecar", "entity": "file:x",
           "artifact_digest": None, "chain_head": "h", "issuer": "i",
           "created_at": "not-an-int"}  # created_at wrong type

    def fake(*a, **k):
        return json.dumps(bad).encode("utf-8")

    monkeypatch.setattr(os, "getxattr", fake)
    with pytest.raises(r.MalformedReceipt):
        r.read_xattr(str(art))


def test_read_xattr_rejects_extra_keys(tmp_path, monkeypatch):
    art = tmp_path / "a"
    art.write_bytes(b"hi")
    pointer = {k: _env()[k] for k in r._XATTR_KEYS}
    pointer["signature"] = "FORGED"  # smuggle an extra key

    def fake(*a, **k):
        return json.dumps(pointer).encode("utf-8")

    monkeypatch.setattr(os, "getxattr", fake)
    with pytest.raises(r.MalformedReceipt):
        r.read_xattr(str(art))


def test_read_xattr_unsupported_returns_none(tmp_path, monkeypatch):
    art = tmp_path / "a"
    art.write_bytes(b"hi")

    def enotsup(*a, **k):
        raise OSError(errno.ENOTSUP, "nope")

    monkeypatch.setattr(os, "getxattr", enotsup)
    assert r.read_xattr(str(art)) is None


def test_read_xattr_eperm_surfaces(tmp_path, monkeypatch):
    art = tmp_path / "a"
    art.write_bytes(b"hi")

    def eperm(*a, **k):
        raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(os, "getxattr", eperm)
    with pytest.raises(OSError):
        r.read_xattr(str(art))  # EPERM is a real signal, not "absent"
