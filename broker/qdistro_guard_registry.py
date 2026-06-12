"""Behavioural guard registry — maps reserved guard names to enforcement.

``qdistro_metadata_schema`` owns the static guard *vocabulary*
(:data:`~qdistro_metadata_schema.RESERVED_GUARDS`) and purely structural
validation. This module is the behavioural layer it deliberately deferred: it
maps each reserved guard to the *enforcement semantics* described in
``doc/guards.md`` and returns the normalized policy verdicts the broker acts on.

Scope (doc/guards.md §Reserved Guards, §Local-only Boundary, §Cross-silo
Contamination, §Policy Verdicts):

* ``local-only`` — data may be processed only on qdistro-controlled local
  compute. A chokepoint that submits guarded content to a remote or unknown
  processing host denies; local processing allows.
* ``no-cross-contaminate`` — a compartment family that must not silently mix
  with a conflicting compartment. Same-compartment flows allow; a flow into a
  conflicting compartment denies (it needs an explicit transfer/declassify
  workflow); a flow that merely *combines* compartments without a registered
  conflict produces a ``contaminate`` (mixed-derivative) verdict.

The registry is keyed by the same vocabulary the schema validates, and
:func:`_assert_registry_covers_vocabulary` fails import if the two ever drift —
a new reserved guard with no behaviour here is a bug, not a silent allow.

This module does **not** implement policy-controlled mutation of security
fields, MCS category allocation, or the conflict/declassification UI — those
remain separate follow-ups. It also does not *produce* ``declassify`` or
``sanitize`` verdicts: those are authority-bearing decisions applied to a flow
from outside, not default enforcement outcomes. The evaluators only ever return
the conservative subset {allow, warn, contaminate, prompt, deny}.

Style mirrors ``qdistro_metadata_schema``: plain functions, frozen collections,
dataclasses, no third-party dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Iterable, cast

from qdistro_metadata_schema import RESERVED_GUARDS

# --------------------------------------------------------------------------
# Normalized verdicts (doc/guards.md §Policy Verdicts)
# --------------------------------------------------------------------------


class GuardVerdict(IntEnum):
    """Normalized guard-aware policy verdicts, ordered least → most restrictive.

    The integer ordering is the combination rule: when more than one guard
    applies to a flow, the aggregate verdict is the most restrictive (highest)
    of the per-guard verdicts (:func:`evaluate_flow`). Only the conservative
    subset is *produced* by the evaluators here; ``transform``/``sanitize``/
    ``declassify`` exist in the vocabulary (doc/guards.md) but are
    authority-bearing decisions applied from outside, so they are not part of
    this enum's default-enforcement ladder.
    """

    #: proceed with no security-field change.
    ALLOW = 0
    #: proceed and apply inherited or added guards (mixed derivative). Produced
    #: when an activity *combines* conflicting inputs into one output (the
    #: lineage/combine path, doc/guards.md §Cross-silo) — not by the single
    #: source→destination flow evaluator here, which allows or denies.
    CONTAMINATE = 1
    #: allow but surface a visible warning and audit record.
    WARN = 2
    #: require owner/admin decision before proceeding.
    PROMPT = 3
    #: refuse.
    DENY = 4

    @property
    def name_lower(self) -> str:
        return self.name.lower()


# --------------------------------------------------------------------------
# Flow context
# --------------------------------------------------------------------------

#: Chokepoints whose default action submits content to a remote endpoint or an
#: otherwise non-local processing host (doc/guards.md §Chokepoint Defaults — the
#: rows that "deny guarded payload"). For ``local-only`` data these deny unless
#: the processing descriptor proves the host is local and no payload egresses.
REMOTE_EGRESS_CHOKEPOINTS = frozenset(
    {
        "browser-upload",
        "api-call",
        "cloud-save",
        "recall-export",
        "backup-export",
        "git-push",
        "issue-upload",
        "pull-request",
        "telemetry",
    }
)
# NOTE: unlike RESERVED_GUARDS (cross-checked by _assert_registry_covers_vocabulary),
# these chokepoint sets are not yet validated against a canonical broker chokepoint
# registry — that registry does not exist yet. A chokepoint name the broker emits
# that is in NEITHER set falls through to the processing-descriptor branch in
# _eval_local_only, which fails closed (deny) unless the host is provably local.

#: Chokepoints that stay local by construction (doc/guards.md §Chokepoint
#: Defaults — the "local ... allowed" rows). ``local-only`` never denies these.
LOCAL_CHOKEPOINTS = frozenset(
    {
        "clipboard",
        "recall-ingest",
        "archive-create",
        "archive-extract",
        "commit-create",
        "backup-local",
    }
)

#: Processing host classes (doc/guards.md §Local-only Boundary "processing:").
#: ``remote-service`` and ``unknown`` are not acceptable for local-only data.
_LOCAL_HOST_CLASSES = frozenset({"local", "local-vm", "local-container"})
_NONLOCAL_HOST_CLASSES = frozenset({"remote-service", "unknown"})


@dataclass(frozen=True)
class ProcessingDescriptor:
    """What an action handler reports about a workflow step's effective host
    (doc/guards.md §Local-only Boundary). Used to decide ``local-only`` for the
    generic ``workflow-step`` chokepoint, where the host is not implied by the
    chokepoint name.

    ``host_class`` is the load-bearing field; ``unknown`` (the default) fails
    closed for local-only data, matching "remote-service and unknown deny".
    """

    host_class: str = "unknown"
    network_egress: str = "unknown"  # none | brokered | unrestricted | unknown
    payload_submitted: bool = True
    telemetry_submitted: bool = False

    @property
    def is_local(self) -> bool:
        return self.host_class in _LOCAL_HOST_CLASSES

    @property
    def submits_remotely(self) -> bool:
        """True when guarded payload/telemetry could leave local control.

        Nothing leaves if no payload/telemetry is submitted. Otherwise a
        non-local host (``remote-service``/``unknown``) always counts as remote,
        and a *local* host counts as remote whenever its egress is anything but
        ``none`` — doc/guards.md §Local-only Boundary: local VMs/containers are
        acceptable only when the action "does not submit guarded content through
        their network egress", so a local VM making a ``brokered`` (or
        ``unrestricted``/``unknown``) call that carries guarded payload denies.
        """
        if not (self.payload_submitted or self.telemetry_submitted):
            return False
        if self.host_class in _NONLOCAL_HOST_CLASSES:
            return True
        return self.network_egress != "none"


@dataclass(frozen=True)
class FlowEndpoint:
    """The security fields of one side of a flow (doc/metadata.md §Security).

    Only the fields the guard registry consults are modelled. ``guards`` is the
    authoritative ``security.guards`` set; ``compartments`` and
    ``conflict_classes`` drive the cross-contamination rule.
    """

    guards: frozenset[str] = frozenset()
    compartments: frozenset[str] = frozenset()
    conflict_classes: frozenset[str] = frozenset()

    @classmethod
    def from_security(cls, security: object) -> "FlowEndpoint":
        """Build an endpoint from a manifest ``security`` mapping (tolerant of
        ``None``/missing/non-list fields — those become empty sets)."""

        def _set(key: str) -> frozenset[str]:
            if not isinstance(security, dict):
                return frozenset()
            val = security.get(key)
            if not isinstance(val, (list, tuple, set, frozenset)):
                return frozenset()
            return frozenset(v for v in val if isinstance(v, str))

        return cls(
            guards=_set("guards"),
            compartments=_set("compartments"),
            conflict_classes=_set("conflictClasses"),
        )


@dataclass(frozen=True)
class FlowContext:
    """A brokered flow under evaluation: data moving from ``source`` through
    ``chokepoint`` toward ``destination``, processed per ``processing``."""

    source: FlowEndpoint
    chokepoint: str
    destination: FlowEndpoint = field(default_factory=FlowEndpoint)
    processing: ProcessingDescriptor = field(default_factory=ProcessingDescriptor)


@dataclass(frozen=True)
class GuardDecision:
    """One guard's verdict plus a human-readable reason and the guards that the
    output should inherit if the flow proceeds."""

    guard: str
    verdict: GuardVerdict
    reason: str
    propagate: frozenset[str] = frozenset()


# --------------------------------------------------------------------------
# Per-guard behavioural evaluators
# --------------------------------------------------------------------------


def _eval_local_only(ctx: FlowContext) -> GuardDecision:
    """``local-only``: deny submission of guarded content to a remote/unknown
    host; allow local processing (doc/guards.md §Local-only Boundary)."""
    guard = "local-only"
    propagate = frozenset({guard})  # viral: derived data inherits local-only

    if ctx.chokepoint in LOCAL_CHOKEPOINTS:
        return GuardDecision(
            guard, GuardVerdict.ALLOW,
            f"{ctx.chokepoint!r} is local-only-controlled compute", propagate,
        )

    if ctx.chokepoint in REMOTE_EGRESS_CHOKEPOINTS:
        return GuardDecision(
            guard, GuardVerdict.DENY,
            f"{ctx.chokepoint!r} submits local-only content to a remote endpoint",
            propagate,
        )

    # Generic / workflow-step chokepoint: decide on the reported processing host.
    if ctx.processing.submits_remotely:
        return GuardDecision(
            guard, GuardVerdict.DENY,
            f"effective processing host {ctx.processing.host_class!r} / egress "
            f"{ctx.processing.network_egress!r} submits local-only content remotely",
            propagate,
        )
    if ctx.processing.is_local:
        return GuardDecision(
            guard, GuardVerdict.ALLOW,
            f"effective processing host {ctx.processing.host_class!r} is local",
            propagate,
        )
    # host_class not provably local and not provably remote → fail closed → deny.
    return GuardDecision(
        guard, GuardVerdict.DENY,
        f"effective processing host {ctx.processing.host_class!r} is not "
        f"provably local; local-only fails closed",
        propagate,
    )


def _eval_no_cross_contaminate(ctx: FlowContext) -> GuardDecision:
    """``no-cross-contaminate``: allow a same-compartment continuation; deny a
    flow into a conflicting, foreign, or unclassified compartment under a shared
    conflict class (doc/guards.md §Cross-silo Contamination).

    The flow rule (doc/guards.md):
        if source.guards contains no-cross-contaminate
        and source.conflictClasses intersects destination.conflictClasses
        and source.compartments is not subset-compatible with destination:
            deny or require an explicit transfer workflow
    Subset-compatible means the source is *classified* (has at least one
    compartment) and all of its compartments are present in the destination —
    i.e. data is not entering a strictly-foreign or unclassified compartment.
    An empty source or empty/foreign destination under a shared conflict class
    is NOT subset-compatible and fails closed to DENY rather than allowing a
    cross-silo leak. (The guard only propagates itself here; the monotonic union
    of compartments/conflictClasses onto the output, doc/guards.md §Propagation,
    is the lineage layer's job, not this default-enforcement function's.)
    """
    guard = "no-cross-contaminate"
    src, dst = ctx.source, ctx.destination
    propagate = frozenset({guard})

    shared_conflict = src.conflict_classes & dst.conflict_classes
    if not shared_conflict:
        # No conflict class in common → the guard does not constrain this flow.
        return GuardDecision(
            guard, GuardVerdict.ALLOW,
            "no shared conflict class between source and destination", propagate,
        )

    # A classified source whose compartments the destination already covers is
    # a same-compartment continuation (doc/guards.md §Cross-silo: subset-compatible).
    # Note: a destination that itself unions extra compartments under the shared
    # conflict class is still treated as a valid (broker-created) wider silo;
    # refining that requires a compartment->conflict-class map not yet modelled.
    subset_compatible = bool(src.compartments) and src.compartments <= dst.compartments
    if subset_compatible:
        return GuardDecision(
            guard, GuardVerdict.ALLOW,
            f"destination compartments {sorted(dst.compartments)} already cover "
            f"source compartments {sorted(src.compartments)} under conflict class "
            f"{sorted(shared_conflict)}",
            propagate,
        )

    # Shared conflict class but not subset-compatible: an unclassified source,
    # an unclassified destination sink, or a strictly-foreign destination
    # compartment. doc/guards.md §Cross-silo: "deny or require an explicit
    # transfer workflow". Fail closed → DENY; an explicit transfer/declassify
    # workflow is the out-of-band override, applied from outside this function.
    return GuardDecision(
        guard, GuardVerdict.DENY,
        f"source compartments {sorted(src.compartments)} are not subset-compatible "
        f"with destination {sorted(dst.compartments)} under conflict class "
        f"{sorted(shared_conflict)}; needs an explicit transfer/declassify workflow",
        propagate,
    )


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardBehaviour:
    """Registry entry: a reserved guard's metadata + its enforcement evaluator."""

    name: str
    #: viral guards propagate to every derived artifact (doc/guards.md §Propagation).
    viral: bool
    description: str
    evaluate: Callable[[FlowContext], GuardDecision]


