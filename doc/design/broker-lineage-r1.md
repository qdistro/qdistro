# Verdict: GO on slice B

Do the broker-central cutover now, but do not call `record_chokepoint` for disposable export lineage in v1. The shippable slice is a broker-owned D-Bus API that records:

- one broker-derived source entity for the disposable export source;
- one export activity;
- one output entity per landed artifact, using the same `eid` already embedded in the receipt surfaces;
- `used`, `wasGeneratedBy`, and `wasDerivedFrom` edges;
- sealed sidecar and export-manifest receipt rows;
- explicit broker-derived assertions that the source/output security snapshot is `unresolved`.

This fixes the current structural lineage gap: `upstream`, `downstream`, and root-cause/impact queries work because they only need `wasDerivedFrom`. It deliberately does not claim guard/compartment inheritance until there is a production resolver for silo/open-class security snapshots.

I do not recommend full option A yet. The critical missing piece is real compartments/conflict classes. Calling `record_chokepoint` with empty `FlowEndpoint`s would make the output look policy-clean, and that is worse than today’s isolated rows.

## Q1. Store ownership and process boundary

The broker must become the only writer for `/var/lib/qdistro/lineage/export-lineage.sqlite`.

Session-manager should stop importing/opening `LineageStore` entirely. It should get receipt context and seal requests through broker D-Bus. It should not keep a SQLite read handle in production. If it later needs read queries, add D-Bus read methods to the broker rather than sharing the SQLite file across daemons. Unit tests may still instantiate `LineageStore` directly with temp paths.

Store lifecycle:

- Add broker constants:
  - `LINEAGE_DB_PATH = os.environ.get("QDISTRO_EXPORT_LINEAGE_DB", "/var/lib/qdistro/lineage/export-lineage.sqlite")`
  - `LINEAGE_ISSUER = "qdistro-broker"`
- Add `AdminBroker._get_lineage_store()` mirroring the current session-manager lazy open:
  - create `/var/lib/qdistro/lineage` if absent;
  - if created, chmod `0700`;
  - open one `LineageStore` instance for broker lifetime;
  - use WAL/busy timeout already handled by `LineageStore`.
- Remove session-manager’s `_get_lineage_store` and `_seal_export_receipts` write path after migration.

Migration story:

- Clean cutover is acceptable. This is not a protocol that needs dual-write or dual-read.
- Existing DB file can stay in place. The path and schema are unchanged; only the writer process changes.
- Installer changes should ensure the lineage directory and DB remain root-owned and only root-readable/writable. Both broker and session-manager run as root, so POSIX ownership alone does not distinguish them. The invariant is process discipline: only broker code opens the file.
- On upgrade, stop `qdistro-session-manager`, stop `qdistro-admin-broker`, install new code/policy, then start broker before session-manager. This avoids an in-flight old session-manager writer racing a new broker writer.
- If there are existing isolated `entities`/`receipts` rows from the old session-manager path, leave them intact. They remain valid sealed receipt authority. The new broker rows are additive. Do not rewrite historical rows into guessed edges.

No dual-read is needed for v1 because receipt verification already reads the central store by path in tests/tools, and production session-manager does not need lineage reads to import artifacts.

## Q2. Caller identity and trust model

Only root may call the new lineage D-Bus methods, and the intended production caller is session-manager.

Implement in-method checks using the existing `_peer_info(sender, conn)` pattern:

- `uid == 0` required for `GetLineageReceiptContext`.
- `uid == 0` required for `RecordExportLineage`.
- D-Bus policy should also deny non-root callers for these members, matching `RegisterLaunch` and `CheckPermissionForClient`.

Root-only is the right v1 gate. If there is a stable session-manager executable path on the installed system, add an optional executable allowlist check as defense in depth, but do not make correctness depend on the path unless the packaging path is stable across VM/test installs. The security boundary is root.

What broker re-validates:

