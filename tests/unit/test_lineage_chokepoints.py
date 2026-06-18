"""Tests for chokepoint guard propagation into lineage (broker/qdistro_lineage.py).

These cover the four scenarios the todo calls out:
- local-only: guarded data denied at a remote-egress chokepoint, allowed locally;
- no-cross-contaminate: cross-compartment flow denied, same-compartment allowed,
  combining compartments marks the output 'mixed';
- data-mapping fallback: an untrusted mapping does NOT narrow guard propagation,
  a trusted (broker-derived) mapping may;
- declassification: an authority-bearing workflow produces a tracked derivative
  whose guard set is narrowed while the source stays guarded.
"""
from __future__ import annotations

import pytest

from qdistro_guard_registry import FlowEndpoint, GuardVerdict, ProcessingDescriptor
from qdistro_lineage import (
    ChokepointInput,
    declassify,
    record_chokepoint,
    report_processing_host,
)
from qdistro_lineage_store import Entity, LineageStore


@pytest.fixture
def store(tmp_path):
    s = LineageStore(str(tmp_path / "lineage.db"), now=lambda: 1000)
    yield s
    s.close()


def _local_only_input(eid="file:secret"):
    return ChokepointInput(eid=eid, endpoint=FlowEndpoint(guards=frozenset({"local-only"})))


# --- local-only -----------------------------------------------------------

def test_local_only_denied_on_remote_upload(store):
    res = record_chokepoint(
        store, chokepoint="browser-upload", inputs=[_local_only_input()],
    )
    assert res.verdict is GuardVerdict.DENY
    assert res.denied
    assert res.output_eid is None  # no clean derivative minted on deny
    # ... but the attempt is still recorded for audit.
    acts = store._conn.execute(
        "SELECT chokepoint, verdict FROM activities").fetchall()
    assert acts == [("browser-upload", "deny")]
    used = store._conn.execute(
        "SELECT object FROM edges WHERE predicate='used'").fetchall()
    assert used == [("file:secret",)]


def test_local_only_allowed_on_local_clipboard(store):
    res = record_chokepoint(
        store, chokepoint="clipboard", inputs=[_local_only_input()],
        output_eid="file:pasted",
    )
    assert res.verdict is GuardVerdict.ALLOW
    assert res.allowed
    assert res.output_guards == frozenset({"local-only"})  # viral inheritance
    out = store.get_entity("file:pasted")
    assert out.guards == frozenset({"local-only"})
    # Derivation edge recorded.
    assert store.upstream("file:pasted") == frozenset({"file:secret"})


def test_local_only_workflow_step_remote_host_denied(store):
    proc = report_processing_host(host_class="remote-service", payload_submitted=True)
    res = record_chokepoint(
        store, chokepoint="workflow-step", inputs=[_local_only_input()],
        processing=proc,
    )
    assert res.denied


def test_local_only_workflow_step_local_host_allowed(store):
    proc = report_processing_host(host_class="local-vm", network_egress="none",
                                  payload_submitted=False)
    res = record_chokepoint(
        store, chokepoint="workflow-step", inputs=[_local_only_input()],
        processing=proc,
    )
    assert res.allowed
    # Effective processing host is reported on the activity row.
    hc = store._conn.execute("SELECT host_class FROM activities").fetchone()[0]
    assert hc == "local-vm"


def test_report_processing_host_garbage_fails_closed():
    proc = report_processing_host(host_class="azure-magic")
    assert proc.host_class == "unknown"
    assert isinstance(proc, ProcessingDescriptor)


def test_most_restrictive_input_wins(store):
    # One clean input + one local-only input through a remote upload: deny.
    clean = ChokepointInput(eid="file:clean", endpoint=FlowEndpoint())
    res = record_chokepoint(
        store, chokepoint="browser-upload", inputs=[clean, _local_only_input()],
    )
    assert res.denied


# --- no-cross-contaminate -------------------------------------------------

def _ncc(eid, compartments, conflict=("home-work-separation",)):
    return ChokepointInput(eid=eid, endpoint=FlowEndpoint(
        guards=frozenset({"no-cross-contaminate"}),
        compartments=frozenset(compartments),
        conflict_classes=frozenset(conflict),
    ))


def test_ncc_cross_compartment_denied(store):
    src = _ncc("file:work", ["work"])
    dst = FlowEndpoint(compartments=frozenset({"home"}),
                       conflict_classes=frozenset({"home-work-separation"}))
    res = record_chokepoint(
        store, chokepoint="clipboard", inputs=[src], destination=dst,
    )
    assert res.denied
    assert res.output_eid is None


def test_ncc_same_compartment_allowed(store):
    src = _ncc("file:work", ["work"])
    dst = FlowEndpoint(compartments=frozenset({"work"}),
                       conflict_classes=frozenset({"home-work-separation"}))
    res = record_chokepoint(
        store, chokepoint="clipboard", inputs=[src], destination=dst,
        output_eid="file:work2",
    )
    assert res.allowed
    assert res.output_compartments == frozenset({"work"})


def test_ncc_combining_compartments_marks_mixed(store):
    # An allowed combine of two compartments under a shared conflict class
    # (e.g. archive-create, which is a local chokepoint the registry allows)
    # produces a mixed-integrity derivative (doc/guards.md §Cross-silo).
    a = _ncc("file:work", ["work"])
    b = _ncc("file:client", ["client-acme"])
    res = record_chokepoint(
        store, chokepoint="archive-create", inputs=[a, b],
        output_eid="archive:mixed",
    )
    assert res.allowed  # archive-create is local
    assert res.output_integrity == "mixed"
    assert res.output_compartments == frozenset({"work", "client-acme"})
    out = store.get_entity("archive:mixed")
    assert out.integrity == "mixed"
    assert out.guards == frozenset({"no-cross-contaminate"})