#: The behavioural registry, keyed by the same names as
#: :data:`~qdistro_metadata_schema.RESERVED_GUARDS`.
GUARD_REGISTRY: dict[str, GuardBehaviour] = {
    "local-only": GuardBehaviour(
        name="local-only",
        viral=True,
        description=(
            "Data may be processed only on qdistro-controlled local compute; "
            "submitting it to a remote/unknown host is denied."
        ),
        evaluate=_eval_local_only,
    ),
    "no-cross-contaminate": GuardBehaviour(
        name="no-cross-contaminate",
        viral=True,
        description=(
            "A compartment family that must not silently mix with a conflicting "
            "compartment; cross-compartment flows deny or require a workflow."
        ),
        evaluate=_eval_no_cross_contaminate,
    ),
}


def _assert_registry_covers_vocabulary() -> None:
    """Fail import if the behavioural registry and the static vocabulary drift.

    A guard reserved in :data:`RESERVED_GUARDS` with no behaviour here would
    silently allow guarded flows; a behaviour for an unreserved name would be
    dead code the schema rejects. Either is a bug.
    """
    missing = RESERVED_GUARDS - GUARD_REGISTRY.keys()
    extra = GUARD_REGISTRY.keys() - RESERVED_GUARDS
    if missing or extra:
        raise RuntimeError(
            "guard registry out of sync with RESERVED_GUARDS: "
            f"missing behaviour for {sorted(missing)}, "
            f"behaviour for unreserved {sorted(extra)}"
        )