- descriptor shape and scalar types;
- `token` grammar using disposable-token validation logic or the same regex contract;
- `mode in {"export", "edit"}`;
- open class resolves in `qdistro_disposable_classes` and has `export=True`; if `mode=="edit"`, also `edit=True`;
- broker permission gate `qdistro.dispose.export:<class>` is still `allow`;
- `request_silo`, `open_class`, `launch_token` are internally consistent in the descriptor;
- every recorded file path is under the landed destination root using `realpath`/`commonpath`;
- every landed file is a regular file, not a symlink;
- size matches the descriptor;
- digest matches by re-reading the landed file.

Re-digesting in the broker is worth it. The files are already durable and bounded by the export caps. Re-digesting removes unnecessary trust in the session-manager’s per-file digest and prevents a compromised or buggy session-manager from sealing a digest that does not match the durable artifact. It does not stop a compromised root session-manager from landing malicious content and asking the broker to record it, but it does stop mismatch between the artifact and the central receipt row.

What still has to be trusted:

- A compromised root session-manager can mint false lineage descriptors for files it can place/read. Broker-side revalidation limits this to real durable files and current policy gates, but root is inside the TCB. This is honest and consistent with existing root-only broker methods.

## Q3. Source security snapshot

Choose option C for v1: record structural provenance, but mark security snapshot unresolved.

The broker should record a source entity whose identity is broker-derived from the export descriptor, for example:

- `source_eid = "disposable-source:<token>"`
- kind: `resource`
- locator: `qdistro://disposable/<token>`
- facets:
  - `request_silo`
  - `open_class`
  - `mode`
  - `launch_token`
  - `source_input` if the descriptor has one
  - `security_snapshot_state: "unresolved"`

Security fields on that source entity should be empty only as a storage placeholder, not as a policy claim. Immediately add sealed assertions:

- subject: source eid
- fact: `security.snapshot.state`
- value: `"unresolved"`
- asserted_by: `"broker"`
- authority: `"broker-derived"`

For each output entity, also record:

- guards: empty
- compartments: empty
- conflict_classes: empty
- integrity: `None`
- facets include `security_snapshot_state: "unresolved"`
- assertion `security.snapshot.state = "unresolved"`

This is honest because an empty array no longer means "clean"; the broker-derived assertion says "not resolved". Any UI/query that presents guard state should treat `security.snapshot.state != "resolved"` as unknown, not clean. That follow-on presentation rule should be documented/tests added where relevant.

Do not add a manifest/YAML silo security loader in this slice. The docs explicitly say field names are still illustrative, and production has no authoritative compartments/conflict_classes record. A minimal loader would create a second authority problem: it would look real while reading a non-final taxonomy.

## Q4. `record_chokepoint` vs thinner recorder

Use a thinner `record_export_activity` helper for this slice.

`record_chokepoint` is the wrong primitive until the source `FlowEndpoint` is real. Its implementation always computes and stores a monotonic union snapshot on the output entity. With an unknown source, that produces empty guard/compartment/conflict-class fields that downstream callers can mistake for a resolved clean result.

The thinner helper should write the same PROV structure without evaluating guards or computing a union:

- activity kind `export`, chokepoint `export`, host `local`, network egress `none`;
- source entity;
- output artifact entity/entities;
- `used(activity, source)`;
- `wasGeneratedBy(output, activity)`;
- `wasDerivedFrom(output, source)`;
- optional attribution/association to broker/session-manager agents;
- receipts.

The `output_eid` join is sound. The session-manager already uses deterministic artifact eids in the receipt envelopes, and receipt verification checks a full-payload sealed row for the same entity/digest. The broker must record the output entity using exactly `env["entity"]`; it must reject any file descriptor whose `entity` differs from its sidecar envelope entity, and it must seal receipts against that same eid.

Future migration path:

- Once a production silo/open-class security resolver exists, replace the unresolved branch with `record_chokepoint` or a new `record_export_chokepoint` that passes a real `FlowEndpoint`.
- The old structural rows remain valid. New rows gain resolved snapshots and guard inheritance.

## Q5. Receipt-chain handoff ordering and atomicity

Use two D-Bus methods, but only one post-landing write transaction.

Recommended methods:

