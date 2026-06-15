"""Tests for the broker-owned git-commit lineage chokepoint
(broker/qdistro_commit_lineage.py).

Covers: a receipt is minted + sealed, the Qdistro-Lineage trailer is appended to
the message (and parses back + verifies against the sealed central row), the
receipt is sealed BEFORE the message is assembled/signed (ordering), guarded
sources propagate, and a denied flow records the audit but emits no trailer.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

BROKER_DIR = Path(__file__).resolve().parents[2] / "broker"
if str(BROKER_DIR) not in sys.path:
    sys.path.insert(0, str(BROKER_DIR))

import qdistro_commit_lineage as cl  # noqa: E402
import qdistro_lineage_receipts as lr  # noqa: E402
from qdistro_lineage_store import Entity, LineageStore  # noqa: E402


def _digest(data: bytes = b"tree") -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path):
    s = LineageStore(str(tmp_path / "lineage.db"), now=lambda: 1000)
    yield s
    s.close()


def _source(store, eid="file:work-note", *, guards=frozenset(),
            compartments=frozenset(), conflict_classes=frozenset()):
    """Record a source entity WITH its broker-derived security snapshot (the
    authoritative snapshot the chokepoint reads), then return a CommitSource that
    references it by eid only."""
    store.record_entity(Entity(
        eid=eid, kind="file", guards=guards, compartments=compartments,
        conflict_classes=conflict_classes, created_at=1000,
    ))
    return cl.CommitSource(eid=eid)


def test_record_commit_mints_receipt_and_appends_trailer(store):
    src = _source(store)
    eid = cl.new_commit_eid()
    td = _digest()
    res = cl.record_commit(
        store,
        message="lineage: wire commit chokepoint\n\nWhy body here.",
        commit_eid=eid,
        sources=[src],
        tree_digest=td,
        branch="claude/topic",
        agent_gid="silo:work",
        now=1234,
    )
    # The commit entity exists and is derived from the source.
    assert store.get_entity(eid) is not None
    assert src.eid in store.upstream(eid)
    # A git-trailer receipt row was sealed for the commit entity.
    rows = store.receipts_for(eid)
    assert [r["kind"] for r in rows] == ["git-trailer"]
    # The trailer is present in the returned message and parses back to the
    # sealed envelope, which verifies against the central store.
    parsed = lr.parse_git_trailer(res.message)
    assert len(parsed) == 1
    assert parsed[0] == res.receipt
    assert lr.verify_against_store(store, parsed[0]) is True
    assert store.verify_chain(store.chain_head()) is True


def test_trailer_envelope_binds_digest_and_branch(store):
    src = _source(store)
    eid = cl.new_commit_eid()
    td = _digest(b"specific-tree")
    res = cl.record_commit(
        store, message="subject", commit_eid=eid, sources=[src],
        tree_digest=td, branch="main", now=7,
    )
    env = res.receipt
    assert env["entity"] == eid
    assert env["artifact_digest"] == td
    assert env["locator"] == "main"
    assert env["issuer"] == cl.LINEAGE_ISSUER
    assert env["kind"] == "git-trailer"


def test_receipt_sealed_before_message_assembled(store):
    """The sealed row must exist for verify_against_store to pass on the trailer
    we hand back to be signed — i.e. the receipt is minted+sealed BEFORE the
    message (the thing that gets signed) is finalized. We assert the row is
    present and the returned message's trailer already verifies."""
    src = _source(store)
    eid = cl.new_commit_eid()
    res = cl.record_commit(
        store, message="subj", commit_eid=eid, sources=[src],
        tree_digest=_digest(), now=1,
    )
    # The trailer carried by the to-be-signed message verifies NOW (the row was
    # sealed before the message came back), so signing covers a verifiable
    # receipt rather than one sealed afterwards.
    parsed = lr.parse_git_trailer(res.message)
    assert lr.verify_against_store(store, parsed[0]) is True


