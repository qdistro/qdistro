"""Tests for the behavioural guard registry (broker/qdistro_guard_registry.py)."""
import pytest

from qdistro_guard_registry import (
    GUARD_REGISTRY,
    FlowContext,
    FlowEndpoint,
    GuardVerdict,
    ProcessingDescriptor,
    evaluate_flow,
    propagate_guards,
)
from qdistro_metadata_schema import RESERVED_GUARDS


# --------------------------------------------------------------------------
# Registry / vocabulary parity
# --------------------------------------------------------------------------

def test_registry_covers_exactly_the_reserved_vocabulary():
    assert set(GUARD_REGISTRY) == set(RESERVED_GUARDS)


def test_every_behaviour_is_viral_and_described():
    for name, behaviour in GUARD_REGISTRY.items():
        assert behaviour.name == name
        assert behaviour.viral is True
        assert behaviour.description


# --------------------------------------------------------------------------
# Verdict ordering — the combination rule depends on it
# --------------------------------------------------------------------------

def test_verdict_ordering_least_to_most_restrictive():
    assert (
        GuardVerdict.ALLOW
        < GuardVerdict.CONTAMINATE
        < GuardVerdict.WARN
        < GuardVerdict.PROMPT
        < GuardVerdict.DENY
    )


# --------------------------------------------------------------------------
# local-only
# --------------------------------------------------------------------------

def _local_only_src():
    return FlowEndpoint(guards=frozenset({"local-only"}))


@pytest.mark.parametrize(
    "chokepoint",
    ["browser-upload", "api-call", "cloud-save", "recall-export",
     "backup-export", "git-push", "issue-upload", "pull-request", "telemetry"],
)
def test_local_only_denies_remote_egress_chokepoints(chokepoint):
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint=chokepoint))
    assert v.verdict is GuardVerdict.DENY
    assert not v.allowed
    assert "local-only" in v.propagate  # viral even on deny


@pytest.mark.parametrize(
    "chokepoint",
    ["clipboard", "recall-ingest", "archive-create", "archive-extract",
     "commit-create", "backup-local"],
)
def test_local_only_allows_local_chokepoints(chokepoint):
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint=chokepoint))
    assert v.verdict is GuardVerdict.ALLOW
    assert v.allowed
    assert v.propagate == frozenset({"local-only"})


def test_local_only_workflow_step_local_host_allows():
    proc = ProcessingDescriptor(host_class="local-vm", network_egress="none",
                                payload_submitted=False)
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step",
                                  processing=proc))
    assert v.verdict is GuardVerdict.ALLOW


def test_local_only_workflow_step_remote_host_denies():
    proc = ProcessingDescriptor(host_class="remote-service", payload_submitted=True)
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step",
                                  processing=proc))
    assert v.verdict is GuardVerdict.DENY


def test_local_only_workflow_step_unknown_host_fails_closed():
    # Default ProcessingDescriptor is host_class="unknown" → deny.
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step"))
    assert v.verdict is GuardVerdict.DENY


def test_local_only_unrestricted_egress_with_payload_denies():
    proc = ProcessingDescriptor(host_class="local", network_egress="unrestricted",
                                payload_submitted=True)
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step",
                                  processing=proc))
    assert v.verdict is GuardVerdict.DENY


def test_local_only_local_host_brokered_egress_no_payload_allows():
    proc = ProcessingDescriptor(host_class="local-container",
                                network_egress="brokered", payload_submitted=False)
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step",
                                  processing=proc))
    assert v.verdict is GuardVerdict.ALLOW


def test_local_only_local_host_brokered_egress_with_payload_denies():
    # A local VM that submits guarded payload over brokered egress still leaks
    # it off-box (doc/guards.md:112) → deny, NOT allow.
    proc = ProcessingDescriptor(host_class="local-vm", network_egress="brokered",
                                payload_submitted=True)
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step",
                                  processing=proc))
    assert v.verdict is GuardVerdict.DENY


def test_local_only_local_host_telemetry_only_denies():
    proc = ProcessingDescriptor(host_class="local", network_egress="brokered",
                                payload_submitted=False, telemetry_submitted=True)
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step",
                                  processing=proc))
    assert v.verdict is GuardVerdict.DENY


def test_local_only_local_host_egress_none_with_payload_allows():
    # Nothing leaves the box: local processing with no off-box egress.
    proc = ProcessingDescriptor(host_class="local-vm", network_egress="none",
                                payload_submitted=True)
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step",
                                  processing=proc))
    assert v.verdict is GuardVerdict.ALLOW


def test_local_only_unknown_host_no_payload_still_denies():
    # Not provably local → fail closed even when no payload is reported.
    proc = ProcessingDescriptor(host_class="unknown", payload_submitted=False)
    v = evaluate_flow(FlowContext(source=_local_only_src(), chokepoint="workflow-step",
                                  processing=proc))
    assert v.verdict is GuardVerdict.DENY


def test_no_guard_on_source_allows_remote_egress():
    # A source without local-only is unconstrained by this registry.
    v = evaluate_flow(FlowContext(source=FlowEndpoint(), chokepoint="git-push"))
    assert v.verdict is GuardVerdict.ALLOW
    assert v.decisions == ()


# --------------------------------------------------------------------------
# no-cross-contaminate
# --------------------------------------------------------------------------