1. `GetLineageReceiptContext() -> a{sv}`
   - root-only;
   - opens broker-owned store;
   - returns `{"chain_head": <store.chain_head()>, "issuer": "qdistro-broker"}`.
   - no DB writes.

2. `RecordExportLineage(a{sv} descriptor) -> a{sv}`
   - root-only;
   - called after files have landed durably;
   - validates/re-digests/re-stats;
   - records source/activity/output/edges/receipts in one `LineageStore.transaction()`;
   - returns summary including `lineage_sealed`, `activity`, `outputs`, and `chain_head`.

This is not a two-phase commit. There is no pending row, lease, or begin transaction before landing. The pre-landing context is just a receipt surface pointer.

Crash semantics:

- If session-manager gets context, lands files, then dies before `RecordExportLineage`, the artifacts and surfaces remain durable but unsealed. Verification against the store fails closed. This preserves the current degraded-safe behavior.
- If session-manager lands files and `RecordExportLineage` fails, the artifacts remain imported and surfaces remain unverified. Session-manager logs and proceeds.
- If broker records rows after re-digesting, all rows for that export commit atomically. There must not be an entity without its edges or receipts.

One-call-post-landing alone is not enough if the session-manager is still emitting surfaces before landing: it needs the broker’s current chain head and issuer. A one-call design would require surfaces to be written after landing, which would disturb the current atomic landing property. Keep context + commit.

## Q6. Failure semantics

Session-manager side:

- Lineage is best-effort and must never fail a successful import/export landing.
- Failure to get receipt context means call `promote_export`/`promote_edit` with `receipt_ctx=None`; no lineage surfaces are emitted.
- Failure to call or complete `RecordExportLineage` after landing sets `receipt["lineage_sealed"] = False`, logs a warning, drops bulky envelope fields from the D-Bus return, and proceeds.
- A broker denial/error before landing is only fatal if it is part of the existing export policy gate. The lineage context call is not a policy gate.

Broker side:

- `RecordExportLineage` validates strictly and raises D-Bus errors on malformed descriptors or failed revalidation.
- Writes are all-or-nothing per export under one `store.transaction()`.
- On validation failure, write nothing. The already-landed artifact remains unverified, which is fail-closed from a lineage authority perspective.
- Do not partially seal only the files that validate. One bad descriptor/file mismatch rejects the whole lineage batch.

## Q7. Final slice decision

Proceed with B.

This slice is meaningful and shippable because it:

- moves the central lineage writer to the broker;
- eliminates cross-process SQLite writes;
- preserves receipt fail-closed semantics;
- makes forward/reverse structural lineage queries work;
- avoids false guard cleanliness;
- leaves a clean upgrade path to real `record_chokepoint` once source security snapshots exist.

Do not block on a silo security taxonomy resolver. Do not ship fake guard unions.

## Concrete D-Bus API

Add methods to the existing `org.qdistro.AdminBroker1` service/interface.

### `GetLineageReceiptContext`

Decorator:

```python
@dbus.service.method(
    BUS_NAME,
    in_signature="",
    out_signature="a{sv}",
    sender_keyword="sender",
    connection_keyword="conn",
)
def GetLineageReceiptContext(self, sender=None, conn=None) -> dict:
    ...
```

Return:

```python
{
    "version": dbus.UInt32(1),
    "chain_head": dbus.String(store.chain_head()),
    "issuer": dbus.String("qdistro-broker"),
}
```

Errors:

- `org.qdistro.AdminBroker1.AccessDenied` for non-root.
- `org.qdistro.AdminBroker1.LineageUnavailable` if the store cannot open.

Session-manager treats any error as "no receipt surfaces".

### `RecordExportLineage`

Decorator:

```python
@dbus.service.method(
    BUS_NAME,
    in_signature="a{sv}",
    out_signature="a{sv}",
    sender_keyword="sender",
    connection_keyword="conn",
)
def RecordExportLineage(self, descriptor: dict, sender=None, conn=None) -> dict:
    ...
```

Use `a{sv}` rather than a wide positional signature so v1 can evolve without a new method for every metadata addition. The method must immediately normalize the descriptor into plain Python strings/ints/lists/dicts and reject unknown required shapes.

