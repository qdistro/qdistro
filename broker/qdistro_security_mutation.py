"""Policy-controlled mutation of security fields (doc/metadata.md §Mutability,
doc/guards.md §Declassification, §Policy Verdicts).

doc/metadata.md §Mutability splits a resource's fields into three tiers:

* *mutable-by-default* — descriptive labels, annotations, display names, status
  conditions. Not this module's concern.
* *policy-controlled mutation* — ``sensitivity``, ``guards``, ``compartments``,
  ``conflictClasses``, ``authority``, ``declassification`` state. This module
  owns the gate on these.
* *immutable after record creation* — audit decision rows, lineage edges,
  workflow-run i/o refs, artifact hashes, declassification evidence. This module
  never edits those; it only ever *appends* new sealed records.

The load-bearing rule (doc/metadata.md §Mutability): "A reclassification records
a NEW event and may add propagated guard facts to downstream entities. It must
not rewrite old broker decisions as if they were made under the new
classification." So every mutation here is append-only against
:class:`~qdistro_lineage_store.LineageStore`: a new entity snapshot row (the
highest-id row is current state, ``get_entity`` returns it) plus a sealed
assertion that is the tamper-evident evidence of *what changed, who authorised
it, and under what verdict*. The earlier snapshot row is never touched.

Three change classes, with very different authority requirements:

* **TIGHTEN (widen the guard set)** — adding a guard/compartment/conflict class,
  or raising sensitivity. This is *monotonically more restrictive*: it can only
  reduce what a holder may do, so it never escalates privilege. Allowed with an
  ordinary authority record; recorded in place as a new snapshot + a sealed
  ``reclassify`` assertion. May also propagate the added guard facts to guarded
  descendants (doc/metadata.md §Mutability "may add propagated guard facts to
  downstream entities").

* **NARROW (declassification)** — removing a guard/compartment/conflict class or
  lowering sensitivity. doc/guards.md §Declassification is explicit: this is a
  *workflow, not a tag deletion*; the source stays guarded and the result is a
  *new tracked derivative* whose guard set is narrowed by explicit authority.
  This module therefore NEVER narrows a source entity in place. A narrowing
  request is routed to :func:`qdistro_lineage.declassify`, which mints a new
  output entity ``wasDerivedFrom`` the (still-guarded) source and records the
  full sealed declassification evidence. It requires (a) a non-empty approving
  authority and (b) an authority-bearing ``declassify`` verdict — absent either,
  it FAILS CLOSED with no write at all.

* **STATE (declassification-state transition)** — ``none → requested →
  approved``. A pure workflow-state move that does **not** itself change the
  actual guard/compartment set, so it is recorded in place as a new snapshot +
  sealed assertion. ``approved`` does not narrow anything by itself; it is the
  authority record a subsequent NARROW (declassify) consumes.

Delegation (decisions/security-field-mutation-authority.md): WHO may attempt a
mutation is a separate, capability-based gate that runs *before* the change is
classified or written. The admin app holds default authority over every field;
any other principal must hold a matching per-field or wildcard grant in the
:class:`~qdistro_security_grants.SecurityGrantStore`, or the mutation is denied
fail-closed with no write. The delegation gate checks **every security field the
change actually touches** (e.g. a request that moves both ``guards`` and
``sensitivity`` needs authority for both), and the authorising scope is recorded
in the sealed evidence so an auditor can see under which grant a mutation ran.

Fail-closed posture: a principal without a matching grant, an unknown field, a
non-monotonic mess of simultaneous add+remove without authority, a narrowing
without authority/verdict, or any inability to write the sealed evidence aborts
the whole transaction and denies.

Style mirrors the other broker modules: plain functions + dataclasses, frozen
sets, stdlib only, bare top-level imports (the broker dir is on the path).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from qdistro_lineage import declassify
from qdistro_lineage_store import Entity, LineageStore
from qdistro_security_grants import GrantCheck, SecurityGrantStore

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: Sensitivity ladder, least → most restrictive (doc/metadata.md §Security).
#: Raising sensitivity is a TIGHTEN; lowering it is a NARROW (needs authority).
SENSITIVITY_ORDER = ("public", "internal", "confidential", "restricted")
_SENSITIVITY_RANK = {s: i for i, s in enumerate(SENSITIVITY_ORDER)}

#: Declassification-state machine (doc/metadata.md §Security
#: ``declassification: none | requested | approved``). Only forward moves are
#: allowed; ``approved`` is terminal until consumed by a declassify workflow.
DECLASSIFICATION_STATES = ("none", "requested", "approved")
_DECLASS_RANK = {s: i for i, s in enumerate(DECLASSIFICATION_STATES)}

#: The authority-bearing verdict a NARROW must carry (doc/guards.md §Policy
#: Verdicts: "declassify: produce a tracked derivative whose guard set is
#: narrowed by explicit authority"). A plain string is not sufficient on its
#: own — the caller must pass a verdict that was reached by broker policy /
#: admin decision and is itself recorded as sealed evidence below.
DECLASSIFY_VERDICT = "declassify"


class ChangeClass(Enum):
    """How a requested security-field change relates to the current state."""

    #: monotonically more restrictive (add guard, raise sensitivity, ...).
    TIGHTEN = "tighten"
    #: reduces restriction (remove guard, lower sensitivity, ...): declassify.
    NARROW = "narrow"
    #: a declassification-state transition that does not change the guard set.
    STATE = "state"
    #: nothing actually changes.
    NOOP = "noop"


class SecurityMutationError(ValueError):
    """Raised on a malformed request before any write (fail closed)."""


# --------------------------------------------------------------------------
# Request / result value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityChange:
    """A requested change to an entity's policy-controlled security fields.

    Every field is optional; ``None`` means "leave as-is". The fields mirror
    doc/metadata.md §Security's policy-controlled tier. ``guards`` /
    ``compartments`` / ``conflict_classes`` are the *desired full set* (not a
    delta), so the classifier can tell add-only from remove apart by comparison
    with the current snapshot.
    """

    guards: frozenset[str] | None = None
    compartments: frozenset[str] | None = None
    conflict_classes: frozenset[str] | None = None
    sensitivity: str | None = None
    declassification: str | None = None


@dataclass(frozen=True)
class MutationResult:
    """Outcome of a mutation attempt."""

    allowed: bool
    change_class: ChangeClass
    reason: str
    #: the eid that now carries the result. For TIGHTEN/STATE this is the same
    #: source eid (new snapshot row); for an allowed NARROW it is the NEW
    #: derivative entity declassify() minted; for a denial it is None.
    result_eid: str | None = None
    #: descendants that should be reviewed after a tighten that propagated facts
    #: (doc/metadata.md §Mutability), or that remain guarded under a still-guarded
    #: source after a declassification (advisory).
    descendants_for_review: frozenset[str] = field(default_factory=frozenset)
    #: per-field authorising scope the delegation gate used (field -> scope, e.g.
    #: ``{"security.guards": "admin-app-default"}`` or ``{"security.sensitivity":
    #: "security.*"}``). Empty for a NOOP. Recorded in the sealed evidence.
    delegated_via: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Classification (pure)
# --------------------------------------------------------------------------


def _is_subset_or_equal(new: frozenset[str], cur: frozenset[str]) -> bool:
    return new <= cur


def classify_change(current: Entity, change: SecurityChange) -> ChangeClass:
    """Classify a requested change against the current snapshot, fail-closed.

    NARROW dominates: if *any* dimension of the request reduces restriction
    (removes a guard/compartment/conflict class or lowers sensitivity) the whole
    request is a NARROW and must go through the declassify gate — you cannot
    smuggle a guard removal alongside a tighten and have it treated as a tighten.

    A pure ``declassification`` state forward-move with no security-field change
    is STATE. Anything that only adds restrictions is TIGHTEN. No change is NOOP.
    """
    narrows = False
    tightens = False

    if change.guards is not None:
        if not change.guards >= current.guards:  # removes at least one guard
            narrows = True
        if change.guards > current.guards:
            tightens = True
    if change.compartments is not None:
        if not change.compartments >= current.compartments:
            narrows = True
        if change.compartments > current.compartments:
            tightens = True
    if change.conflict_classes is not None:
        if not change.conflict_classes >= current.conflict_classes:
            narrows = True
        if change.conflict_classes > current.conflict_classes:
            tightens = True
    if change.sensitivity is not None:
        cur_rank = _sensitivity_rank(_current_sensitivity(current))
        new_rank = _sensitivity_rank(change.sensitivity)
        if new_rank < cur_rank:
            narrows = True
        elif new_rank > cur_rank:
            tightens = True

    if narrows:
        return ChangeClass.NARROW
    if tightens:
        return ChangeClass.TIGHTEN

    # No guard-set change. A declassification-state forward move is STATE.
    if change.declassification is not None:
        cur_state = _current_declass_state(current)
        if change.declassification != cur_state:
            return ChangeClass.STATE
    return ChangeClass.NOOP


# Sensitivity / declassification state are stored as advisory facets on the
# entity snapshot (the lineage store's typed columns cover guards/compartments/
# conflictClasses/integrity; sensitivity + declassification live in `facets`,
# which is part of the sealed entity body, so they are tamper-evident too).

def _current_sensitivity(ent: Entity) -> str:
    val = ent.facets.get("sensitivity") if isinstance(ent.facets, dict) else None
    return val if isinstance(val, str) and val in _SENSITIVITY_RANK else "internal"


def _current_declass_state(ent: Entity) -> str:
    val = ent.facets.get("declassification") if isinstance(ent.facets, dict) else None
    return val if isinstance(val, str) and val in _DECLASS_RANK else "none"


def touched_fields(current: Entity, change: SecurityChange) -> frozenset[str]:
    """The set of canonical ``security.*`` fields this change actually MOVES.

    A field whose requested value equals the current value is NOT touched (so a
    NOOP needs no grant), and a field left as ``None`` is never touched. This is
    what the delegation gate authorises against: you need a grant only for the
    fields you are actually changing. Fail-closed callers gate on this set; an
    empty set means nothing is being mutated.
    """
    fields: set[str] = set()
    if change.guards is not None and frozenset(change.guards) != current.guards:
        fields.add("security.guards")
    if (change.compartments is not None
            and frozenset(change.compartments) != current.compartments):
        fields.add("security.compartments")
    if (change.conflict_classes is not None
            and frozenset(change.conflict_classes) != current.conflict_classes):
        fields.add("security.conflict_classes")
    if (change.sensitivity is not None
            and change.sensitivity != _current_sensitivity(current)):
        fields.add("security.sensitivity")
    if (change.declassification is not None
            and change.declassification != _current_declass_state(current)):
        fields.add("security.declassification")
    return frozenset(fields)


def _sensitivity_rank(s: str) -> int:
    if s not in _SENSITIVITY_RANK:
        # Unknown sensitivity fails closed: treat as the most restrictive so a
        # request *to* an unknown value never reads as a lowering, and a request
        # *from* an unknown value never reads as a raise that drops the guard.
        return len(SENSITIVITY_ORDER)
    return _SENSITIVITY_RANK[s]


# --------------------------------------------------------------------------
# The mutator
# --------------------------------------------------------------------------


class SecurityMutator:
    """Policy gate over security-field mutation, backed by a LineageStore.

    Construct it with the store; call :meth:`reclassify`. The mutator never
    deletes or edits an existing row — TIGHTEN/STATE append a new entity
    snapshot, NARROW delegates to :func:`qdistro_lineage.declassify` (new
    derivative entity). All authority is recorded as sealed assertions.
    """

    def __init__(self, store: LineageStore, *,
                 grant_store: SecurityGrantStore | None = None,
                 now: Any | None = None):
        self._store = store
        self._now = now or (lambda: int(time.time()))
        #: The capability/delegation gate. Constructed against the same store if
        #: not supplied (so it shares the sealed hash chain). WHO may mutate is
        #: decided here; WHAT is a legal move is decided by classify_change.
        self._grants = grant_store or SecurityGrantStore(store, now=self._now)

    @property
    def grants(self) -> SecurityGrantStore:
        return self._grants

    def _check_delegation(
        self, principal: str, fields: frozenset[str],
    ) -> tuple[bool, str, dict[str, str]]:
        """Fail-closed delegation gate: the calling principal must be authorised
        for EVERY security field the change touches (admin app by default, anyone
        else via a matching per-field/wildcard grant). Returns
        ``(allowed, reason, {field: authorising_scope})``. The first ungranted
        field denies the whole request — you cannot partially apply a multi-field
        change."""
        via: dict[str, str] = {}
        for f in sorted(fields):
            chk: GrantCheck = self._grants.check(principal, f)
            if not chk.allowed:
                return False, chk.reason, {}
            via[f] = chk.via or "unknown"
        return True, "", via

    # -- public API --------------------------------------------------------

    def reclassify(
        self,
        eid: str,
        change: SecurityChange,
        *,
        actor: str,
        principal: str | None = None,
        authority: str | None = None,
        reason: str = "",
        policy_verdict: str | None = None,
        propagate_to_descendants: bool = False,
        # NARROW-only declassification evidence (doc/guards.md §Declassification):
        output_kind: str = "export",
        output_digest: str | None = None,
        transform: str | None = None,
        destination: str | None = None,
    ) -> MutationResult:
        """Apply a policy-controlled security-field change, fail closed.

        * **TIGHTEN** (more restrictive): allowed with ``actor`` (and
          ``authority`` if present, recorded for audit). Appends a new snapshot
          + a sealed ``reclassify`` assertion. With ``propagate_to_descendants``
          the added guard facts are recorded on guarded descendants too.
        * **NARROW** (declassification): requires a non-empty ``authority`` AND
          ``policy_verdict == "declassify"``. Routes to
          :func:`qdistro_lineage.declassify`, which mints a NEW derivative whose
          guard set is the requested (narrower) set, ``wasDerivedFrom`` the
          source; the source row is left guarded and untouched. Missing
          authority or verdict → DENY, no write.
        * **STATE** (``none→requested→approved``): a forward-only declassification
          state move with no guard-set change; appended as a new snapshot +
          assertion. ``approved`` requires an ``authority`` (it is the authority
          record a later declassify consumes); a backward move is denied.
        * **NOOP**: allowed, no write.

        Before any of the above, the **delegation gate** runs (fail closed): the
        calling ``principal`` (defaults to the admin-app principal of the grant
        store, so an unspecified principal is the privileged admin app) must be
        authorised for every security field the change touches. Any ungranted
        field denies the whole request with no write. The NARROW/STATE
        authority+verdict checks are *in addition to* the delegation gate, not a
        substitute for it: being allowed to declassify (NARROW) still requires a
        grant for the fields you narrow.
        """
        current = self._store.get_entity(eid)
        if current is None:
            raise SecurityMutationError(
                f"cannot mutate unknown entity {eid!r} (no recorded snapshot)"
            )
        if not actor:
            raise SecurityMutationError("a non-empty actor is required")

        klass = classify_change(current, change)

        if klass is ChangeClass.NOOP:
            return MutationResult(True, klass, "no security-field change requested", eid)

        # --- delegation gate (WHO), fail closed, before any write ---
        # An unspecified principal is the privileged admin app (default authority);
        # any named non-admin principal must hold a matching grant for EVERY field
        # this change touches.
        who = principal if principal is not None else self._grants.admin_app
        touched = touched_fields(current, change)
        ok, deny_reason, via = self._check_delegation(who, touched)
        if not ok:
            return MutationResult(False, klass, f"delegation denied: {deny_reason}")

        if klass is ChangeClass.NARROW:
            return self._do_narrow(
                current, change,
                actor=actor, authority=authority, reason=reason, delegated_via=via,
                policy_verdict=policy_verdict, output_kind=output_kind,
                output_digest=output_digest, transform=transform,
                destination=destination,
            )

        if klass is ChangeClass.STATE:
            return self._do_state(current, change, actor=actor, authority=authority,
                                  reason=reason, delegated_via=via)

        # TIGHTEN
        return self._do_tighten(
            current, change, actor=actor, authority=authority, reason=reason,
            propagate_to_descendants=propagate_to_descendants, delegated_via=via,
        )

    # -- TIGHTEN -----------------------------------------------------------

    def _do_tighten(
        self, current: Entity, change: SecurityChange, *, actor: str,
        authority: str | None, reason: str, propagate_to_descendants: bool,
        delegated_via: dict[str, str],
    ) -> MutationResult:
        new_guards = change.guards if change.guards is not None else current.guards
        new_comps = (change.compartments if change.compartments is not None
                     else current.compartments)
        new_confs = (change.conflict_classes if change.conflict_classes is not None
                     else current.conflict_classes)
        new_sens = change.sensitivity or _current_sensitivity(current)
        new_facets = dict(current.facets) if isinstance(current.facets, dict) else {}
        new_facets["sensitivity"] = new_sens
        if change.declassification is not None:
            new_facets["declassification"] = change.declassification

        added_guards = frozenset(new_guards) - current.guards
        ts = self._now()
        reviewed: set[str] = set()
        with self._store.transaction():
            self._store.record_entity(
                Entity(
                    eid=current.eid, kind=current.kind, digest=current.digest,
                    locator=current.locator, guards=frozenset(new_guards),
                    compartments=frozenset(new_comps),
                    conflict_classes=frozenset(new_confs),
                    integrity=current.integrity, status=current.status,
                    facets=new_facets, created_at=ts,
                )
            )
            self._store.record_assertion(
                subject=current.eid, fact="security.reclassify",
                value={
                    "class": "tighten",
                    "added_guards": sorted(added_guards),
                    "guards": sorted(new_guards),
                    "compartments": sorted(new_comps),
                    "conflict_classes": sorted(new_confs),
                    "sensitivity": new_sens,
                    "actor": actor, "authority": authority, "reason": reason,
                    "delegated_via": dict(delegated_via),
                },
                asserted_by=actor, authority="broker-derived",
            )
            # Optionally push the *added* guard facts onto guarded descendants
            # (doc/metadata.md §Mutability: a reclassification "may add propagated
            # guard facts to downstream entities"). This is a sealed assertion on
            # each descendant — never an edit of its row, never a removal.
            if propagate_to_descendants and added_guards:
                for d in self._store.downstream(current.eid):
                    self._store.record_assertion(
                        subject=d, fact="security.propagated_guards",
                        value={"from": current.eid, "added_guards": sorted(added_guards),
                               "actor": actor},
                        asserted_by=actor, authority="broker-derived",
                    )
                    reviewed.add(d)
        return MutationResult(
            True, ChangeClass.TIGHTEN,
            f"tightened {current.eid} (added guards {sorted(added_guards)})", current.eid,
            descendants_for_review=frozenset(reviewed), delegated_via=dict(delegated_via),
        )

    # -- STATE -------------------------------------------------------------

    def _do_state(
        self, current: Entity, change: SecurityChange, *, actor: str,
        authority: str | None, reason: str, delegated_via: dict[str, str],
    ) -> MutationResult:
        target = change.declassification
        cur_state = _current_declass_state(current)
        if target not in _DECLASS_RANK:
            raise SecurityMutationError(
                f"unknown declassification state {target!r} "
                f"(allowed: {list(DECLASSIFICATION_STATES)})"
            )
        # Forward-only. A backward move (approved→requested, requested→none) would
        # erase an authority record, so it is denied (fail closed).
        if _DECLASS_RANK[target] <= _DECLASS_RANK[cur_state]:
            return MutationResult(
                False, ChangeClass.STATE,
                f"declassification state cannot move {cur_state!r} -> {target!r} "
                f"(forward-only)",
            )
        # Reaching `approved` is itself an authority-bearing act.
        if target == "approved" and not authority:
            return MutationResult(
                False, ChangeClass.STATE,
                "declassification 'approved' requires an explicit authority",
            )
        new_facets = dict(current.facets) if isinstance(current.facets, dict) else {}
        new_facets["declassification"] = target
        ts = self._now()
        with self._store.transaction():
            self._store.record_entity(
                Entity(
                    eid=current.eid, kind=current.kind, digest=current.digest,
                    locator=current.locator, guards=current.guards,
                    compartments=current.compartments,
                    conflict_classes=current.conflict_classes,
                    integrity=current.integrity, status=current.status,
                    facets=new_facets, created_at=ts,
                )
            )
            self._store.record_assertion(
                subject=current.eid, fact="security.declassification_state",
                value={"from": cur_state, "to": target, "actor": actor,
                       "authority": authority, "reason": reason,
                       "delegated_via": dict(delegated_via)},
                asserted_by=actor, authority="broker-derived",
            )
        return MutationResult(
            True, ChangeClass.STATE,
            f"declassification state {cur_state!r} -> {target!r}", current.eid,
            delegated_via=dict(delegated_via),
        )

    # -- NARROW (declassification) -----------------------------------------

    def _do_narrow(
        self, current: Entity, change: SecurityChange, *, actor: str,
        authority: str | None, reason: str, policy_verdict: str | None,
        output_kind: str, output_digest: str | None, transform: str | None,
        destination: str | None, delegated_via: dict[str, str],
    ) -> MutationResult:
        # Fail closed: declassification needs BOTH an explicit approving authority
        # and the authority-bearing 'declassify' verdict. Either missing → DENY,
        # no write at all (doc/guards.md §Declassification, §Policy Verdicts).
        if not authority:
            return MutationResult(
                False, ChangeClass.NARROW,
                "narrowing security fields is a declassification and requires an "
                "explicit approving authority; none supplied",
            )
        if policy_verdict != DECLASSIFY_VERDICT:
            return MutationResult(
                False, ChangeClass.NARROW,
                f"narrowing requires the authority-bearing {DECLASSIFY_VERDICT!r} "
                f"verdict; got {policy_verdict!r}",
            )

        # The residual (narrower) security fields are the requested sets, falling
        # back to the source's current sets where the request leaves a field as-is
        # (a None field is "unchanged", which for a narrow means it stays as the
        # source had it).
        residual_guards = change.guards if change.guards is not None else current.guards
        residual_comps = (change.compartments if change.compartments is not None
                          else current.compartments)
        residual_confs = (change.conflict_classes if change.conflict_classes is not None
                          else current.conflict_classes)

        # Atomic: declassify() mints the new derivative wasDerivedFrom the
        # (untouched, still-guarded) source and writes the core sealed
        # declassification evidence; we then append the destination / intended
        # use / verdict / delegated_via context. Both must commit together — a
        # crash between them must NOT leave a declassified derivative whose
        # delegation/destination evidence is missing (the audit dimension a
        # declassification-authority system most needs intact). declassify()'s
        # own _txn joins this outer transaction (record store is re-entrant), so
        # the whole narrow is all-or-nothing, like _do_tighten / _do_state.
        with self._store.transaction():
            out_eid = declassify(
                self._store,
                source_eid=current.eid,
                output_kind=output_kind,
                output_digest=output_digest,
                residual_guards=sorted(residual_guards),
                residual_compartments=sorted(residual_comps),
                residual_conflict_classes=sorted(residual_confs),
                authority_gid=authority,
                transform=transform,
                agent_gid=actor,
                now=self._now(),
            )
            # destination / intended use + verdict + reason + delegated_via
            # (doc/guards.md §Declassification requires destination + intended
            # use in the evidence).
            self._store.record_assertion(
                subject=out_eid, fact="declassification.context",
                value={"source": current.eid, "destination": destination,
                       "policy_verdict": policy_verdict, "reason": reason,
                       "actor": actor, "authority": authority,
                       "delegated_via": dict(delegated_via)},
                asserted_by=authority, authority="broker-derived",
            )
        # The source remains guarded; its guarded descendants are advisory review.
        descendants = self._store.downstream(current.eid)
        return MutationResult(
            True, ChangeClass.NARROW,
            f"declassified {current.eid} -> new derivative {out_eid} "
            f"(residual guards {sorted(residual_guards)})",
            out_eid, descendants_for_review=frozenset(descendants),
            delegated_via=dict(delegated_via),
        )
