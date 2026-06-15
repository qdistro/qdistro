"""Broker-owned browser-upload chokepoint (doc/lineage.md §Chokepoints
"browser upload/download", §Storage "upload receipt").

This is the production SURFACE that ``qdistro_lineage_receipts`` documents as
shipping ahead of wiring: the upload-receipt helpers are pure batch transforms a
"broker-owned upload chokepoint" calls, and this module is that chokepoint. Like
``qdistro_commit_lineage`` and ``qdistro_export_lineage`` it (1) records the
chokepoint into the central store, (2) seals a per-file receipt row, and (3)
emits the portable batch surface — here a ``qdistro-upload-receipt.json``
manifest covering every uploaded file.

An upload sends bytes to a REMOTE destination, so the chokepoint's processing
descriptor reports ``remote-service`` egress: that is exactly the case the guard
registry denies for ``local-only`` content (doc/guards.md §Local-only Boundary).
A denied upload still records the attempt for audit but seals no receipts and
emits no manifest — the caller must block the upload. Fail-closed: guarded bytes
never silently acquire a clean upload receipt.

Each uploaded file is recorded as its own ``upload`` chokepoint flow + sealed
``upload-receipt`` child envelope (only children are sealed; the container
manifest is the non-authoritative on-disk surface, doc/lineage.md §Storage). The
audit-significant destination lives in each child envelope's ``extra`` so it is
covered by the sealed row, not only the container's convenience summary.

Batch ``chain_head``: every child envelope is built with ONE head captured
BEFORE any receipt is sealed (mirroring ``qdistro_export_lineage``), so the
container and all children share a single head AND each sealed row's payload is
the exact envelope :func:`~qdistro_lineage_receipts.verify_against_store` will
re-canonicalize and match. The envelope's ``chain_head`` is the descriptive
head-at-emission; the strong binding is the full-payload sealed-row match, not
the head value.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import qdistro_lineage_receipts as lr
from qdistro_guard_registry import FlowEndpoint, ProcessingDescriptor
from qdistro_lineage import ChokepointInput, record_chokepoint
from qdistro_lineage_store import Entity, LineageStore

LINEAGE_ISSUER = "qdistro-broker"

#: An upload sends bytes off-host: remote-service host, brokered egress, the
#: payload IS submitted. This is the descriptor the guard registry uses to deny
#: local-only content at the upload boundary.
_UPLOAD_PROCESSING = ProcessingDescriptor(
    host_class="remote-service",
    network_egress="brokered",
    payload_submitted=True,
    telemetry_submitted=False,
)


class UploadLineageError(ValueError):
    """Base class for malformed upload-lineage inputs."""


class BadUploadInput(UploadLineageError):
    """The upload descriptor shape is invalid."""


class UploadDenied(UploadLineageError):
    """The guard verdict denied one or more uploaded files; no manifest is
    emitted. The caller must abort the upload rather than send guarded bytes."""

    def __init__(self, denied: tuple[str, ...], reasons: tuple[str, ...]):
        super().__init__(
            "upload denied by guard policy for "
            + ", ".join(denied)
            + ": "
            + "; ".join(reasons)
        )
        self.denied_sources = denied
        self.reasons = reasons


@dataclass(frozen=True)
class UploadFile:
    """One file in the upload batch: ``source_eid`` is a tracked entity ALREADY
    recorded in the store; ``digest`` is the sha256 of the bytes that will be SENT
    (describes the payload, so it legitimately comes from the broker upload path);
    ``locator`` is the per-file remote locator. The security snapshot the
    chokepoint enforces is read from the recorded source entity, NOT supplied here
    (doc/lineage.md §Authority) — a source the store has never recorded cannot be
    laundered into a clean upload by inventing a clean endpoint."""

    source_eid: str
    digest: str
    locator: str | None = None


@dataclass(frozen=True)
class UploadLineageResult:
    """Outcome of an ALLOWED upload batch (a denied batch raises
    :class:`UploadDenied` and never returns this). ``manifest`` is the batch
    surface, ``output_eids`` the per-file uploaded-artifact entities (one per
    allowed file — and on the success path every file is allowed), and
    ``activity_aids`` the per-file ``browser-upload`` activity ids in input
    order. ``chain_head`` is the head after the batch committed."""

    manifest: dict
    output_eids: tuple[str, ...]
    activity_aids: tuple[str, ...]
    chain_head: str


def record_upload(
    store: LineageStore,
    *,
    files: Sequence[UploadFile],
    destination: str,
    agent_gid: str | None = None,
    action_version: str = "browser-upload/v1",
    now: int | None = None,
) -> UploadLineageResult:
    """Record the upload chokepoint for every file in ``files``, seal a per-file
    ``upload-receipt`` child envelope, and return the batch manifest container.
    ``destination`` is the remote target (origin/URL) the bytes are sent to; it
    is recorded as a broker-derived ``extra.destination`` on every sealed child
    AND as the container's convenience summary. Raises :class:`UploadDenied` if
    guard policy denies ANY file (the whole upload fails closed — a batch is sent
    atomically, so one guarded member taints the batch)."""
    if not isinstance(destination, str) or not destination:
        raise BadUploadInput("destination must be a non-empty string")
    if not files:
        raise BadUploadInput("upload requires at least one file")
    seen: set[str] = set()
    plan: list[tuple[UploadFile, str, FlowEndpoint]] = []
    for f in files:
        if not isinstance(f.source_eid, str) or not f.source_eid:
            raise BadUploadInput("file source_eid must be a non-empty string")
        if not lr.is_hex_digest(f.digest):
            raise BadUploadInput("file digest must be a sha256 hex string")
        if f.source_eid in seen:
            raise BadUploadInput(f"duplicate source entity {f.source_eid!r}")
        seen.add(f.source_eid)
        # The security snapshot the guard registry evaluates MUST be the
        # broker-derived one in the store, never caller-supplied (doc/lineage.md
        # §Authority). Resolving it before any output id is generated also lets a
        # missing source fail before the transaction opens. The upload digest is
        # the EXCEPTION: it describes the bytes being sent, so it legitimately
        # comes from the broker upload path, not the recorded source entity.
        ent = store.get_entity(f.source_eid)
        if ent is None:
            raise BadUploadInput(
                f"source entity {f.source_eid!r} is not recorded in the lineage store"
            )
        endpoint = FlowEndpoint(
            guards=ent.guards,
            compartments=ent.compartments,
            conflict_classes=ent.conflict_classes,
        )
        plan.append((f, f"upload:{uuid.uuid4().hex}", endpoint))

    ts = int(time.time()) if now is None else int(now)

    children: list[dict[str, Any]] = []
    output_eids: list[str] = []
    activity_aids: list[str] = []
    denied_sources: list[str] = []
    denied_reasons: list[str] = []

    # ONE batch transaction. The whole batch is sent atomically, so one guarded
    # member taints the batch and the upload is refused. Two phases, both inside
    # the txn that COMMITS:
    #   phase 1 records every per-file chokepoint (the activity + used edge — the
    #     denial audit doc/lineage.md wants persisted even when refused), and
    #   phase 2 seals the per-file receipts + builds the manifest ONLY when no
    #     file was denied.
    # On a denial we leave phase 1's audit recorded but seal NO receipts (a
    # sealed upload receipt would falsely imply the bytes left the host) and
    # raise AFTER the block so the audit commits. The child ``chain_head`` is the
    # head captured before any receipt is sealed, so all children/container share
    # one head and each sealed payload is the exact envelope
    # verify_against_store will match.
    with store.transaction():
        batch_head = store.chain_head()
        allowed: list[tuple[UploadFile, str, str]] = []
        for f, out_eid, endpoint in plan:
            inp = ChokepointInput(eid=f.source_eid, endpoint=endpoint)
            res = record_chokepoint(
                store,
                chokepoint="browser-upload",
                inputs=[inp],
                processing=_UPLOAD_PROCESSING,
                output_eid=out_eid,
                output_kind="artifact",
                output_digest=f.digest,
                output_locator=f.locator,
                agent_gid=agent_gid,
                action_version=action_version,
                now=ts,
            )
            activity_aids.append(res.activity_aid)
            if res.denied:
                denied_sources.append(f.source_eid)
                denied_reasons.extend(res.reasons)
            else:
                allowed.append((f, out_eid, res.activity_aid))

        if not denied_sources:
            for f, out_eid, aid in allowed:
                # browser-upload is NOT a deriving chokepoint, so
                # record_chokepoint records the activity + used edge but mints no
                # output entity. The uploaded artifact is its own recorded entity
                # (the bytes that left the host), derived from the source, so the
                # sealed receipt has an entity to bind to and verify_against_store
                # can authenticate it.
                store.record_entity(
                    Entity(eid=out_eid, kind="artifact", digest=f.digest,
                           locator=f.locator, created_at=ts)
                )
                store.record_edge("wasGeneratedBy", out_eid, aid, activity=aid)
                store.record_edge("wasDerivedFrom", out_eid, f.source_eid,
                                  activity=aid)
                if agent_gid is not None:
                    store.record_edge("wasAttributedTo", out_eid, agent_gid,
                                      activity=aid)
                output_eids.append(out_eid)
                child = lr.build_envelope(
                    entity=out_eid,
                    kind="upload-receipt",
                    chain_head=batch_head,
                    created_at=ts,
                    artifact_digest=f.digest,
                    locator=f.locator,
                    issuer=LINEAGE_ISSUER,
                    extra={"destination": destination},
                )
                store.record_receipt(
                    entity=out_eid,
                    kind="upload-receipt",
                    locator=f.locator,
                    digest=f.digest,
                    payload=child,
                )
                children.append(child)
            manifest = lr.build_upload_receipt(
                children, chain_head=batch_head, created_at=ts,
                destination=destination, issuer=LINEAGE_ISSUER,
            )

    if denied_sources:
        raise UploadDenied(tuple(denied_sources), tuple(denied_reasons))

    return UploadLineageResult(
        manifest=manifest,
        output_eids=tuple(output_eids),
        activity_aids=tuple(activity_aids),
        chain_head=store.chain_head(),
    )