Descriptor shape:

```python
{
    "version": 1,
    "launch_token": "disp-...",
    "request_silo": "work",
    "open_class": "text/plain",
    "mode": "export",          # "export" | "edit"
    "dest": "/home/work/Incoming/text%2Fplain-...",
    "source": {
        "kind": "disposable-export",
        "source_input": "...", # optional, descriptor/meta-provided identity
        "locator": "qdistro://disposable/<token>"
    },
    "files": [
        {
            "entity": "artifact:<token>:<name>",
            "name": "report.txt",
            "path": "/home/work/Incoming/.../report.txt",
            "locator": "Incoming/.../report.txt",
            "digest": "<sha256 hex>",
            "size": 1234,
            "receipt": { ... sidecar envelope ... }
        }
    ],
    "manifest": {
        ... export manifest envelope ...
    }                           # absent/empty for edit mode
}
```

Return:

```python
{
    "version": dbus.UInt32(1),
    "lineage_sealed": dbus.Boolean(True),
    "activity": dbus.String(aid),
    "source": dbus.String(source_eid),
    "outputs": dbus.Array([dbus.String(eid), ...], signature="s"),
    "chain_head": dbus.String(store.chain_head()),
}
```

Errors:

- `AccessDenied`: non-root caller.
- `BadArgument`: malformed descriptor, unsupported mode, bad digest grammar, mismatched receipt/entity/digest, path outside dest, missing required fields.
- `LineagePolicyDenied`: current open-class export/edit capability or broker export gate denies.
- `LineageValidationFailed`: landed file cannot be stat/read, is not regular, size mismatch, digest mismatch.
- `LineageUnavailable`: store open/write failure.

Session-manager catches all of these after landing and degrades.

## Broker method body outline

Files touched:

- `broker/qdistro_admin_broker.py`
- `broker/org.qdistro.AdminBroker1.conf`
- new `broker/qdistro_export_lineage.py` or helper functions in `broker/qdistro_lineage.py`
- `scripts/install/install-broker-for-qdwin.sh` or relevant broker install script if module lists are explicit
- `scripts/install/install-session-manager.sh` to stop installing lineage store/write helper for session-manager if no longer needed there

Pure helper:

```python
def record_export_activity(
    store: LineageStore,
    *,
    token: str,
    request_silo: str,
    open_class: str,
    mode: str,
    dest: str,
    source_input: str | None,
    files: list[ExportLineageFile],
    manifest: dict | None,
    now: int | None = None,
) -> ExportLineageResult:
    ts = now or int(time.time())
    aid = f"activity:{uuid.uuid4().hex}"
    source_eid = f"disposable-source:{token}"

    with store.transaction():
        store.record_entity(Entity(
            eid=source_eid,
            kind="resource",
            locator=f"qdistro://disposable/{token}",
            facets={
                "request_silo": request_silo,
                "open_class": open_class,
                "mode": mode,
                "source_input": source_input,
                "security_snapshot_state": "unresolved",
            },
            created_at=ts,
        ))
        store.record_assertion(
            subject=source_eid,
            fact="security.snapshot.state",
            value="unresolved",
            asserted_by="broker",
            authority="broker-derived",
        )
        store.record_activity(Activity(
            aid=aid,
            kind="export",
            chokepoint="export",
            action_version="disposable-export/v1",
            host_class="local",
            network_egress="none",
            verdict="recorded-unresolved",
            started_at=ts,
            ended_at=ts,
        ))
        store.record_edge("used", aid, source_eid, activity=aid)

        for f in files:
            store.record_entity(Entity(
                eid=f.entity,
                kind="artifact",
                digest=f.digest,
                locator=f.locator,
                facets={
                    "request_silo": request_silo,
                    "open_class": open_class,
                    "mode": mode,
                    "security_snapshot_state": "unresolved",
                },
                created_at=ts,
            ))
            store.record_assertion(
                subject=f.entity,
                fact="security.snapshot.state",
                value="unresolved",
                asserted_by="broker",
                authority="broker-derived",
            )
            store.record_edge("wasGeneratedBy", f.entity, aid, activity=aid)
            store.record_edge("wasDerivedFrom", f.entity, source_eid, activity=aid)
            store.record_receipt(
                entity=f.entity,
                kind="sidecar",
                locator=f.receipt.get("locator"),
                digest=f.digest,
                payload=f.receipt,
            )
            if manifest:
                child = find_manifest_child_for_entity(manifest, f.entity)
                store.record_receipt(
                    entity=f.entity,
                    kind="export-manifest",
                    locator=child.get("locator"),
                    digest=f.digest,
                    payload=child,
                )
    return ExportLineageResult(aid=aid, source_eid=source_eid, outputs=[...])
```

Broker D-Bus method pseudocode:

```python
def RecordExportLineage(self, descriptor, sender=None, conn=None):
    uid, pid, exe, _ = self._peer_info(sender, conn)
    if uid != 0:
        raise AccessDenied

    d = normalize_descriptor(descriptor)
    validate_token(d.launch_token)
    validate_mode(d.mode)

    cls = qdistro_disposable_classes.resolve_from_registry(d.open_class)
    if not cls.export or (d.mode == "edit" and not cls.edit):
        raise LineagePolicyDenied

    gate = qdistro_disposable_classes.export_action(d.open_class)
    if self._check_permission_internal(gate) != "allow":
        raise LineagePolicyDenied

    validate_dest_root(d.dest)
    for f in d.files:
        validate_receipt_envelope(f.receipt, expected_kind="sidecar")
        require f.receipt["entity"] == f.entity
        require f.receipt["artifact_digest"] == f.digest
        require commonpath(realpath(f.path), realpath(d.dest)) == realpath(d.dest)
        st = os.stat(f.path, follow_symlinks=False)
        require regular file
        require st.st_size == f.size
        digest = sha256_file_open_nofollow(f.path)
        require digest == f.digest

    if d.manifest:
        validate_export_manifest(d.manifest)
        require every child entity/digest matches one file

    store = self._get_lineage_store()
    result = record_export_activity(store, ...)
    return result.as_dbus()
```

Use a direct rules/cache helper for the export gate rather than making the broker call its own D-Bus method. The session-manager already checks the gate before landing; the broker re-check is defense in depth and catches policy changes between landing and sealing.

## Session-manager diff sketch

Files touched:

- `session_manager/qdistro_session_manager.py`
- `session_manager/qdistro_disposable_export.py` only if descriptor assembly needs an extra returned path/locator field
- `scripts/install/install-session-manager.sh`
- unit tests around export receipt sealing/degraded behavior

Changes:

- Remove `LINEAGE_ISSUER = "qdistro-session-manager"` or stop using it.
- Remove/lobotomize `_get_lineage_store`.
- Replace pre-landing context acquisition:

```python
receipt_ctx = None
try:
    receipt_ctx = self._ops.broker_lineage_receipt_context()
except Exception as e:
    log.warning("import_from_disposable: broker lineage context unavailable, no receipts emitted: %s", e)
```

- Keep passing `receipt_ctx` to `_dispexport.promote_export` / `promote_edit`.
- After landing, replace `_seal_export_receipts(lineage, receipt)` with:

```python
try:
    desc = _build_export_lineage_descriptor(
        token=token,
        meta=meta,
        mode="edit" if edit_mode else "export",
        state_path=state_path,
        receipt=receipt,
    )
    result = self._ops.broker_record_export_lineage(desc)
    receipt["lineage_sealed"] = bool(result.get("lineage_sealed"))
except Exception as e:
    receipt["lineage_sealed"] = False
    log.warning(
        "import_from_disposable: broker lineage seal failed "
        "(artifacts landed; lineage degraded/unverified): %s", e)
finally:
    receipt["lineage_receipts"] = len(receipt.get("lineage_sidecars") or [])
    receipt.pop("lineage_sidecars", None)
    receipt.pop("lineage_manifest", None)
```

- If `receipt_ctx is None`, skip descriptor/seal call because no surfaces were emitted.
- Preserve the current "artifacts already durable; never fail import" behavior.