# --- data-mapping fallback ------------------------------------------------

def test_untrusted_mapping_does_not_narrow_guards(store):
    src = _local_only_input("archive:guarded")
    # An app-reported mapping claims the output is clean — it must be ignored.
    res = record_chokepoint(
        store, chokepoint="archive-extract", inputs=[src],
        output_eid="file:member",
        trusted_mapping={"confidence": "app-reported", "output_guards": []},
    )
    assert res.output_guards == frozenset({"local-only"})  # fell back to union


def test_trusted_mapping_may_narrow_guards(store):
    # archive-extract is a local (allowed) deriving chokepoint, so the output is
    # actually minted; a broker-derived mapping proves a member draws only from a
    # clean part, so its guards are narrowed away.
    a = ChokepointInput(eid="archive:a", endpoint=FlowEndpoint(guards=frozenset({"local-only"})))
    res = record_chokepoint(
        store, chokepoint="archive-extract", inputs=[a], output_eid="file:clean",
        trusted_mapping={"confidence": "broker-derived", "output_guards": []},
    )
    assert res.allowed
    assert res.output_guards == frozenset()
    out = store.get_entity("file:clean")
    assert out.guards == frozenset()


def test_trusted_mapping_without_output_guards_keeps_union(store):
    a = _local_only_input("archive:a")
    res = record_chokepoint(
        store, chokepoint="archive-extract", inputs=[a], output_eid="file:m",
        trusted_mapping={"confidence": "broker-derived"},  # no output_guards key
    )
    assert res.output_guards == frozenset({"local-only"})


# --- declassification -----------------------------------------------------

def test_declassify_produces_tracked_derivative(store):
    from qdistro_lineage_store import Agent, Entity
    store.record_agent(Agent(gid="credential:reviewer", kind="credential-authority"))
    store.record_entity(Entity(eid="file:secret", kind="file",
                               guards=frozenset({"local-only", "no-cross-contaminate"}),
                               compartments=frozenset({"work"})))
    out = declassify(
        store,
        source_eid="file:secret",
        output_eid="export:released",
        residual_guards=["local-only"],  # one guard narrowed away
        authority_gid="credential:reviewer",
        transform="redact-pii/2.0",
        agent_gid="workflow:export-review",
    )
    assert out == "export:released"
    # Source stays guarded.
    src = store.get_entity("file:secret")
    assert "no-cross-contaminate" in src.guards
    # Output is a narrowed, tracked derivative.
    released = store.get_entity("export:released")
    assert released.guards == frozenset({"local-only"})
    assert released.kind == "export"
    # Output remains derived from the source.
    assert store.upstream("export:released") == frozenset({"file:secret"})
    # Declassification evidence recorded as a broker-derived assertion.
    ev = store.assertions_for("export:released", authority="broker-derived")
    assert len(ev) == 1
    assert ev[0]["fact"] == "declassification"
    assert ev[0]["value"]["transform"] == "redact-pii/2.0"
    assert ev[0]["value"]["authority"] == "credential:reviewer"
    # actedOnBehalfOf edge from workflow agent to the authority.
    aob = store._conn.execute(
        "SELECT subject, object FROM edges WHERE predicate='actedOnBehalfOf'"
    ).fetchall()
    assert aob == [("workflow:export-review", "credential:reviewer")]


def test_empty_inputs_on_deriving_chokepoint_rejected(store):
    # A deriving chokepoint with no inputs must NOT mint a clean, sourceless
    # output (the unsourced-export laundering hole).
    with pytest.raises(ValueError):
        record_chokepoint(store, chokepoint="export", inputs=[],
                          output_eid="export:laundered")
    assert store.get_entity("export:laundered") is None


def test_empty_inputs_on_nonderiving_chokepoint_allowed(store):
    # A non-deriving chokepoint (e.g. a download with nothing guarded) with no
    # inputs is a benign no-op allow — no output minted, nothing denied.
    res = record_chokepoint(store, chokepoint="browser-download", inputs=[])
    assert res.allowed
    assert res.output_eid is None


def test_chokepoint_is_atomic_on_failure(store):
    # A failure midway through recording a deriving chokepoint must roll back the
    # whole group — no output entity without its derivation edges, chain intact.
    import sqlite3
    store._conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON edges "
        "WHEN NEW.predicate='wasDerivedFrom' "
        "BEGIN SELECT RAISE(ABORT, 'crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        record_chokepoint(store, chokepoint="clipboard",
                          inputs=[_local_only_input()], output_eid="file:partial")
    store._conn.execute("DROP TRIGGER boom")
    # Nothing from the failed chokepoint survived (activity + output rolled back).
    assert store.get_entity("file:partial") is None
    assert store._conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    assert store.verify_chain() is True


def test_chokepoint_records_keep_chain_intact(store):
    record_chokepoint(store, chokepoint="clipboard", inputs=[_local_only_input()],
                      output_eid="file:p")
    # declassify fails closed on an unrecorded source, so seed it first.
    store.record_entity(Entity(eid="file:secret", kind="file",
                               guards=frozenset({"local-only"})))
    declassify(store, source_eid="file:secret", output_eid="ex:1",
               residual_guards=[], authority_gid="cred:a")
    assert store.verify_chain() is True