def _ncc(compartments, conflict):
    return FlowEndpoint(
        guards=frozenset({"no-cross-contaminate"}),
        compartments=frozenset(compartments),
        conflict_classes=frozenset(conflict),
    )


def test_ncc_same_compartment_allows():
    src = _ncc(["work"], ["home-work-separation"])
    dst = _ncc(["work"], ["home-work-separation"])
    v = evaluate_flow(FlowContext(source=src, chokepoint="clipboard", destination=dst))
    assert v.verdict is GuardVerdict.ALLOW


def test_ncc_conflicting_compartment_denies():
    src = _ncc(["work"], ["home-work-separation"])
    dst = _ncc(["home"], ["home-work-separation"])
    v = evaluate_flow(FlowContext(source=src, chokepoint="clipboard", destination=dst))
    assert v.verdict is GuardVerdict.DENY
    assert "no-cross-contaminate" in v.propagate


def test_ncc_no_shared_conflict_class_allows():
    src = _ncc(["work"], ["home-work-separation"])
    dst = _ncc(["client-acme"], ["client-conflict"])
    v = evaluate_flow(FlowContext(source=src, chokepoint="clipboard", destination=dst))
    assert v.verdict is GuardVerdict.ALLOW


def test_ncc_unclassified_destination_denies():
    # Shared conflict class but an unclassified destination sink: not
    # subset-compatible → fail closed to DENY (doc/guards.md §Cross-silo).
    src = _ncc(["work"], ["home-work-separation"])
    dst = FlowEndpoint(conflict_classes=frozenset({"home-work-separation"}))
    v = evaluate_flow(FlowContext(source=src, chokepoint="commit-create", destination=dst))
    assert v.verdict is GuardVerdict.DENY
    assert not v.allowed


def test_ncc_empty_source_compartments_denies():
    # A guarded source with a conflict class but no compartment is not
    # subset-compatible with any classified destination (frozenset() <= X is
    # vacuously true, which must NOT allow) → DENY.
    src = FlowEndpoint(
        guards=frozenset({"no-cross-contaminate"}),
        conflict_classes=frozenset({"home-work-separation"}),
    )
    dst = _ncc(["home"], ["home-work-separation"])
    v = evaluate_flow(FlowContext(source=src, chokepoint="clipboard", destination=dst))
    assert v.verdict is GuardVerdict.DENY


def test_ncc_destination_superset_compartment_allows():
    # Destination already covers the source compartment (subset-compatible).
    src = _ncc(["work"], ["home-work-separation"])
    dst = _ncc(["work", "home"], ["home-work-separation"])
    v = evaluate_flow(FlowContext(source=src, chokepoint="clipboard", destination=dst))
    assert v.verdict is GuardVerdict.ALLOW


# --------------------------------------------------------------------------
# Aggregation across multiple guards (most-restrictive wins)
# --------------------------------------------------------------------------

def test_multiple_guards_take_most_restrictive():
    # local-only would ALLOW a local clipboard, but ncc DENYs the cross-compartment.
    src = FlowEndpoint(
        guards=frozenset({"local-only", "no-cross-contaminate"}),
        compartments=frozenset({"work"}),
        conflict_classes=frozenset({"home-work-separation"}),
    )
    dst = FlowEndpoint(
        compartments=frozenset({"home"}),
        conflict_classes=frozenset({"home-work-separation"}),
    )
    v = evaluate_flow(FlowContext(source=src, chokepoint="clipboard", destination=dst))
    assert v.verdict is GuardVerdict.DENY
    assert len(v.decisions) == 2
    assert v.propagate == frozenset({"local-only", "no-cross-contaminate"})


def test_unknown_guard_is_propagated_but_not_enforced():
    src = FlowEndpoint(guards=frozenset({"local-only", "future-guard"}))
    v = evaluate_flow(FlowContext(source=src, chokepoint="clipboard"))
    # Only local-only has behaviour (ALLOW on clipboard); future-guard carries on.
    assert v.verdict is GuardVerdict.ALLOW
    assert "future-guard" in v.propagate
    assert "local-only" in v.propagate


# --------------------------------------------------------------------------
# FlowEndpoint.from_security tolerance
# --------------------------------------------------------------------------

def test_from_security_parses_fields():
    ep = FlowEndpoint.from_security(
        {"guards": ["local-only"], "compartments": ["work"],
         "conflictClasses": ["home-work-separation"]}
    )
    assert ep.guards == frozenset({"local-only"})
    assert ep.compartments == frozenset({"work"})
    assert ep.conflict_classes == frozenset({"home-work-separation"})


@pytest.mark.parametrize("bad", [None, [], "nope", 42, {"guards": "local-only"}])
def test_from_security_tolerates_garbage(bad):
    ep = FlowEndpoint.from_security(bad)
    assert ep.guards == frozenset()
    assert ep.compartments == frozenset()
    assert ep.conflict_classes == frozenset()


# --------------------------------------------------------------------------
# propagate_guards
# --------------------------------------------------------------------------

def test_propagate_guards_unions():
    assert propagate_guards(["local-only"], ["no-cross-contaminate"]) == frozenset(
        {"local-only", "no-cross-contaminate"}
    )


def test_propagate_guards_tolerates_none_and_non_strings():
    assert propagate_guards(None, ["local-only", 7, None], frozenset({"local-only"})) == (
        frozenset({"local-only"})
    )


def test_propagate_guards_empty():
    assert propagate_guards() == frozenset()