_assert_registry_covers_vocabulary()


# --------------------------------------------------------------------------
# Aggregate entry points
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowVerdict:
    """Aggregate outcome of evaluating every guard on a flow's source."""

    verdict: GuardVerdict
    decisions: tuple[GuardDecision, ...]
    #: guards the output should inherit if the flow proceeds (union of the
    #: per-guard ``propagate`` sets — monotonic, doc/guards.md §Propagation).
    propagate: frozenset[str]

    @property
    def allowed(self) -> bool:
        """True when the flow may proceed without owner/admin intervention.
        ``contaminate`` and ``warn`` proceed (with side effects); ``prompt`` and
        ``deny`` do not."""
        return self.verdict <= GuardVerdict.WARN

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(d.reason for d in self.decisions)


def evaluate_flow(ctx: FlowContext) -> FlowVerdict:
    """Evaluate every guard present on ``ctx.source`` against the flow.

    The aggregate verdict is the most restrictive per-guard verdict. Guards on
    the source that are not in the registry are ignored here — the schema layer
    is responsible for rejecting unknown guards before they reach policy — but
    their presence is still propagated so an unrecognised guard never silently
    drops off a derived artifact.

    Only the *source's* guards are enforced here: the guard rules in
    doc/guards.md are written source-centrically ("if source.guards contains
    ..."). Guards carried by the destination constrain flows *out of* it, which
    is a separate evaluation with that endpoint as the source.
    """
    decisions: list[GuardDecision] = []
    propagate: set[str] = set()
    worst = GuardVerdict.ALLOW

    for guard in sorted(ctx.source.guards):
        behaviour = GUARD_REGISTRY.get(guard)
        if behaviour is None:
            # Unknown guard: carry it forward conservatively, but it has no
            # behaviour to enforce (schema validation owns rejecting it).
            propagate.add(guard)
            continue
        decision = behaviour.evaluate(ctx)
        decisions.append(decision)
        propagate.update(decision.propagate)
        worst = max(worst, decision.verdict)

    return FlowVerdict(
        verdict=worst,
        decisions=tuple(decisions),
        propagate=frozenset(propagate),
    )


def propagate_guards(*input_guard_sets: object) -> frozenset[str]:
    """Default monotonic guard propagation (doc/guards.md §Propagation):
    ``output.guards = union(inputs.guards)``.

    Each argument is an iterable of guard-name strings (tolerant of ``None`` and
    non-string members). A trusted action handler may record a narrower
    input-to-output mapping elsewhere; absent that, the broker uses this union.
    """
    out: set[str] = set()
    for s in input_guard_sets:
        if s is None:
            continue
        try:
            # s is typed `object` (callers may pass junk); the try/except is the
            # real runtime guard against a non-iterable. Cast so mypy accepts
            # the iteration — a genuine non-iterable still raises TypeError here.
            out.update(g for g in cast(Iterable[Any], s) if isinstance(g, str))
        except TypeError:
            continue
    return frozenset(out)
