"""Tests for the broker-owned browser-upload lineage chokepoint
(broker/qdistro_upload_lineage.py).

Covers: per-file receipts minted + sealed, the batch manifest shape (children
share the container chain_head, destination summary + per-child sealed
destination), each child verifies against the central store, and a denied flow
records audit but seals no receipts and emits no manifest.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

BROKER_DIR = Path(__file__).resolve().parents[2] / "broker"
if str(BROKER_DIR) not in sys.path:
    sys.path.insert(0, str(BROKER_DIR))

import qdistro_lineage_receipts as lr  # noqa: E402
import qdistro_upload_lineage as ul  # noqa: E402
from qdistro_lineage_store import Entity, LineageStore  # noqa: E402


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path):
    s = LineageStore(str(tmp_path / "lineage.db"), now=lambda: 1000)
    yield s
    s.close()


def _src(store, eid, data, *, guards=frozenset(), compartments=frozenset(),
         conflict_classes=frozenset()):
    """Record a source entity WITH its broker-derived security snapshot, then
    return an UploadFile referencing it by eid (the chokepoint reads the snapshot
    from the store, not from caller-supplied fields)."""
    store.record_entity(Entity(
        eid=eid, kind="file", guards=guards, compartments=compartments,
        conflict_classes=conflict_classes, created_at=1000,
    ))
    return ul.UploadFile(source_eid=eid, digest=_digest(data),
                         locator=f"https://x/{eid}")


DEST = "https://example.com/upload"


def test_record_upload_seals_per_file_receipts_and_manifest(store):
    files = [
        _src(store, "file:a", b"alpha"),
        _src(store, "file:b", b"beta"),
    ]
    res = ul.record_upload(store, files=files, destination=DEST,
                           agent_gid="silo:work", now=42)

    assert len(res.output_eids) == 2
    # Manifest shape: upload-receipt container with 2 children sharing one head.
    man = res.manifest
    assert man["schema"] == lr.UPLOAD_RECEIPT_SCHEMA
    assert man["destination"] == DEST
    assert len(man["receipts"]) == 2
    heads = {c["chain_head"] for c in man["receipts"]}
    assert len(heads) == 1
    assert man["chain_head"] in heads

    # Each child is sealed and verifies against the central store, and carries
    # the audit-significant destination in its sealed extra.
    for child in man["receipts"]:
        assert child["kind"] == "upload-receipt"
        assert child["extra"]["destination"] == DEST
        assert lr.verify_against_store(store, child) is True
        rows = store.receipts_for(child["entity"])
        assert [r["kind"] for r in rows] == ["upload-receipt"]
    assert store.verify_chain(store.chain_head()) is True


def test_uploaded_artifact_derives_from_source(store):
    files = [_src(store, "file:a", b"alpha")]
    res = ul.record_upload(store, files=files, destination=DEST, now=1)
    out = res.output_eids[0]
    assert "file:a" in store.upstream(out)


def test_local_only_file_denies_whole_batch(store):
    files = [
        _src(store, "file:clean", b"alpha"),
        _src(store, "file:secret", b"beta", guards=frozenset({"local-only"})),
    ]
    with pytest.raises(ul.UploadDenied) as ei:
        ul.record_upload(store, files=files, destination=DEST, now=1)
    assert "file:secret" in ei.value.denied_sources

    # The attempt is audited: both per-file upload activities are recorded...
    acts = store._conn.execute(
        "SELECT verdict FROM activities WHERE chokepoint='browser-upload' ORDER BY id"
    ).fetchall()
    assert ("deny",) in acts
    # ... but NO upload receipt was sealed for ANY file (the batch fails closed,
    # so even the clean member gets no sealed receipt implying it was sent).
    assert store.receipts_for("file:clean") == []
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE kind='upload-receipt'"
    ).fetchone()
    assert rows[0] == 0
    assert store.verify_chain(store.chain_head()) is True


def test_forged_destination_does_not_verify(store):
    files = [_src(store, "file:a", b"alpha")]
    res = ul.record_upload(store, files=files, destination=DEST, now=1)
    child = res.manifest["receipts"][0]
    forged = {**child, "extra": {"destination": "https://evil/"}}
    assert lr.verify_against_store(store, forged) is False


def test_unrecorded_source_rejected(store):
    # A source the store has never recorded cannot be uploaded with a sealed
    # receipt by naming it; the authoritative snapshot lives in the store.
    bad = ul.UploadFile(source_eid="file:never-recorded", digest=_digest(b"x"))
    with pytest.raises(ul.BadUploadInput):
        ul.record_upload(store, files=[bad], destination=DEST)
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE kind='upload-receipt'").fetchone()
    assert rows[0] == 0


def test_store_local_only_snapshot_denies_even_clean_caller(store):
    # The source is recorded local-only in the store; the UploadFile carries no
    # security fields, so the only snapshot is the store's. A remote upload of
    # local-only content is denied (caller cannot launder by omission).
    f = _src(store, "file:secret", b"beta", guards=frozenset({"local-only"}))
    with pytest.raises(ul.UploadDenied):
        ul.record_upload(store, files=[f], destination=DEST, now=1)
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE kind='upload-receipt'").fetchone()
    assert rows[0] == 0


def test_requires_at_least_one_file(store):
    with pytest.raises(ul.BadUploadInput):
        ul.record_upload(store, files=[], destination=DEST)


def test_rejects_bad_digest(store):
    store.record_entity(Entity(eid="file:a", kind="file", created_at=1000))
    bad = ul.UploadFile(source_eid="file:a", digest="nope")
    with pytest.raises(ul.BadUploadInput):
        ul.record_upload(store, files=[bad], destination=DEST)


def test_rejects_empty_destination(store):
    files = [_src(store, "file:a", b"alpha")]
    with pytest.raises(ul.BadUploadInput):
        ul.record_upload(store, files=files, destination="")
