"""Guard propagation at chokepoints, recorded into the lineage store.

This is the wiring layer doc/lineage.md §Chokepoints asks for: at each brokered
boundary (clipboard, commit, upload/download, import/export, extract, Recall
query/export, archive create/extract, workflow step) the broker calls
:func:`record_chokepoint`. That function:

1. evaluates guards on the flow via the behavioural guard registry
   (``qdistro_guard_registry.evaluate_flow`` — the just-shipped enforcement
   layer; we *consume* it, we do not reimplement guard logic);
2. records the activity, its effective processing host, the inputs it ``used``,
   the output it ``wasGeneratedBy``/``wasDerivedFrom``, and the guard verdict
   into the central :class:`~qdistro_lineage_store.LineageStore`;
3. propagates the inherited guard/compartment/conflict-class sets onto the
   output entity (the monotonic union, ``propagate_guards`` + doc/guards.md
   §Propagation), unless a trusted data mapping narrows it.

The split of responsibility is deliberate:

* ``qdistro_guard_registry`` decides allow/deny/contaminate for a single flow.
* ``qdistro_lineage_store`` is the dumb relational record.
* This module is the broker-facing glue: it turns a chokepoint event into the
  registry call + the lineage rows, and computes the *output* security snapshot
  the next hop will see.

Style mirrors the other broker modules: plain functions, dataclasses, frozen
sets, no third-party deps.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from qdistro_guard_registry import (
    FlowContext,
    FlowEndpoint,
    GuardVerdict,
    ProcessingDescriptor,
    evaluate_flow,
    propagate_guards,
)
from qdistro_lineage_store import (
    Activity,
    Entity,
    LineageStore,
    mapping_narrows_guards,
)

#: Map each chokepoint name to the lineage activity kind it records. The keys
#: are exactly the chokepoints doc/lineage.md §Chokepoints and the todo list:
#: clipboard, commit, upload, download, extract, import, export, Recall
#: query/export, archive create/extract, workflow steps.
CHOKEPOINT_ACTIVITY_KIND = {
    "clipboard": "clipboard",
    "commit-create": "commit",
    "browser-upload": "upload",
    "browser-download": "download",
    "archive-extract": "extract",
    "archive-create": "archive-create",
    "import": "import",
    "export": "export",
    "recall-query": "recall-query",
    "recall-export": "recall-export",
    "workflow-step": "workflow-step",
}

#: Chokepoints whose output entity is a derivative of the inputs (gets a
#: ``wasDerivedFrom`` edge + inherits the guard union). The rest still ``used``
#: their inputs but do not necessarily mint a derivative entity.
DERIVING_CHOKEPOINTS = frozenset(
    {
        "clipboard",
        "commit-create",
        "archive-extract",
        "archive-create",
        "import",
        "export",
        "recall-query",
        "recall-export",
    }
)


@dataclass(frozen=True)
class ChokepointInput:
    """One input entity to a chokepoint flow, with its security snapshot."""

    eid: str
    endpoint: FlowEndpoint = field(default_factory=FlowEndpoint)


@dataclass(frozen=True)
class ChokepointResult:
    """Outcome of recording a chokepoint: the registry verdict, whether the flow
    may proceed, the output entity's inherited security snapshot, and the
    lineage ids written."""

    verdict: GuardVerdict
    allowed: bool
    reasons: tuple[str, ...]
    output_guards: frozenset[str]
    output_compartments: frozenset[str]
    output_conflict_classes: frozenset[str]
    output_integrity: str | None
    activity_aid: str
    output_eid: str | None

    @property
    def denied(self) -> bool:
        return not self.allowed


def _gen_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def record_chokepoint(
    store: LineageStore,
    *,
    chokepoint: str,
    inputs: Sequence[ChokepointInput],
    destination: FlowEndpoint | None = None,
    processing: ProcessingDescriptor | None = None,
    output_eid: str | None = None,
    output_kind: str = "artifact",
    output_digest: str | None = None,
    output_locator: str | None = None,
    agent_gid: str | None = None,
    action_version: str | None = None,
    trusted_mapping: dict | None = None,
    now: int | None = None,
) -> ChokepointResult:
    """Evaluate + record one chokepoint flow.

    Guard enforcement is delegated to the registry: every input endpoint is
    evaluated against the flow, and the aggregate verdict is the most restrictive
    across all inputs (so combining one denied source with one clean source still
    denies). The output security snapshot is the monotonic union of the inputs'
    guards/compartments/conflict-classes — that is the contamination rule of
    doc/lineage.md §Contamination — *unless* a trusted (broker-derived /
    trusted-tool) mapping is supplied, in which case propagation may be narrowed.

    Returns a :class:`ChokepointResult`. The caller (broker) enforces the
    verdict; this function records lineage regardless of allow/deny so a *denied*
    attempt is still auditable (doc/lineage.md: lineage is for audit + forensic
    queries, not just successful flows). When denied, no derivative output entity
    is minted (there is no clean output), but the activity + ``used`` edges are
    still written.
    """
    ts = now if now is not None else int(time.time())
    proc = processing or ProcessingDescriptor()
    dst = destination or FlowEndpoint()

    # Fail closed on a deriving chokepoint with no inputs: an output that derives
    # from nothing would be minted clean with no `used`/`wasDerivedFrom` edges,
    # which is exactly the unsourced-clean-artifact a malicious export wants.
    # A genuinely source-free artifact must be recorded via record_entity, not
    # laundered through a deriving chokepoint.
    if not inputs and chokepoint in DERIVING_CHOKEPOINTS:
        raise ValueError(
            f"deriving chokepoint {chokepoint!r} requires at least one input "
            f"(a source-free artifact must use record_entity, not a chokepoint)"
        )

    # 1. Evaluate guards per input against the flow; most-restrictive wins.
    worst = GuardVerdict.ALLOW
    reasons: list[str] = []
    propagated_guards: set[str] = set()
    for inp in inputs:
        ctx = FlowContext(
            source=inp.endpoint,
            chokepoint=chokepoint,
            destination=dst,
            processing=proc,
        )
        v = evaluate_flow(ctx)
        worst = max(worst, v.verdict)
        reasons.extend(v.reasons)
        propagated_guards |= set(v.propagate)
    allowed = worst <= GuardVerdict.WARN

    # 2. Compute the output security snapshot (contamination union).
    union_guards = propagate_guards(*[set(i.endpoint.guards) for i in inputs], propagated_guards)
    union_compartments: set[str] = set()
    union_conflicts: set[str] = set()
    for i in inputs:
        union_compartments |= set(i.endpoint.compartments)
        union_conflicts |= set(i.endpoint.conflict_classes)

    # A trusted mapping may narrow guard propagation to a declared subset; an
    # untrusted/absent mapping falls back to the whole-entity union.
    if trusted_mapping is not None and mapping_narrows_guards(
        trusted_mapping.get("confidence", "")
    ):
        narrowed = trusted_mapping.get("output_guards")
        if narrowed is not None:
            union_guards = frozenset(
                g for g in union_guards if g in set(narrowed)
            )

    # Integrity becomes "mixed" when the flow combines >1 distinct compartment
    # under a shared conflict class (doc/guards.md §Cross-silo "mixed derivative").
    distinct_compartments = {c for i in inputs for c in i.endpoint.compartments}
    output_integrity: str | None = None
    if len(distinct_compartments) > 1 and union_conflicts:
        output_integrity = "mixed"

    # Record everything for this chokepoint in ONE atomic transaction: a partial
    # write (e.g. an output entity without its wasDerivedFrom edges) would be
    # chain-valid but incomplete lineage. store.transaction() is re-entrant, so
    # the per-record_* txns join this outer scope.
    aid = _gen_id("activity")
    out_eid: str | None = None
    with store.transaction():
        # 3. Record the activity (with effective processing host).
        store.record_activity(
            Activity(
                aid=aid,
                kind=CHOKEPOINT_ACTIVITY_KIND.get(chokepoint, "workflow-step"),
                chokepoint=chokepoint,
                action_version=action_version,
                host_class=proc.host_class,
                network_egress=proc.network_egress,
                verdict=worst.name_lower,
                started_at=ts,
                ended_at=ts,
            )
        )
        if agent_gid is not None:
            store.record_edge("wasAssociatedWith", aid, agent_gid, activity=aid)

        # 4. ``used`` edges for every input.
        for inp in inputs:
            store.record_edge("used", aid, inp.eid, activity=aid)

        # 5. Mint + record the output entity for deriving chokepoints when allowed.
        if allowed and chokepoint in DERIVING_CHOKEPOINTS:
            out_eid = output_eid or _gen_id("entity")
            store.record_entity(
                Entity(
                    eid=out_eid,
                    kind=output_kind,
                    digest=output_digest,
                    locator=output_locator,
                    guards=union_guards,
                    compartments=frozenset(union_compartments),
                    conflict_classes=frozenset(union_conflicts),
                    integrity=output_integrity,
                    created_at=ts,
                )
            )
            store.record_edge("wasGeneratedBy", out_eid, aid, activity=aid)
            for inp in inputs:
                store.record_edge("wasDerivedFrom", out_eid, inp.eid, activity=aid)
            if agent_gid is not None:
                store.record_edge("wasAttributedTo", out_eid, agent_gid, activity=aid)

    return ChokepointResult(
        verdict=worst,
        allowed=allowed,
        reasons=tuple(reasons),
        output_guards=frozenset(union_guards),
        output_compartments=frozenset(union_compartments),
        output_conflict_classes=frozenset(union_conflicts),
        output_integrity=output_integrity,
        activity_aid=aid,
        output_eid=out_eid,
    )


def report_processing_host(
    *,
    host_class: str = "unknown",
    network_egress: str = "unknown",
    payload_submitted: bool = True,
    telemetry_submitted: bool = False,
) -> ProcessingDescriptor:
    """Build the effective-processing-host descriptor an action handler reports
    (doc/lineage.md "Add action-handler reporting for effective processing host:
    local, local VM/container, remote service, or unknown"). Unknown host fails
    closed for local-only data, matching the guard registry's default."""
    if host_class not in {"local", "local-vm", "local-container", "remote-service", "unknown"}:
        host_class = "unknown"  # fail closed on a garbage host claim
    return ProcessingDescriptor(
        host_class=host_class,
        network_egress=network_egress,
        payload_submitted=payload_submitted,
        telemetry_submitted=telemetry_submitted,
    )


def declassify(
    store: LineageStore,
    *,
    source_eid: str,
    output_eid: str | None = None,
    output_kind: str = "export",
    output_digest: str | None = None,
    residual_guards: Sequence[str] = (),
    residual_compartments: Sequence[str] = (),
    residual_conflict_classes: Sequence[str] = (),
    authority_gid: str,
    transform: str | None = None,
    agent_gid: str | None = None,
    now: int | None = None,
) -> str:
    """Record a declassification workflow (doc/guards.md §Declassification,
    doc/lineage.md §Contamination "Sanitize/export is not lineage erasure").

    Declassification is an authority-bearing decision applied from *outside* the
    default guard enforcement: it mints a tracked derivative whose guard set is
    narrowed by an explicit authority, while the source stays guarded and the
    output remains ``wasDerivedFrom`` the source. We record the source refs,
    output refs, transform, the approving authority, and the residual security
    fields, and emit an ``actedOnBehalfOf`` edge from the workflow agent to the
    approving authority. Returns the new output eid.

    AUTHORITY CONTRACT (codex review 2026-06-18): this function performs the
    *mechanics* of a declassification; it does NOT itself enforce the
    capability/delegation gate. The supported, authority-checked entry point is
    :meth:`qdistro_security_mutation.SecurityMutator.reclassify` (NARROW path),
    which runs the per-field/wildcard delegation gate AND requires an approving
    authority + ``declassify`` verdict before calling here. Do not call
    ``declassify`` directly from an unauthenticated or ungated surface — a caller
    that can do so is as trusted as one that can write the lineage store. As a
    minimal guard against a mistaken caller, a non-empty ``authority_gid`` and an
    EXISTING source entity are required (fail closed: you cannot declassify a
    source that was never recorded).
    """
    if not authority_gid:
        raise ValueError("declassify requires a non-empty approving authority_gid")
    if store.get_entity(source_eid) is None:
        raise ValueError(
            f"cannot declassify unknown source entity {source_eid!r} "
            "(no recorded snapshot)")
    ts = now if now is not None else int(time.time())
    out_eid = output_eid or _gen_id("entity")
    aid = _gen_id("activity")

    # Whole declassification (activity + edges + output entity + evidence
    # assertion) commits atomically — declassification evidence with a missing
    # output entity, or vice versa, would be a broken authority record.
    with store.transaction():
        store.record_activity(
            Activity(
                aid=aid,
                kind="declassify",
                chokepoint="declassify",
                host_class="local",  # declassification is a local broker workflow
                network_egress="none",
                verdict="declassify",
                started_at=ts,
                ended_at=ts,
            )
        )
        store.record_edge("used", aid, source_eid, activity=aid)
        store.record_edge("wasAssociatedWith", aid, authority_gid, activity=aid)
        if agent_gid is not None:
            # The workflow agent acted under the approving authority.
            store.record_edge("actedOnBehalfOf", agent_gid, authority_gid, activity=aid)

        store.record_entity(
            Entity(
                eid=out_eid,
                kind=output_kind,
                digest=output_digest,
                guards=frozenset(residual_guards),
                compartments=frozenset(residual_compartments),
                conflict_classes=frozenset(residual_conflict_classes),
                created_at=ts,
            )
        )
        store.record_edge("wasGeneratedBy", out_eid, aid, activity=aid)
        # Output remains derived from the (still-guarded) source.
        store.record_edge("wasDerivedFrom", out_eid, source_eid, activity=aid)
        store.record_edge("wasAttributedTo", out_eid, authority_gid, activity=aid)

        # Declassification evidence is a broker-derived assertion (policy-bearing).
        store.record_assertion(
            subject=out_eid,
            fact="declassification",
            value={
                "source": source_eid,
                "transform": transform,
                "authority": authority_gid,
                "residual_guards": sorted(set(residual_guards)),
            },
            asserted_by=authority_gid,
            authority="broker-derived",
        )
    return out_eid
