"""Broker-owned commit-creation chokepoint (doc/lineage.md §Chokepoints
"commit creation and signing", §Commit Example).

This is the production SURFACE that ``qdistro_lineage_receipts`` documents as
shipping ahead of wiring: the git-trailer helpers are pure transforms a
"broker-owned commit handler" calls, and this module is that handler. It does
three things, in order, so the trailer is part of the bytes that get signed:

1. record the ``commit-create`` chokepoint into the central store
   (:func:`qdistro_lineage.record_chokepoint` — guard verdict + activity +
   commit entity + ``used``/``wasGeneratedBy``/``wasDerivedFrom`` edges). The
   chokepoint name and its ``commit`` activity kind already exist in
   ``CHOKEPOINT_ACTIVITY_KIND``; we consume that, we do not reimplement guards;
2. build + seal a ``git-trailer`` receipt envelope for the commit entity
   (:meth:`LineageStore.record_receipt` with the full envelope as payload, so
   :func:`qdistro_lineage_receipts.verify_against_store` can authenticate every
   field later);
3. emit the portable ``Qdistro-Lineage`` trailer into the commit message
   (:func:`qdistro_lineage_receipts.append_git_trailer`) and return the new
   message for the caller to sign.

Trust split (matches ``qdistro_export_lineage`` and doc/lineage.md §Authority):
the broker mints the receipt and assembles the message; the SIGNING happens
outside this module (the agent-relay SSH/GPG path in ``workflow/agent_relay.py``)
on the message this function returns. The trailer is a parseable receipt, not
integrity by itself — a signed commit plus the central sealed row provide that
(doc/lineage.md §Commit Example).

A *denied* commit (a guarded source that may not flow into a commit, e.g. a
local-only secret captured into the tree) still records the attempt for audit
but mints no commit entity and emits no trailer — the caller must fail the
commit. This is the fail-closed direction: a guarded source never silently
acquires a clean-looking commit receipt.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import qdistro_lineage_receipts as lr
from qdistro_guard_registry import FlowEndpoint, ProcessingDescriptor
from qdistro_lineage import ChokepointInput, record_chokepoint
from qdistro_lineage_store import LineageStore

LINEAGE_ISSUER = "qdistro-broker"

#: A git commit is assembled and signed on the local host; the broker does not
#: ship the tree/diff to a remote service to create the commit.
_COMMIT_PROCESSING = ProcessingDescriptor(
    host_class="local", network_egress="none",
    payload_submitted=False, telemetry_submitted=False,
)


class CommitLineageError(ValueError):
    """Base class for malformed commit-lineage inputs."""


class BadCommitInput(CommitLineageError):
    """The commit descriptor shape is invalid."""


class CommitDenied(CommitLineageError):
    """The guard verdict denied the commit; no trailer is emitted. The caller
    must abort the commit rather than sign an unguarded message."""

    def __init__(self, reasons: tuple[str, ...]):
        super().__init__("commit denied by guard policy: " + "; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class CommitSource:
    """One input that contributed to the commit: the ``eid`` of a tracked
    file/resource entity ALREADY recorded in the store. The security snapshot the
    chokepoint enforces is read from that recorded entity, NOT supplied here —
    doc/lineage.md §Authority: "app-reported lineage cannot authorize"; the
    authoritative guards/compartments come from broker decisions in the store. A
    caller that has not recorded the source entity (with its broker-derived
    snapshot) cannot launder it through this chokepoint."""

    eid: str


@dataclass(frozen=True)
class CommitLineageResult:
    """Outcome of the commit chokepoint: the assembled message (trailer appended),
    the minted commit entity, the activity, and the chain head at emission."""

    message: str
    commit_eid: str
    activity_aid: str
    receipt: dict
    chain_head: str


def record_commit(
    store: LineageStore,
    *,
    message: str,
    commit_eid: str,
    sources: Sequence[CommitSource],
    tree_digest: str | None = None,
    branch: str | None = None,
    dest_compartments: Sequence[str] = (),
    dest_conflict_classes: Sequence[str] = (),
    agent_gid: str | None = None,
    action_version: str = "git-commit/v1",
    now: int | None = None,
) -> CommitLineageResult:
    """Record the commit chokepoint, seal a git-trailer receipt, and return the
    commit ``message`` with the ``Qdistro-Lineage`` trailer appended (BEFORE the
    caller signs). ``sources`` are the tracked input entities whose guards the
    commit inherits; at least one is required (a deriving chokepoint with no
    input would mint a source-free clean commit — exactly what fail-closed
    forbids). ``tree_digest`` is the sha256 of the commit tree/content when
    known; it binds the receipt to immutable bytes. ``branch`` is a secondary
    mutable locator. ``dest_compartments``/``dest_conflict_classes`` describe the
    repository the commit lands in (a repo belongs to a compartment), so a
    ``no-cross-contaminate`` source committed into a conflicting-compartment repo
    is denied. Raises :class:`CommitDenied` if guard policy denies the flow (no
    trailer emitted)."""
    if not isinstance(message, str):
        raise BadCommitInput("message must be a string")
    if not isinstance(commit_eid, str) or not commit_eid:
        raise BadCommitInput("commit_eid must be a non-empty string")
    if not sources:
        raise BadCommitInput("commit requires at least one source entity")
    if tree_digest is not None and not lr.is_hex_digest(tree_digest):
        raise BadCommitInput("tree_digest must be a sha256 hex string or None")
    seen: set[str] = set()
    inputs: list[ChokepointInput] = []
    for s in sources:
        if not isinstance(s.eid, str) or not s.eid:
            raise BadCommitInput("source eid must be a non-empty string")
        if s.eid in seen:
            raise BadCommitInput(f"duplicate source entity {s.eid!r}")
        seen.add(s.eid)
        # The security snapshot used for enforcement MUST be the broker-derived
        # one recorded in the store, never caller-supplied (doc/lineage.md
        # §Authority). A source the store has never recorded cannot be laundered
        # into a clean commit by inventing a clean endpoint for it.
        ent = store.get_entity(s.eid)
        if ent is None:
            raise BadCommitInput(
                f"source entity {s.eid!r} is not recorded in the lineage store"
            )
        inputs.append(
            ChokepointInput(
                eid=s.eid,
                endpoint=FlowEndpoint(
                    guards=ent.guards,
                    compartments=ent.compartments,
                    conflict_classes=ent.conflict_classes,
                ),
            )
        )

    ts = int(time.time()) if now is None else int(now)
    dest = FlowEndpoint(
        compartments=frozenset(dest_compartments),
        conflict_classes=frozenset(dest_conflict_classes),
    )

    # Record the chokepoint + seal the receipt in ONE transaction so a denied
    # attempt, the commit entity, its edges, and the sealed receipt row are
    # all-or-nothing. store.transaction() is re-entrant; record_chokepoint's
    # inner txn joins this one.
    with store.transaction():
        res = record_chokepoint(
            store,
            chokepoint="commit-create",
            inputs=inputs,
            destination=dest,
            processing=_COMMIT_PROCESSING,
            output_eid=commit_eid,
            output_kind="commit",
            output_digest=tree_digest,
            output_locator=branch,
            agent_gid=agent_gid,
            action_version=action_version,
            now=ts,
        )
        if res.denied:
            # The attempt is already recorded by record_chokepoint (activity +
            # used edges, no derivative). Roll nothing back beyond that by
            # raising — but the recorded denial IS the audit trail we want, so
            # we must NOT abort the transaction. Re-raise after the with-block.
            denied_reasons = res.reasons
        else:
            denied_reasons = None
            head = store.chain_head()
            envelope = lr.build_envelope(
                entity=commit_eid,
                kind="git-trailer",
                chain_head=head,
                created_at=ts,
                artifact_digest=tree_digest,
                locator=branch,
                issuer=LINEAGE_ISSUER,
            )
            store.record_receipt(
                entity=commit_eid,
                kind="git-trailer",
                locator=branch,
                digest=tree_digest,
                payload=envelope,
            )

    if denied_reasons is not None:
        raise CommitDenied(denied_reasons)

    new_message = lr.append_git_trailer(message, envelope)
    return CommitLineageResult(
        message=new_message,
        commit_eid=commit_eid,
        activity_aid=res.activity_aid,
        receipt=envelope,
        chain_head=store.chain_head(),
    )


def new_commit_eid() -> str:
    """A fresh commit entity id for a commit whose object hash is not yet known
    (it is assigned by git only after the message — including our trailer — is
    finalized and signed)."""
    return f"commit:{uuid.uuid4().hex}"