Ops/client helper changes:

- Add broker D-Bus client wrappers:
  - `broker_lineage_receipt_context()`
  - `broker_record_export_lineage(descriptor)`

## D-Bus policy

In `broker/org.qdistro.AdminBroker1.conf`, add root-only permissions for:

- `org.qdistro.AdminBroker1.GetLineageReceiptContext`
- `org.qdistro.AdminBroker1.RecordExportLineage`

This should mirror the policy stance for `RegisterLaunch`/root portal methods: D-Bus policy blocks non-root, and method body checks `uid == 0`.

## Test plan

Pure unit tests:

- New `tests/unit/test_export_lineage_recording.py`
  - records one export with one source and two files;
  - asserts `upstream(output)` contains the source;
  - asserts `downstream(source)` contains both outputs;
  - asserts `guarded_descendants(source)` returns outputs with empty guard sets plus `security.snapshot.state == "unresolved"` assertions;
  - asserts receipts are stored for sidecar and manifest;
  - asserts store chain verifies.
- Test the helper rejects empty file list only if policy wants that. I would allow an empty file list to record nothing only at session-manager receipt level, and skip `RecordExportLineage` for zero-file imports.
- Test all-or-nothing: one bad file descriptor/receipt mismatch raises and leaves no activity/output/receipt rows.
- Test output eid join: sidecar `entity` mismatch rejects.
- Test digest mismatch rejects.
- Test path outside dest rejects.
- Test manifest child mismatch rejects.
- Test unknown security is asserted and no guard union is claimed.

Broker unit tests without system bus:

- Instantiate `AdminBroker` or extracted validator helper with fake ops/store.
- Test root-only branch via direct method wrapper where possible, or test `_require_root_lineage_peer(uid)`.
- Test open-class `export=false` and edit `edit=false` reject.
- Test export gate non-allow rejects before writing.

Session-manager unit tests:

- Context failure means `promote_export` runs with `receipt_ctx=None`, import succeeds, no lineage surfaces.
- Post-landing broker seal failure sets `lineage_sealed=False`, strips bulky envelopes, import succeeds.
- Successful broker seal sets `lineage_sealed=True`, strips bulky envelopes, import succeeds.
- Zero-file no-staging path remains unchanged.

Existing receipt tests:

- Keep `test_lineage_export_receipts.py` coverage for surface emit/parse/verify.
- Update any issuer expectation from `qdistro-session-manager` to `qdistro-broker`.

Integration VM:

- Extend `tests/integration/vm/disp-export-e2e.bats` / `probes/disp-export-probe.sh`:
  - after export, verify sidecar and manifest exist;
  - query the central store and verify sealed receipt rows exist;
  - verify `wasDerivedFrom` edges from landed artifact eids to the source eid;
  - verify `upstream(artifact)` is non-empty and contains `disposable-source:<token>`;
  - verify `downstream(source)` contains the artifact eids;
  - verify `security.snapshot.state` assertion is `unresolved`;
  - verify receipt surface still fails closed if the broker is stopped before post-landing seal.

Hardening regression checks:

- O_NOFOLLOW rooted-tree promotion remains in `qdistro_disposable_export.py`; broker recording only re-opens landed files for validation and never changes the landing tree.
- Export policy gates remain fail-closed before promotion.
- Lineage rows remain additive.
- Receipt surfaces are never authority without matching sealed rows.
- Reserved-name collision checks remain in the promoter.
- New D-Bus API is root-only and performs strict validation/re-digesting.

## Follow-on required before guard inheritance

Before switching this path to `record_chokepoint`, implement and test a production source-security resolver with explicit authority:

- inputs: `request_silo`, `open_class`, maybe launch/disposable class;
- outputs: `FlowEndpoint(guards, compartments, conflict_classes)` plus `state="resolved"`;
- source of truth: a production policy/resource/session record, not illustrative docs;
- failure mode: unresolved, not empty-clean.

Only after that resolver exists should broker export lineage call `record_chokepoint` or compute guard unions for disposable outputs.