def test_guarded_source_propagates_to_commit(store):
    src = _source(store, eid="file:secret",
                  compartments=frozenset({"work"}))
    eid = cl.new_commit_eid()
    cl.record_commit(
        store, message="s", commit_eid=eid, sources=[src],
        tree_digest=_digest(), now=1,
    )
    ent = store.get_entity(eid)
    assert "work" in ent.compartments


def test_local_only_source_allowed_locally(store):
    # A commit is assembled+signed locally, so local-only content may flow into
    # it (local-only forbids REMOTE egress, not a local commit). The guard
    # inherits virally onto the commit entity.
    src = _source(store, eid="file:local-secret",
                  guards=frozenset({"local-only"}))
    eid = cl.new_commit_eid()
    res = cl.record_commit(
        store, message="s", commit_eid=eid, sources=[src],
        tree_digest=_digest(), now=1,
    )
    assert "local-only" in store.get_entity(eid).guards
    assert lr.verify_against_store(store, res.receipt) is True


def test_cross_compartment_commit_denied_no_trailer(store):
    # A work-compartment source committed into a home-compartment repo under the
    # home-work-separation conflict class is denied (doc/guards.md cross-silo).
    src = _source(store, eid="file:work-secret",
                  guards=frozenset({"no-cross-contaminate"}),
                  compartments=frozenset({"work"}),
                  conflict_classes=frozenset({"home-work-separation"}))
    eid = cl.new_commit_eid()
    with pytest.raises(cl.CommitDenied):
        cl.record_commit(
            store, message="s", commit_eid=eid, sources=[src],
            tree_digest=_digest(),
            dest_compartments=["home"],
            dest_conflict_classes=["home-work-separation"],
            now=1,
        )
    # The attempt is audited (a denied commit activity exists)...
    acts = store._conn.execute(
        "SELECT chokepoint, verdict FROM activities WHERE chokepoint='commit-create'"
    ).fetchall()
    assert acts == [("commit-create", "deny")]
    # ... but no commit entity and no sealed receipt were minted.
    assert store.get_entity(eid) is None
    assert store.receipts_for(eid) == []
    assert store.verify_chain(store.chain_head()) is True


def test_unrecorded_source_rejected(store):
    # A source the store has never recorded cannot be laundered into a commit by
    # naming it; the authoritative snapshot lives in the store.
    src = cl.CommitSource(eid="file:never-recorded")
    eid = cl.new_commit_eid()
    with pytest.raises(cl.BadCommitInput):
        cl.record_commit(store, message="s", commit_eid=eid, sources=[src],
                         tree_digest=_digest())
    assert store.get_entity(eid) is None
    assert store.receipts_for(eid) == []


def test_store_snapshot_wins_over_clean_caller(store):
    # The source is recorded with local-only in the store. The CommitSource
    # carries no security fields at all, so the only snapshot is the store's —
    # it inherits onto the commit (caller cannot launder by omission).
    _source(store, eid="file:guarded", guards=frozenset({"local-only"}))
    eid = cl.new_commit_eid()
    cl.record_commit(store, message="s", commit_eid=eid,
                     sources=[cl.CommitSource(eid="file:guarded")],
                     tree_digest=_digest(), now=1)
    assert "local-only" in store.get_entity(eid).guards


def test_requires_at_least_one_source(store):
    with pytest.raises(cl.BadCommitInput):
        cl.record_commit(store, message="s", commit_eid=cl.new_commit_eid(),
                         sources=[], tree_digest=_digest())


def test_rejects_duplicate_source(store):
    src = _source(store)
    with pytest.raises(cl.BadCommitInput):
        cl.record_commit(store, message="s", commit_eid=cl.new_commit_eid(),
                         sources=[src, src], tree_digest=_digest())


def test_rejects_bad_tree_digest(store):
    src = _source(store)
    with pytest.raises(cl.BadCommitInput):
        cl.record_commit(store, message="s", commit_eid=cl.new_commit_eid(),
                         sources=[src], tree_digest="not-a-digest")
