"""Unit tests for qdistro_security_mutation — policy-controlled mutation of
security fields with immutable audit/lineage history (doc/metadata.md
§Mutability, doc/guards.md §Declassification).

Covers: change classification, tighten (in-place new snapshot), declassification
state machine, fail-closed narrowing (no authority / no verdict), narrowing
produces a NEW derivative leaving the source guarded, audit/lineage immutability
(old snapshot + hash chain preserved), and descendant impact reporting.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))

from qdistro_lineage_store import Entity, LineageStore  # noqa: E402
from qdistro_security_grants import ADMIN_APP_PRINCIPAL, SecurityGrantStore  # noqa: E402
from qdistro_security_mutation import (  # noqa: E402
    ChangeClass,
    SecurityChange,
    SecurityMutationError,
    SecurityMutator,
    classify_change,
    touched_fields,
)


@pytest.fixture
def store(tmp_path):
    s = LineageStore(str(tmp_path / "lineage.db"))
    yield s
    s.close()


@pytest.fixture
def mutator(store):
    return SecurityMutator(store)


def _seed(store, eid="file:secret", *, guards=("local-only",),
          compartments=("work",), conflict_classes=("hw",),
          sensitivity="confidential", declass="none"):
    store.record_entity(Entity(
        eid=eid, kind="file", guards=frozenset(guards),
        compartments=frozenset(compartments),
        conflict_classes=frozenset(conflict_classes),
        facets={"sensitivity": sensitivity, "declassification": declass},
    ))


# --------------------------------------------------------------------------
# classification (pure)
# --------------------------------------------------------------------------

def test_classify_add_guard_is_tighten(store):
    _seed(store)
    ent = store.get_entity("file:secret")
    c = SecurityChange(guards=frozenset({"local-only", "no-cross-contaminate"}))
    assert classify_change(ent, c) is ChangeClass.TIGHTEN


def test_classify_remove_guard_is_narrow(store):
    _seed(store)
    ent = store.get_entity("file:secret")
    assert classify_change(ent, SecurityChange(guards=frozenset())) is ChangeClass.NARROW


def test_classify_lower_sensitivity_is_narrow(store):
    _seed(store, sensitivity="restricted")
    ent = store.get_entity("file:secret")
    c = SecurityChange(sensitivity="public")
    assert classify_change(ent, c) is ChangeClass.NARROW


def test_classify_narrow_dominates_a_simultaneous_tighten(store):
    """Removing a compartment while adding a guard is still a NARROW — you can't
    smuggle a removal behind an addition."""
    _seed(store, guards=("local-only",), compartments=("work", "proj"))
    ent = store.get_entity("file:secret")
    c = SecurityChange(
        guards=frozenset({"local-only", "no-cross-contaminate"}),  # add
        compartments=frozenset({"work"}),  # remove 'proj'
    )
    assert classify_change(ent, c) is ChangeClass.NARROW


def test_classify_declass_state_move_is_state(store):
    _seed(store, declass="none")
    ent = store.get_entity("file:secret")
    assert classify_change(ent, SecurityChange(declassification="requested")) is ChangeClass.STATE


def test_classify_noop(store):
    _seed(store)
    ent = store.get_entity("file:secret")
    assert classify_change(ent, SecurityChange()) is ChangeClass.NOOP


# --------------------------------------------------------------------------
# tighten
# --------------------------------------------------------------------------

def test_tighten_appends_new_snapshot_and_keeps_old(store, mutator):
    _seed(store)
    before_rows = store._conn.execute(
        "SELECT COUNT(*) FROM entities WHERE eid='file:secret'").fetchone()[0]
    r = mutator.reclassify(
        "file:secret",
        SecurityChange(guards=frozenset({"local-only", "no-cross-contaminate"})),
        actor="admin",
    )
    assert r.allowed and r.change_class is ChangeClass.TIGHTEN
    after_rows = store._conn.execute(
        "SELECT COUNT(*) FROM entities WHERE eid='file:secret'").fetchone()[0]
    # New snapshot appended, old one preserved (append-only).
    assert after_rows == before_rows + 1
    cur = store.get_entity("file:secret")
    assert cur.guards == frozenset({"local-only", "no-cross-contaminate"})


def test_tighten_records_sealed_evidence(store, mutator):
    _seed(store)
    mutator.reclassify(
        "file:secret", SecurityChange(sensitivity="restricted"), actor="admin",
        reason="raised for client X",
    )
    ev = [a for a in store.assertions_for("file:secret", authority="broker-derived")
          if a["fact"] == "security.reclassify"]
    assert ev and ev[0]["value"]["class"] == "tighten"
    assert ev[0]["value"]["sensitivity"] == "restricted"


def test_tighten_can_propagate_added_guards_to_descendants(store, mutator):
    _seed(store)
    store.record_entity(Entity(eid="file:deriv", kind="artifact"))
    store.record_edge("wasDerivedFrom", "file:deriv", "file:secret")
    r = mutator.reclassify(
        "file:secret",
        SecurityChange(guards=frozenset({"local-only", "no-cross-contaminate"})),
        actor="admin", propagate_to_descendants=True,
    )
    assert "file:deriv" in r.descendants_for_review
    prop = [a for a in store.assertions_for("file:deriv", authority="broker-derived")
            if a["fact"] == "security.propagated_guards"]
    assert prop and "no-cross-contaminate" in prop[0]["value"]["added_guards"]


# --------------------------------------------------------------------------
# declassification state machine
# --------------------------------------------------------------------------

def test_state_forward_move_allowed(store, mutator):
    _seed(store, declass="none")
    r = mutator.reclassify("file:secret", SecurityChange(declassification="requested"),
                           actor="user")
    assert r.allowed and r.change_class is ChangeClass.STATE
    assert store.get_entity("file:secret").facets["declassification"] == "requested"


def test_state_backward_move_denied(store, mutator):
    _seed(store, declass="approved")
    r = mutator.reclassify("file:secret", SecurityChange(declassification="requested"),
                           actor="user")
    assert not r.allowed
    # unchanged
    assert store.get_entity("file:secret").facets["declassification"] == "approved"


def test_state_approved_requires_authority(store, mutator):
    _seed(store, declass="requested")
    denied = mutator.reclassify("file:secret", SecurityChange(declassification="approved"),
                                actor="user")
    assert not denied.allowed
    ok = mutator.reclassify("file:secret", SecurityChange(declassification="approved"),
                            actor="user", authority="agent:admin")
    assert ok.allowed


# --------------------------------------------------------------------------
# narrowing / declassification — fail closed
# --------------------------------------------------------------------------

def test_narrow_without_authority_denied_no_write(store, mutator):
    _seed(store)
    rows_before = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    r = mutator.reclassify("file:secret", SecurityChange(guards=frozenset()),
                           actor="admin", policy_verdict="declassify")
    assert not r.allowed and "authority" in r.reason
    rows_after = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert rows_after == rows_before  # no write


def test_narrow_without_declassify_verdict_denied(store, mutator):
    _seed(store)
    r = mutator.reclassify("file:secret", SecurityChange(guards=frozenset()),
                           actor="admin", authority="agent:admin",
                           policy_verdict="allow")
    assert not r.allowed and "verdict" in r.reason


def test_narrow_produces_new_derivative_source_stays_guarded(store, mutator):
    _seed(store, guards=("local-only", "no-cross-contaminate"))
    r = mutator.reclassify(
        "file:secret", SecurityChange(guards=frozenset({"local-only"})),
        actor="wf:export", authority="agent:admin", policy_verdict="declassify",
        destination="review-silo", transform="redactor-v1",
    )
    assert r.allowed and r.change_class is ChangeClass.NARROW
    # A NEW entity, not the source.
    assert r.result_eid is not None and r.result_eid != "file:secret"
    # Source is untouched: still carries both guards.
    src = store.get_entity("file:secret")
    assert src.guards == frozenset({"local-only", "no-cross-contaminate"})
    # The derivative carries the narrowed (residual) guard set.
    out = store.get_entity(r.result_eid)
    assert out.guards == frozenset({"local-only"})
    # And is wasDerivedFrom the source.
    assert "file:secret" in store.upstream(r.result_eid)


def test_narrow_records_declassification_evidence(store, mutator):
    _seed(store)
    r = mutator.reclassify(
        "file:secret", SecurityChange(guards=frozenset()),
        actor="wf", authority="agent:admin", policy_verdict="declassify",
        destination="export-dest",
    )
    ev = {a["fact"] for a in store.assertions_for(r.result_eid,
                                                  authority="broker-derived")}
    assert "declassification" in ev  # from declassify()
    assert "declassification.context" in ev  # destination / verdict / reason


# --------------------------------------------------------------------------
# immutability of audit / lineage history
# --------------------------------------------------------------------------

def test_all_mutations_preserve_hash_chain(store, mutator):
    _seed(store)
    mutator.reclassify("file:secret",
                       SecurityChange(guards=frozenset({"local-only", "no-cross-contaminate"})),
                       actor="admin")
    mutator.reclassify("file:secret", SecurityChange(declassification="requested"),
                       actor="user")
    mutator.reclassify("file:secret", SecurityChange(guards=frozenset({"local-only"})),
                       actor="wf", authority="agent:admin", policy_verdict="declassify")
    assert store.verify_chain() is True


def test_mutate_unknown_entity_raises(store, mutator):
    with pytest.raises(SecurityMutationError):
        mutator.reclassify("file:nope", SecurityChange(guards=frozenset()),
                           actor="admin")


def test_empty_actor_rejected(store, mutator):
    _seed(store)
    with pytest.raises(SecurityMutationError):
        mutator.reclassify("file:secret", SecurityChange(sensitivity="restricted"),
                           actor="")


def test_noop_is_allowed_with_no_write(store, mutator):
    _seed(store)
    rows_before = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    r = mutator.reclassify("file:secret", SecurityChange(), actor="admin")
    assert r.allowed and r.change_class is ChangeClass.NOOP
    rows_after = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert rows_after == rows_before


# --------------------------------------------------------------------------
# delegation gate (decisions/security-field-mutation-authority.md)
# --------------------------------------------------------------------------

def test_touched_fields_only_changed_fields():
    from qdistro_security_mutation import _current_sensitivity  # noqa
    e = Entity(eid="x", kind="file", guards=frozenset({"g"}),
               compartments=frozenset({"c"}),
               facets={"sensitivity": "confidential", "declassification": "none"})
    # same guards as current -> not touched; sensitivity raised -> touched.
    tf = touched_fields(e, SecurityChange(guards=frozenset({"g"}),
                                          sensitivity="restricted"))
    assert tf == frozenset({"security.sensitivity"})


def test_admin_app_default_allow_records_via(store):
    """Unspecified principal == admin app == default authority; evidence records
    the admin-app-default scope per touched field."""
    _seed(store)
    mut = SecurityMutator(store)  # default admin app principal
    r = mut.reclassify("file:secret",
                       SecurityChange(guards=frozenset({"local-only", "x"})),
                       actor="admin")
    assert r.allowed
    assert r.delegated_via.get("security.guards") == "admin-app-default"
    ev = [a for a in store.assertions_for("file:secret", authority="broker-derived")
          if a["fact"] == "security.reclassify"][0]
    assert ev["value"]["delegated_via"]["security.guards"] == "admin-app-default"


def test_ungranted_principal_denied_no_write(store):
    _seed(store)
    mut = SecurityMutator(store)
    rows_before = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    r = mut.reclassify("file:secret",
                       SecurityChange(guards=frozenset({"local-only", "x"})),
                       actor="wf:export", principal="wf:export")
    assert not r.allowed and "delegation denied" in r.reason
    rows_after = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert rows_after == rows_before  # fail closed: no snapshot written


def test_delegated_exact_grant_allows_that_field(store):
    _seed(store)
    grants = SecurityGrantStore(store)
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    mut = SecurityMutator(store, grant_store=grants)
    r = mut.reclassify("file:secret",
                       SecurityChange(guards=frozenset({"local-only", "x"})),
                       actor="wf:export", principal="wf:export")
    assert r.allowed and r.delegated_via["security.guards"] == "security.guards"


def test_delegated_grant_wrong_field_denied(store):
    _seed(store)
    grants = SecurityGrantStore(store)
    grants.grant("wf:export", "sensitivity", granter=ADMIN_APP_PRINCIPAL)
    mut = SecurityMutator(store, grant_store=grants)
    # holds sensitivity grant but tries to change guards -> denied
    r = mut.reclassify("file:secret",
                       SecurityChange(guards=frozenset({"local-only", "x"})),
                       actor="wf:export", principal="wf:export")
    assert not r.allowed and "delegation denied" in r.reason


def test_wildcard_grant_allows_multi_field_change(store):
    _seed(store)
    grants = SecurityGrantStore(store)
    grants.grant("wf:promote", "security.*", granter=ADMIN_APP_PRINCIPAL)
    mut = SecurityMutator(store, grant_store=grants)
    r = mut.reclassify(
        "file:secret",
        SecurityChange(guards=frozenset({"local-only", "x"}), sensitivity="restricted"),
        actor="wf:promote", principal="wf:promote")
    assert r.allowed
    assert r.delegated_via["security.guards"] == "security.*"
    assert r.delegated_via["security.sensitivity"] == "security.*"


def test_partial_grant_denies_whole_multi_field_change_no_write(store):
    """A multi-field change needs a grant for EVERY touched field; missing one
    denies the whole request with no write."""
    _seed(store)
    grants = SecurityGrantStore(store)
    grants.grant("wf:x", "guards", granter=ADMIN_APP_PRINCIPAL)  # but not sensitivity
    mut = SecurityMutator(store, grant_store=grants)
    rows_before = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    r = mut.reclassify(
        "file:secret",
        SecurityChange(guards=frozenset({"local-only", "x"}), sensitivity="restricted"),
        actor="wf:x", principal="wf:x")
    assert not r.allowed
    rows_after = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert rows_after == rows_before


def test_delegated_narrow_requires_grant(store):
    """A delegated principal declassifying (NARROW) still needs a guards grant in
    addition to authority + verdict; without the grant it is denied."""
    _seed(store, guards=("local-only", "no-cross-contaminate"))
    grants = SecurityGrantStore(store)
    mut = SecurityMutator(store, grant_store=grants)
    # ungranted principal with full authority+verdict still denied (delegation gate)
    r = mut.reclassify(
        "file:secret", SecurityChange(guards=frozenset({"local-only"})),
        actor="wf:export", principal="wf:export",
        authority="agent:admin", policy_verdict="declassify")
    assert not r.allowed and "delegation denied" in r.reason
    # grant guards -> now allowed (still needs authority+verdict, which it has)
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    r2 = mut.reclassify(
        "file:secret", SecurityChange(guards=frozenset({"local-only"})),
        actor="wf:export", principal="wf:export",
        authority="agent:admin", policy_verdict="declassify")
    assert r2.allowed and r2.change_class is ChangeClass.NARROW
    assert r2.delegated_via["security.guards"] == "security.guards"


def test_narrow_is_atomic_derivative_and_context_or_neither(store):
    """The declassification derivative AND its delegation/destination context
    must commit together. If the context assertion fails, the derivative must NOT
    persist (no orphan declassified output missing its delegated_via evidence)."""
    _seed(store, guards=("local-only", "no-cross-contaminate"))
    mut = SecurityMutator(store)
    entities_before = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

    real_record_assertion = store.record_assertion

    def boom_on_context(*args, **kwargs):
        if kwargs.get("fact") == "declassification.context":
            raise RuntimeError("simulated crash recording context evidence")
        return real_record_assertion(*args, **kwargs)

    store.record_assertion = boom_on_context  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            mut.reclassify(
                "file:secret", SecurityChange(guards=frozenset({"local-only"})),
                actor="wf:export", authority="agent:admin",
                policy_verdict="declassify", destination="dest")
    finally:
        store.record_assertion = real_record_assertion  # type: ignore[assignment]

    entities_after = store._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    # The whole narrow rolled back: no orphan derivative.
    assert entities_after == entities_before
    assert store.verify_chain() is True


def test_delegation_and_chain_integrity_with_grants(store):
    _seed(store)
    grants = SecurityGrantStore(store)
    grants.grant("wf:export", "security.*", granter=ADMIN_APP_PRINCIPAL)
    grants.revoke("wf:export", "security.*", granter=ADMIN_APP_PRINCIPAL)
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    mut = SecurityMutator(store, grant_store=grants)
    mut.reclassify("file:secret",
                   SecurityChange(guards=frozenset({"local-only", "x"})),
                   actor="wf:export", principal="wf:export")
    assert store.verify_chain() is True
