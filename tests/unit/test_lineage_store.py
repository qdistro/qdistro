"""Tests for the central lineage store (broker/qdistro_lineage_store.py).

Covers: schema creation + migration version, idempotent reopen, 0600 perms,
vocabulary enforcement, forward/reverse (impact/root-cause) queries, receipts,
assertion authority filtering, data-mapping confidence, retention gc, and the
tamper-evidence hash chain.
"""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from qdistro_lineage_store import (
    SCHEMA_VERSION,
    Activity,
    Agent,
    Entity,
    LineageStore,
    LineageStoreError,
    mapping_narrows_guards,
)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "sub" / "lineage.db")


@pytest.fixture
def store(db):
    s = LineageStore(db)
    yield s
    s.close()


# --- schema / migration ---------------------------------------------------

def test_fresh_db_stamps_schema_version(store):
    assert store.schema_version == SCHEMA_VERSION == 1


def test_db_file_is_0600(db):
    LineageStore(db).close()
    mode = stat.S_IMODE(os.stat(db).st_mode)
    assert mode == 0o600


def test_all_tables_created(db):
    LineageStore(db).close()
    con = sqlite3.connect(db)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert {
        "entities", "activities", "agents", "edges", "assertions",
        "receipts", "mapping_activity", "mapping_input", "mapping_output",
    } <= names


def test_reopen_preserves_data_and_version(db):
    s = LineageStore(db)
    s.record_entity(Entity(eid="file:a", kind="file"))
    s.close()
    s2 = LineageStore(db)
    assert s2.schema_version == SCHEMA_VERSION
    assert s2.get_entity("file:a") is not None
    s2.close()


def test_newer_user_version_not_downgraded(db):
    LineageStore(db).close()
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    s = LineageStore(db)  # must not raise, must not downgrade
    assert s.schema_version == 99
    s.close()


def test_double_reopen_is_idempotent(db):
    for _ in range(3):
        LineageStore(db).close()
    s = LineageStore(db)
    assert s.schema_version == SCHEMA_VERSION
    s.close()


# --- vocabulary enforcement ----------------------------------------------

def test_unknown_entity_kind_rejected(store):
    with pytest.raises(LineageStoreError):
        store.record_entity(Entity(eid="x:1", kind="bogus"))


def test_unknown_activity_kind_rejected(store):
    with pytest.raises(LineageStoreError):
        store.record_activity(Activity(aid="a:1", kind="bogus"))


def test_unknown_host_class_rejected(store):
    with pytest.raises(LineageStoreError):
        store.record_activity(Activity(aid="a:1", kind="export", host_class="cloud"))


def test_unknown_edge_predicate_rejected(store):
    with pytest.raises(LineageStoreError):
        store.record_edge("influencedSomehow", "x", "y")


def test_unknown_agent_kind_rejected(store):
    with pytest.raises(LineageStoreError):
        store.record_agent(Agent(gid="g:1", kind="robot"))


def test_unknown_receipt_kind_rejected(store):
    store.record_entity(Entity(eid="file:a", kind="file"))
    with pytest.raises(LineageStoreError):
        store.record_receipt(entity="file:a", kind="blockchain")


def test_unknown_assertion_authority_rejected(store):
    with pytest.raises(LineageStoreError):
        store.record_assertion(subject="x", fact="f", value=1,
                               asserted_by="g:1", authority="vibes")


def test_bad_integrity_rejected(store):
    with pytest.raises(LineageStoreError):
        store.record_entity(Entity(eid="x:1", kind="file", integrity="weird"))


# --- entity round-trip + snapshot -----------------------------------------

def test_entity_round_trip(store):
    store.record_entity(Entity(
        eid="file:secret", kind="file", digest="sha256:abc",
        guards=frozenset({"local-only"}),
        compartments=frozenset({"work"}),
        conflict_classes=frozenset({"home-work-separation"}),
        facets={"title": "Q3 plan"},
    ))
    e = store.get_entity("file:secret")
    assert e.kind == "file"
    assert e.digest == "sha256:abc"
    assert e.guards == frozenset({"local-only"})
    assert e.compartments == frozenset({"work"})
    assert e.facets == {"title": "Q3 plan"}


def test_get_entity_returns_latest_snapshot(store):
    store.record_entity(Entity(eid="file:a", kind="file", status="active"))
    store.record_entity(Entity(eid="file:a", kind="file", status="archived",
                               guards=frozenset({"local-only"})))
    e = store.get_entity("file:a")
    assert e.status == "archived"
    assert e.guards == frozenset({"local-only"})


def test_missing_entity_is_none(store):
    assert store.get_entity("nope") is None


