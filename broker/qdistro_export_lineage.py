"""Broker-owned recording for disposable export lineage.

This module deliberately does not call ``record_chokepoint``. It records
structural provenance and explicit ``security.snapshot.state = "unresolved"``
assertions instead of minting a clean-looking guard union.

The production silo -> security-snapshot resolver now exists
(``qdistro_silo_security``: a verified live pid -> launcher-attested silo ->
central snapshot store behind the ``SnapshotAuthority`` seam). It still does NOT
unblock THIS caller, by design (codex design review, 2026-06-18): the resolver's
only safe input is a verified live ``(pid, starttime)``, but a disposable
export-back has no live source process — the descriptor carries a disposable
``launch_token`` plus a caller-supplied ``request_silo`` STRING (exactly the
untrusted value the resolver refuses to consume), and the D-Bus caller pid is the
root helper, not the disposable source (which may have exited). Wiring this to
``record_chokepoint`` from those would be cross-silo source forgery. Doing so
needs a separate, trusted ``launch_token -> silo`` authority (issuer-authenticated,
bound at disposable creation, expiry/replay handling) — a future design item, NOT
this v1 slice. Until then the unresolved branch stays (empty guards mean unknown,
never clean).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from typing import Any

import qdistro_lineage_receipts as lr
from qdistro_lineage_store import Activity, Agent, Entity, LineageStore

LINEAGE_ISSUER = "qdistro-broker"


class ExportLineageError(ValueError):
    """Base class for malformed or unverifiable export-lineage descriptors."""


class BadDescriptor(ExportLineageError):
    """The descriptor shape or receipt metadata is invalid."""


class ValidationFailed(ExportLineageError):
    """A landed artifact failed filesystem validation or re-digesting."""


@dataclass(frozen=True)
class ExportLineageFile:
    entity: str
    name: str
    path: str
    locator: str
    digest: str
    size: int
    receipt: dict[str, Any]


@dataclass(frozen=True)
class ExportLineageDescriptor:
    launch_token: str
    request_silo: str
    open_class: str
    mode: str
    dest: str
    source_input: str | None
    files: tuple[ExportLineageFile, ...]
    manifest: dict[str, Any] | None


@dataclass(frozen=True)
class ExportLineageResult:
    activity: str
    source: str
    outputs: tuple[str, ...]
    chain_head: str


def load_descriptor_json(payload: str) -> ExportLineageDescriptor:
    if not isinstance(payload, str) or not payload:
        raise BadDescriptor("descriptor must be a non-empty JSON string")
    try:
        raw = json.loads(payload)
    except (TypeError, ValueError) as e:
        raise BadDescriptor(f"descriptor is not valid JSON: {e}") from e
    return normalize_descriptor(raw)


def normalize_descriptor(raw: Any) -> ExportLineageDescriptor:
    if not isinstance(raw, dict):
        raise BadDescriptor("descriptor must be a JSON object")
    version = _int_field(raw, "version")
    if version != 1:
        raise BadDescriptor(f"unsupported descriptor version {version!r}")
    token = _str_field(raw, "launch_token")
    request_silo = _str_field(raw, "request_silo")
    open_class = _str_field(raw, "open_class")
    mode = _str_field(raw, "mode")
    if mode not in {"export", "edit"}:
        raise BadDescriptor("mode must be 'export' or 'edit'")
    dest = _str_field(raw, "dest")
    source = raw.get("source") or {}
    if source is not None and not isinstance(source, dict):
        raise BadDescriptor("source must be an object when present")
    source_input = source.get("source_input") or raw.get("source_input")
    if source_input is not None and not isinstance(source_input, str):
        raise BadDescriptor("source_input must be a string or null")
    files_raw = raw.get("files")
    if not isinstance(files_raw, list):
        raise BadDescriptor("files must be a list")
    files = tuple(_normalize_file(f) for f in files_raw)
    if not files:
        raise BadDescriptor("files must not be empty")
    manifest = raw.get("manifest")
    if manifest in ({}, None):
        manifest = None
    elif not isinstance(manifest, dict):
        raise BadDescriptor("manifest must be an object or null")
    return ExportLineageDescriptor(
        launch_token=token,
        request_silo=request_silo,
        open_class=open_class,
        mode=mode,
        dest=dest,
        source_input=source_input,
        files=files,
        manifest=manifest,
    )


def validate_landed_files(desc: ExportLineageDescriptor) -> None:
    if not os.path.isabs(desc.dest):
        raise BadDescriptor("dest must be an absolute path")
    dest_abs = os.path.abspath(desc.dest)
    dest_fd = _open_dir_nofollow_absolute(dest_abs)
    try:
        seen: set[str] = set()
        for f in desc.files:
            if f.entity in seen:
                raise BadDescriptor(f"duplicate file entity {f.entity!r}")
            seen.add(f.entity)
            env = lr.validate_envelope(f.receipt, expected_kind="sidecar")
            if env["entity"] != f.entity:
                raise BadDescriptor("sidecar entity does not match file entity")
            if env["artifact_digest"] != f.digest:
                raise BadDescriptor("sidecar digest does not match file digest")
            if env["issuer"] != LINEAGE_ISSUER:
                raise BadDescriptor("sidecar issuer is not qdistro-broker")
            rel = _relative_to_dest(f.path, dest_abs)
            _validate_one_file(f, dest_fd=dest_fd, relpath=rel)
        if desc.manifest is not None:
            _validate_manifest(desc.manifest, desc.files)
    finally:
        os.close(dest_fd)


def record_export_activity(
    store: LineageStore,
    desc: ExportLineageDescriptor,
    *,
    now: int | None = None,
) -> ExportLineageResult:
    ts = int(time.time()) if now is None else int(now)
    aid = f"activity:{uuid.uuid4().hex}"
    source_eid = f"disposable-source:{desc.launch_token}"
    outputs = tuple(f.entity for f in desc.files)
    facets = {
        "request_silo": desc.request_silo,
        "open_class": desc.open_class,
        "mode": desc.mode,
        "launch_token": desc.launch_token,
        "security_snapshot_state": "unresolved",
    }
    if desc.source_input:
        facets["source_input"] = desc.source_input
    with store.transaction():
        store.record_entity(
            Entity(
                eid=source_eid,
                kind="resource",
                locator=f"qdistro://disposable/{desc.launch_token}",
                facets=facets,
                created_at=ts,
            )
        )
        store.record_assertion(
            subject=source_eid,
            fact="security.snapshot.state",
            value="unresolved",
            asserted_by="broker",
            authority="broker-derived",
        )
        store.record_activity(
            Activity(
                aid=aid,
                kind="export",
                chokepoint="export",
                action_version="disposable-export/v1",
                host_class="local",
                network_egress="none",
                verdict="recorded-unresolved",
                started_at=ts,
                ended_at=ts,
            )
        )
        store.record_agent(Agent(gid="broker", kind="broker", name="qdistro-broker"))
        store.record_edge("used", aid, source_eid, activity=aid)
        store.record_edge("wasAssociatedWith", aid, "broker", activity=aid)
        for f in desc.files:
            ofacets = {
                "request_silo": desc.request_silo,
                "open_class": desc.open_class,
                "mode": desc.mode,
                "security_snapshot_state": "unresolved",
            }
            store.record_entity(
                Entity(
                    eid=f.entity,
                    kind="artifact",
                    digest=f.digest,
                    locator=f.locator,
                    facets=ofacets,
                    created_at=ts,
                )
            )
            store.record_assertion(
                subject=f.entity,
                fact="security.snapshot.state",
                value="unresolved",
                asserted_by="broker",
                authority="broker-derived",
            )
            store.record_edge("wasGeneratedBy", f.entity, aid, activity=aid)
            store.record_edge("wasDerivedFrom", f.entity, source_eid, activity=aid)
            store.record_edge("wasAttributedTo", f.entity, "broker", activity=aid)
            store.record_receipt(
                entity=f.entity,
                kind="sidecar",
                locator=f.receipt.get("locator"),
                digest=f.digest,
                payload=f.receipt,
            )
            if desc.manifest is not None:
                child = _manifest_child(desc.manifest, f.entity)
                store.record_receipt(
                    entity=f.entity,
                    kind="export-manifest",
                    locator=child.get("locator"),
                    digest=f.digest,
                    payload=child,
                )
    return ExportLineageResult(
        activity=aid,
        source=source_eid,
        outputs=outputs,
        chain_head=store.chain_head(),
    )


def _normalize_file(raw: Any) -> ExportLineageFile:
    if not isinstance(raw, dict):
        raise BadDescriptor("file entry must be an object")
    size = _int_field(raw, "size")
    if size < 0:
        raise BadDescriptor("file size must be non-negative")
    digest = _str_field(raw, "digest")
    if not _is_sha256_hex(digest):
        raise BadDescriptor("file digest must be a lowercase sha256 hex string")
    receipt = raw.get("receipt")
    if not isinstance(receipt, dict):
        raise BadDescriptor("file receipt must be an object")
    return ExportLineageFile(
        entity=_str_field(raw, "entity"),
        name=_str_field(raw, "name"),
        path=_str_field(raw, "path"),
        locator=_str_field(raw, "locator"),
        digest=digest,
        size=size,
        receipt=receipt,
    )


def _validate_one_file(f: ExportLineageFile, *, dest_fd: int, relpath: str) -> None:
    if not os.path.isabs(f.path):
        raise BadDescriptor("file path must be absolute")
    fd = _open_file_beneath(dest_fd, relpath, display_path=f.path)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValidationFailed(f"landed path is not a regular file: {f.path!r}")
        if int(st.st_size) != f.size:
            raise ValidationFailed(
                f"landed file size mismatch for {f.path!r}: {st.st_size} != {f.size}"
            )
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
        if h.hexdigest() != f.digest:
            raise ValidationFailed(f"landed file digest mismatch for {f.path!r}")
    finally:
        os.close(fd)


def _relative_to_dest(path: str, dest_abs: str) -> str:
    if not os.path.isabs(path):
        raise BadDescriptor("file path must be absolute")
    path_abs = os.path.abspath(path)
    try:
        common = os.path.commonpath([dest_abs, path_abs])
    except ValueError as e:
        raise BadDescriptor("file path and dest are not comparable") from e
    if common != dest_abs:
        raise BadDescriptor(f"file path outside dest: {path!r}")
    rel = os.path.relpath(path_abs, dest_abs)
    if rel in ("", ".") or rel.startswith(".."):
        raise BadDescriptor(f"file path outside dest: {path!r}")
    return rel


def _open_dir_nofollow_absolute(path: str) -> int:
    parts = [p for p in os.path.abspath(path).split(os.sep) if p]
    cur = os.open(os.sep, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        for part in parts:
            nxt = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=cur,
            )
            os.close(cur)
            cur = nxt
        return cur
    except OSError as e:
        os.close(cur)
        raise ValidationFailed(f"cannot open dest directory {path!r}: {e}") from e


def _open_file_beneath(dest_fd: int, relpath: str, *, display_path: str) -> int:
    parts = [p for p in relpath.split(os.sep) if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        raise BadDescriptor(f"file path outside dest: {display_path!r}")
    cur = os.dup(dest_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(
                part,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=cur,
            )
            os.close(cur)
            cur = nxt
        try:
            return os.open(
                parts[-1],
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=cur,
            )
        except OSError as e:
            raise ValidationFailed(f"cannot open landed file {display_path!r}: {e}") from e
    except OSError as e:
        raise ValidationFailed(f"cannot walk landed file path {display_path!r}: {e}") from e
    finally:
        os.close(cur)


def _validate_manifest(manifest: dict[str, Any], files: tuple[ExportLineageFile, ...]) -> None:
    if manifest.get("schema") != lr.EXPORT_MANIFEST_SCHEMA:
        raise BadDescriptor("unexpected export manifest schema")
    if manifest.get("chain_algo") != lr.CHAIN_ALGO:
        raise BadDescriptor("unexpected export manifest chain_algo")
    receipts = manifest.get("receipts")
    if not isinstance(receipts, list):
        raise BadDescriptor("manifest receipts must be a list")
    expected = {f.entity: f.digest for f in files}
    seen: set[str] = set()
    for child in receipts:
        env = lr.validate_envelope(child, expected_kind="export-manifest")
        if env["chain_head"] != manifest.get("chain_head"):
            raise BadDescriptor("manifest child chain_head differs from manifest")
        if env["issuer"] != LINEAGE_ISSUER:
            raise BadDescriptor("manifest child issuer is not qdistro-broker")
        ent = env["entity"]
        if ent not in expected:
            raise BadDescriptor(f"manifest child has unknown entity {ent!r}")
        if ent in seen:
            raise BadDescriptor(f"duplicate manifest child {ent!r}")
        if env["artifact_digest"] != expected[ent]:
            raise BadDescriptor("manifest child digest does not match file digest")
        seen.add(ent)
    if seen != set(expected):
        raise BadDescriptor("manifest children do not match descriptor files")


def _manifest_child(manifest: dict[str, Any], entity: str) -> dict[str, Any]:
    for child in manifest.get("receipts", []):
        if child.get("entity") == entity:
            return child
    raise BadDescriptor(f"manifest has no child for entity {entity!r}")


def _str_field(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise BadDescriptor(f"{key} must be a non-empty string")
    return value


def _int_field(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BadDescriptor(f"{key} must be an int")
    return value


def _is_sha256_hex(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )
