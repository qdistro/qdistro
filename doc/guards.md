# Guards And Contamination

Guards are typed security fields that constrain where data may flow and how
derived data is classified. They are not free-form user tags.

qdistro uses guards at brokered chokepoints: clipboard, import/export,
browser upload/download, API calls, Recall query/export, archive
create/extract, commit creation, backup/export, and workflow steps.

The goal is pragmatic information-flow control. qdistro does not claim
perfect noninterference inside every application. It does require conservative
lineage and policy decisions at qdistro-controlled boundaries.

## Metadata Shape

Use `security.guards` as the authoritative field. A matching label may exist
only for fast selectors and UI grouping:

```yaml
metadata:
  labels:
    qdistro.io/guard.local-only: "true"
security:
  guards: [local-only]
```

Labels are advisory selectors. The broker must consult typed security fields
and authoritative lineage before allowing a guarded flow.

## Reserved Guards

`local-only` means the data may be processed only on qdistro-controlled local
compute. Any derived artifact inherits `local-only` until an approved
declassification workflow creates a new tracked derivative.

`no-cross-contaminate` means the resource belongs to a compartment family that
must not silently mix with conflicting compartments. The common example is
`work` versus `home`: work data should not touch home resources, and home data
should not touch work resources, unless a brokered transfer, import, sanitize,
or declassification workflow records that decision.

Additional guards may be added later, but they must be reserved in
[metadata.md](metadata.md) and interpreted by policy as typed fields, not
string tags.

## Propagation

Default propagation is monotonic:

```text
output.security.guards = union(inputs.security.guards)
output.security.compartments = union(inputs.security.compartments)
output.security.conflictClasses = union(inputs.security.conflictClasses)
```

Every output of an activity inherits all inputs unless a trusted action
handler records a narrower input-to-output mapping. If the mapping is absent,
untrusted, or incomplete, qdistro uses the conservative union.

Derived data includes:

- summaries, rewritten text, translations, extracted entities, and labels;
- embeddings and vector indexes;
- OCR text from screenshots;
- model outputs produced from guarded prompts or context;
- diffs, patches, build logs, test logs, and crash reports;
- archives, exported datasets, notebooks, generated reports;
- Recall rows, FTS rows, embeddings, query results, and exports;
- commits, commit messages, trailers, issue comments, and pull request text.

Embeddings and summaries inherit guards. They are transformed data, not
automatically safe data.

## Local-only Boundary

`local-only` allows processing on:

- a local process under qdistro policy;
- a local container or VM whose lifecycle and network are broker-controlled;
- a local model, indexer, OCR tool, summarizer, compiler, archiver, or
  converter that does not send prompts, outputs, telemetry, or payloads to a
  remote service;
- local storage, local snapshots, and local backups.

It denies by default:

- browser upload of guarded content;
- HTTP API calls that submit guarded content, including LLM, embedding, OCR,
  speech, translation, summarization, analytics, pastebin, issue tracker, CI,
  and document conversion APIs;
- cloud-backed editor saves where the remote service receives guarded content;
- remote Recall export;
- remote backup/export;
- telemetry or crash reports containing guarded payload, snippets, sensitive
  paths, or derived summaries;
- git push, issue upload, or pull request creation for guarded commits, diffs,
  archives, or messages.

Remote browsing is not automatically denied. The denied action is submitting
guarded content to a remote endpoint.

Workflow action handlers must report the effective processing host:

```yaml
processing:
  hostClass: local | local-vm | local-container | remote-service | unknown
  networkEgress: none | brokered | unrestricted
  payloadSubmitted: false
  telemetrySubmitted: false
```

For `local-only` input, `remote-service` and `unknown` deny. Local VMs and
containers are acceptable only when qdistro controls lifecycle and the action
does not submit guarded content through their network egress.

## Cross-silo Contamination

A work/home guard set should look like:

```yaml
metadata:
  labels:
    qdistro.io/silo.family: work
    qdistro.io/guard.no-cross-contaminate: "true"
security:
  compartments: [work]
  conflictClasses: [home-work-separation]
  guards: [no-cross-contaminate]
```

The default flow rule is:

```text
if source.guards contains no-cross-contaminate
and source.conflictClasses intersects destination.conflictClasses
and source.compartments is not subset-compatible with destination.compartments:
    deny or require an explicit transfer workflow
```

If an activity combines conflicting compartments, the output becomes a mixed
derivative:

```text
output.integrity = mixed
output.status.conditions += cross_contaminated
```

Mixed data is not destroyed, but future movement is restricted. It may live in
a review/export silo or in a target allowed by policy; it should not silently
become clean just because it was copied or summarized.

qdistro distinguishes two contamination modes:

- **Confidentiality contamination**: work secrets appeared in home, or home
  secrets appeared in work. This usually denies egress and requires
  declassification.
- **Integrity/context contamination**: untrusted or wrong-context input
  influenced a more controlled context. This may allow import into a review
  sandbox while blocking execution, credential use, auto-open, commit signing,
  or remote publication until reviewed.

## Policy Verdicts

Guard-aware policy returns these normalized verdicts:

- `allow`: proceed with no security-field change.
- `deny`: refuse; do not enqueue an approval prompt unless the caller
  separately requests a policy change.
- `prompt`: require owner/admin decision before proceeding.
- `warn`: allow but surface a visible warning and audit record.
- `transform`: run a declared sanitizer or converter and evaluate the output.
- `contaminate`: proceed and apply inherited or added guards.
- `sanitize`: produce a tracked derivative that may still carry residual
  guards.
- `declassify`: produce a tracked derivative whose guard set is narrowed by
  explicit authority.

`sanitize` and `declassify` are not synonyms. Sanitization is a transform;
declassification is an authority-bearing decision about the resulting
security fields.

## Declassification

Declassification is a workflow, not a tag deletion. It must record:

- source entity refs and hashes;
- output entity refs and hashes;
- destination and intended use;
- transform or sanitizer used;
- authority that approved the narrowing;
- policy decision and audit record;
- residual guards, compartments, and conflict classes.

The source remains guarded. The output remains derived from the source even
when its guard set is narrower.

## Chokepoint Defaults

| Chokepoint | Default behavior |
| --- | --- |
| Clipboard send/paste | Same-silo allow; cross-silo checks source, destination, MIME, guards, and conflict classes. |
| Rich clipboard | Require explicit MIME policy; prefer safe Markdown or sanitizer. |
| Browser upload | Deny guarded payload unless export/declassification permits it. |
| Browser download | Treat remote data as untrusted until imported/classified. |
| API/cloud-save | Deny guarded payload to remote services by default. |
| Recall ingest | Local ingest allowed; indexes and query results inherit source guards. |
| Recall export | Declassification workflow only. |
| Archive create | Archive inherits union of member guards. |
| Archive extract | Outputs inherit archive/member guards; record extractor and version. |
| Commit create | Commit inherits guarded diff/message/source inputs. |
| Git push/issue upload | Deny guarded commit/diff/message unless declassified. |
| Backup/export | Local backup allowed; remote backup denied unless a policy-specific exception exists. |
| Workflow step | Deny guarded inputs when effective processing host is remote or unknown. |

Status: this table is the target guard contract. In v1, guard enforcement is
implemented only on broker paths that explicitly call the lineage chokepoint
layer. Clipboard paste/receive is the first receive-time derivative path under
`lineage_enforce`; clipboard transfer/set-selection remains a permission/audit
gate, and the other rows remain design requirements until their handlers wire
`record_chokepoint` or an equivalent broker-owned guard decision.

## Hidden State

Stores that can retain content from more than one silo must be session-scoped,
silo-scoped, or broker-governed as resources with lineage:

- clipboard history;
- browser history, cookies, sessions, downloads, form autofill;
- editor recent files, project indexes, and language-server caches;
- shell history and terminal scrollback;
- Recall indexes and embeddings;
- thumbnail, OCR, crash dump, and telemetry queues;
- cloud sync queues;
- temporary files in shared runtime locations;
- git index, stash, reflog, hooks, and ignored build artifacts.

## Design Anchors

The design borrows from established systems without importing them wholesale:

- W3C PROV for entity/activity/agent lineage and derivation vocabulary.
- NIST SP 800-53 AC-4/AC-16 for information-flow decisions based on
  attributes of information, source, destination, and environment.
- Qubes OS for explicit inter-domain clipboard/file movement.
- SELinux MCS as a runtime compartment primitive, not the durable taxonomy.
- SLSA/in-toto style attestations as the shape for portable artifact receipts.