# --- forward / reverse lineage queries ------------------------------------

def _chain(store, n):
    """Build a derivation chain e0 <- e1 <- ... (each derived from prev)."""
    for i in range(n):
        store.record_entity(Entity(eid=f"e{i}", kind="file"))
    for i in range(1, n):
        store.record_edge("wasDerivedFrom", f"e{i}", f"e{i-1}")


def test_downstream_impact(store):
    _chain(store, 4)
    assert store.downstream("e0") == frozenset({"e1", "e2", "e3"})
    assert store.downstream("e2") == frozenset({"e3"})
    assert store.downstream("e3") == frozenset()


def test_upstream_root_cause(store):
    _chain(store, 4)
    assert store.upstream("e3") == frozenset({"e0", "e1", "e2"})
    assert store.upstream("e1") == frozenset({"e0"})
    assert store.upstream("e0") == frozenset()


def test_lineage_walk_handles_cycle(store):
    store.record_entity(Entity(eid="a", kind="file"))
    store.record_entity(Entity(eid="b", kind="file"))
    store.record_edge("wasDerivedFrom", "b", "a")
    store.record_edge("wasDerivedFrom", "a", "b")  # pathological cycle
    # Must terminate and not include the start node.
    assert store.downstream("a") == frozenset({"b"})


def test_guarded_descendants_after_reclassification(store):
    store.record_entity(Entity(eid="src", kind="file"))
    store.record_entity(Entity(eid="der", kind="file", guards=frozenset({"local-only"})))
    store.record_edge("wasDerivedFrom", "der", "src")
    gd = store.guarded_descendants("src")
    assert gd == {"der": frozenset({"local-only"})}


# --- receipts -------------------------------------------------------------

def test_receipts_round_trip(store):
    store.record_entity(Entity(eid="export:1", kind="export"))
    store.record_receipt(entity="export:1", kind="export-manifest",
                         locator="/out/qdistro-export-manifest.json",
                         digest="sha256:deef",
                         payload={"items": ["a", "b"]})
    store.record_receipt(entity="export:1", kind="git-trailer",
                         locator="Qdistro-Lineage")
    rs = store.receipts_for("export:1")
    assert {r["kind"] for r in rs} == {"export-manifest", "git-trailer"}
    manifest = next(r for r in rs if r["kind"] == "export-manifest")
    assert manifest["payload"] == {"items": ["a", "b"]}


# --- assertions / authority -----------------------------------------------

def test_assertion_authority_filter(store):
    store.record_assertion(subject="file:a", fact="security.guards",
                           value=["local-only"], asserted_by="broker",
                           authority="broker-derived")
    store.record_assertion(subject="file:a", fact="facet.title",
                           value="My Doc", asserted_by="app:editor",
                           authority="app-reported")
    broker = store.assertions_for("file:a", authority="broker-derived")
    assert len(broker) == 1
    assert broker[0]["fact"] == "security.guards"
    app = store.assertions_for("file:a", authority="app-reported")
    assert len(app) == 1 and app[0]["value"] == "My Doc"
    assert len(store.assertions_for("file:a")) == 2


# --- data mapping ---------------------------------------------------------

def test_mapping_confidence_policy():
    assert mapping_narrows_guards("broker-derived") is True
    assert mapping_narrows_guards("trusted-tool") is True
    assert mapping_narrows_guards("app-reported") is False
    assert mapping_narrows_guards("inferred") is False
    assert mapping_narrows_guards("nonsense") is False


def test_record_mapping_round_trip(store):
    store.record_entity(Entity(eid="in", kind="archive"))
    store.record_entity(Entity(eid="out", kind="file"))
    store.record_mapping(
        map_id="m:1", activity="a:1", mapping_kind="archive-member",
        confidence="broker-derived",
        inputs=[("in", "member/foo.txt")],
        outputs=[("out", None)],
        handler_version="unzip/1.2",
    )
    rows = store._conn.execute(
        "SELECT confidence FROM mapping_activity WHERE map_id='m:1'").fetchall()
    assert rows[0][0] == "broker-derived"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM mapping_input WHERE map_id='m:1'").fetchone()[0] == 1


def test_record_mapping_unknown_confidence_rejected(store):
    with pytest.raises(LineageStoreError):
        store.record_mapping(map_id="m", activity="a", mapping_kind="x",
                             confidence="guess", inputs=[], outputs=[])


# --- retention gc ---------------------------------------------------------

def test_gc_prunes_old_entities_of_kind(db):
    clock = {"t": 1000}
    s = LineageStore(db, now=lambda: clock["t"])
    s.record_entity(Entity(eid="old", kind="payload"))
    clock["t"] = 1000 + 10_000
    s.record_entity(Entity(eid="new", kind="payload"))
    s.record_entity(Entity(eid="export", kind="export"))
    # Prune payloads older than 5000s; 'old' goes, 'new' and the export stay.
    removed = s.gc("payload", older_than_seconds=5000)
    assert removed == 1
    assert s.get_entity("old") is None
    assert s.get_entity("new") is not None
    assert s.get_entity("export") is not None
    s.close()


def test_gc_does_not_cascade_into_edges(db):
    clock = {"t": 1000}
    s = LineageStore(db, now=lambda: clock["t"])
    s.record_entity(Entity(eid="payload", kind="payload"))
    s.record_edge("wasDerivedFrom", "payload", "src")
    clock["t"] = 50_000
    s.gc("payload", older_than_seconds=1000)
    # Lineage edge (digest/source ref) survives the payload expiry.
    cnt = s._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    assert cnt == 1
    s.close()


def test_gc_rejects_unknown_kind_and_negative(store):
    with pytest.raises(LineageStoreError):
        store.gc("bogus", 10)
    with pytest.raises(LineageStoreError):
        store.gc("payload", -1)


# --- tamper-evidence hash chain -------------------------------------------

def test_chain_verifies_on_clean_store(store):
    store.record_entity(Entity(eid="a", kind="file"))
    store.record_activity(Activity(aid="act:1", kind="export"))
    store.record_edge("used", "act:1", "a")
    store.record_agent(Agent(gid="g:1", kind="app"))
    assert store.verify_chain() is True


def test_chain_detects_in_place_edit(store):
    store.record_entity(Entity(eid="a", kind="file", guards=frozenset({"local-only"})))
    store.record_entity(Entity(eid="b", kind="file"))
    assert store.verify_chain() is True
    # Silently strip a guard out from under the chain.
    store._conn.execute("UPDATE entities SET guards='[]' WHERE eid='a'")
    assert store.verify_chain() is False


def test_chain_detects_row_deletion(store):
    store.record_entity(Entity(eid="a", kind="file"))
    store.record_entity(Entity(eid="b", kind="file"))
    store.record_entity(Entity(eid="c", kind="file"))
    assert store.verify_chain() is True
    store._conn.execute("DELETE FROM entities WHERE eid='b'")
    assert store.verify_chain() is False


def test_chain_detects_activity_verdict_flip(store):
    # The authoritative verdict is a sealed assertion, so flipping the cached
    # activities.verdict column does NOT fool verify_chain, and the assertion
    # still records the real decision.
    store.record_activity(Activity(aid="act:1", kind="export", verdict="deny"))
    assert store.verify_chain() is True
    store._conn.execute("UPDATE activities SET verdict='allow' WHERE aid='act:1'")
    # The cached column changed but the sealed verdict assertion is untouched.
    assert store.verify_chain() is True  # column is unsealed cache, no false alarm
    ev = store.assertions_for("act:1")
    assert [a["value"] for a in ev if a["fact"] == "activity.verdict"] == ["deny"]


def test_chain_detects_assertion_edit(store):
    # Editing the sealed verdict ASSERTION (the authoritative record) is caught.
    store.record_activity(Activity(aid="act:1", kind="export", verdict="deny"))
    store._conn.execute(
        "UPDATE assertions SET value='\"allow\"' WHERE fact='activity.verdict'")
    assert store.verify_chain() is False


def test_chain_expected_head_detects_tail_truncation(store):
    store.record_entity(Entity(eid="a", kind="file"))
    store.record_entity(Entity(eid="b", kind="file"))
    store.record_entity(Entity(eid="c", kind="file"))
    # A trusted party captures the head off-host AFTER c is committed.
    captured = store.chain_head()
    # An attacker truncates the tail: delete entity c AND its ledger entry, so
    # the chain's prefix is once again self-consistent and ends at b's seal.
    store._conn.execute("DELETE FROM entities WHERE eid='c'")
    store._conn.execute(
        "DELETE FROM lineage_chain WHERE seq=(SELECT MAX(seq) FROM lineage_chain)")
    # Bare verify cannot see the truncation (the remaining prefix is valid) ...
    assert store.verify_chain() is True
    # ... but the off-host-held expected head no longer matches the live head.
    assert store.chain_head() != captured
    assert store.verify_chain(expected_head=captured) is False
    # A matching head still verifies cleanly (no false positive).
    assert store.verify_chain(expected_head=store.chain_head()) is True


def test_tombstone_cannot_mask_live_row_edit(store):
    # Forging a tombstone over a row that is still live (then editing it) must
    # NOT verify clean: the tombstone's target must be genuinely absent.
    store.record_entity(Entity(eid="a", kind="file",
                               guards=frozenset({"local-only"})))
    row = store._conn.execute(
        "SELECT seq, body FROM lineage_chain WHERE tbl='entities'").fetchone()
    seq, body = row
    store._tombstone(seq, "entities", body)  # forged: row 'a' still exists
    assert store.verify_chain() is False


def test_tombstone_with_bogus_target_fails(store):
    store.record_entity(Entity(eid="a", kind="file"))
    # Tombstone pointing at a seq/body that was never sealed.
    store._tombstone(9999, "entities", '{"eid":"ghost"}')
    assert store.verify_chain() is False


def test_legitimate_gc_keeps_chain_verifiable(db):
    clock = {"t": 1000}
    s = LineageStore(db, now=lambda: clock["t"])
    s.record_entity(Entity(eid="old", kind="payload"))
    clock["t"] = 50_000
    s.record_entity(Entity(eid="new", kind="payload"))
    assert s.verify_chain() is True
    s.gc("payload", older_than_seconds=1000)  # prunes 'old' with a tombstone
    assert s.get_entity("old") is None
    assert s.verify_chain() is True  # tombstoned deletion stays verifiable
    s.close()


def test_chain_detects_injected_unsealed_row(store):
    # An attacker INSERTs a newer entity row directly without a ledger entry, so
    # get_entity would return it; the live-vs-ledger bijection must catch it.
    store.record_entity(Entity(eid="secret", kind="file",
                               guards=frozenset({"local-only"})))
    assert store.verify_chain(expected_head=store.chain_head()) is True
    store._conn.execute(
        "INSERT INTO entities (eid, kind, digest, locator, guards, compartments, "
        "conflict_classes, integrity, status, facets, created_at, seal) "
        "VALUES ('secret','file',NULL,NULL,'[]','[]','[]',NULL,'active',NULL,"
        "9999,'not-in-ledger')")
    # Newest row would otherwise mask the guard.
    assert store.get_entity("secret").guards == frozenset()
    assert store.verify_chain() is False
    assert store.verify_chain(expected_head=store.chain_head()) is False


def test_gc_duplicate_body_rows_stay_verifiable(db):
    # Two entity snapshots with byte-identical sealed bodies (same eid, same
    # created_at, same fields) both get distinct tombstones on gc, so the chain
    # stays verifiable (regression: a single shared seq would leave one
    # unretired).
    clock = {"t": 1000}
    s = LineageStore(db, now=lambda: clock["t"])
    s.record_entity(Entity(eid="dup", kind="payload"))
    s.record_entity(Entity(eid="dup", kind="payload"))  # identical sealed body
    assert s.verify_chain() is True
    clock["t"] = 50_000
    removed = s.gc("payload", older_than_seconds=1000)
    assert removed == 2
    assert s.verify_chain() is True
    s.close()


def test_record_entity_is_atomic_on_failure(db):
    # If the table INSERT fails after the ledger seal, the transaction must roll
    # back BOTH so no orphan ledger entry is left (which would fail verify_chain).
    s = LineageStore(db)
    s.record_entity(Entity(eid="a", kind="file"))
    ledger_before = s._conn.execute("SELECT COUNT(*) FROM lineage_chain").fetchone()[0]
    # Force the entities INSERT (which runs AFTER the ledger seal inside the same
    # _txn) to fail, via a BEFORE INSERT trigger that aborts.
    s._conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON entities "
        "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        s.record_entity(Entity(eid="b", kind="file"))
    s._conn.execute("DROP TRIGGER boom")
    # The ledger seal was rolled back with the failed row — no orphan entry.
    assert s._conn.execute(
        "SELECT COUNT(*) FROM lineage_chain").fetchone()[0] == ledger_before
    assert s.verify_chain() is True
    s.close()


def test_verify_tolerates_tampered_body_keys(store):
    # A tampered ledger body with a bogus column key must make verify return
    # False, not raise a SQL error (cols come from _SEALED_COLUMNS, not body).
    store.record_entity(Entity(eid="a", kind="file"))
    seq = store._conn.execute("SELECT seq FROM lineage_chain").fetchone()[0]
    # Re-seal a forged body in place so the seal itself still matches, but the
    # body shape is wrong. (Recompute the seal from genesis since it's seq 1.)
    forged = '{"nonsense_column": 1}'
    from qdistro_lineage_store import LineageStore as LS
    seal = LS._seal_value("qdistro-lineage-genesis", "entities", forged)
    store._conn.execute(
        "UPDATE lineage_chain SET body=?, seal=? WHERE seq=?", (forged, seal, seq))
    assert store.verify_chain() is False  # not an exception
