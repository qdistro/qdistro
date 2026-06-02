# Metadata

qdistro separates selector metadata, rich descriptions, security
classification, lineage, and audit records. A single `tags` field is too
ambiguous for policy.

## Blocks

Resource and workflow manifests use these blocks:

- `metadata.labels` — small, indexed key/value selectors used by policy,
  UI grouping, and fast queries.
- `metadata.annotations` — non-selector descriptions for tools and humans.
  They may be structured, but large blobs belong elsewhere with a reference.
- `security` — typed policy-relevant fields such as sensitivity,
  integrity, compartments, conflict classes, authority, and
  declassification state.
- `lineageRefs` — references into the authoritative lineage graph.
- `auditRefs` — references to append-only decision and event records.

`tags` may remain a user-facing shorthand, but manifests use the precise
fields above.

## Labels

Labels are constrained because they sit on the hot path. They should be
stable, bounded, and cheap to index. Policy can match labels with exact or
declared selector semantics; labels are not a place for prose, screenshots,
large JSON, or provenance graphs.

System-defined labels use the reserved prefix `qdistro.io/*`. Unprefixed
labels are private to the owner, workflow, or local tool. This follows the
same collision-avoidance rule as Kubernetes-style labels: automated components
use a DNS-style prefix, while unprefixed keys remain private.

Examples:

```yaml
metadata:
  labels:
    qdistro.io/kind: silo
    qdistro.io/project: qdistro
    qdistro.io/guard.local-only: "true"
    client: acme
    purpose: commit
```

Reserved guard labels are policy selectors, not proof by themselves. For
example `qdistro.io/guard.local-only: "true"` lets policy quickly find
resources that must never be uploaded to a remote service, while the typed
`security.guards` field below carries the authoritative classification.

Reserved selector families:

- `qdistro.io/kind`
- `qdistro.io/project`
- `qdistro.io/silo.family`
- `qdistro.io/guard.<name>`
- `qdistro.io/authority.<name>`
- `qdistro.io/workflow.<name>`

## Annotations

Annotations hold non-selector metadata: display hints, generator versions,
schema notes, tool checksums, or links to external records. They are not
authoritative policy identity by default. Specialized policy may read a
bounded typed annotation, but routine allow/deny decisions should use labels
and typed `security` fields.

## Security

Security metadata is typed, not stringly tags. The broker evaluates it as
ABAC input: subject, action, resource, environment, labels, and security
fields go into policy; policy returns allow, deny, prompt, warn, transform,
contaminate, or declassify.

Core fields:

```yaml
security:
  sensitivity: internal        # public | internal | confidential | restricted
  integrity: trusted           # trusted | mixed | untrusted
  compartments: [client-acme]
  conflictClasses: [client-conflict]
  authority: [github-signing]
  guards: [local-only]         # local-only | no-cross-contaminate | ...
  declassification: none       # none | requested | approved
```

SELinux MCS categories may enforce some compartment decisions at runtime, but
they are not the durable taxonomy. MCS categories are scarce, checked after
DAC and type enforcement, and a process that accumulates enough categories can
become a bridge unless broker policy prevents category mixing.

`local-only` is the guard for data that may be processed only on local
qdistro-controlled compute. It denies browser upload, API calls that submit
content to a remote service, cloud-backed editor saves, remote Recall export,
remote backup/export, and any workflow step whose effective processing host is
not local. The guard is viral: any artifact, summary, embedding, transformed
payload, model output, archive, commit, or dataset derived from a `local-only`
source inherits `local-only` until an explicit declassification workflow
records the transform, authority, and audit evidence.

`no-cross-contaminate` is the guard for resources whose compartments must not
be mixed by default. A `work` resource must not be pasted, imported, indexed,
summarized, or merged into `home` resources, and `home` data must not flow into
`work`, unless policy explicitly allows a brokered transfer or an approved
sanitize/declassify workflow produces a new tracked derivative.

See [guards.md](guards.md) for guard propagation, local-only processing
boundaries, cross-silo contamination, and declassification semantics.

## Mutability

Current manifest labels and security fields may change, but historical audit
and lineage records do not. A reclassification records a new event and may add
propagated guard facts to downstream entities. It must not rewrite old broker
decisions as if they were made under the new classification.

Policy reads the current resource state when deciding a new action. For
forensics, qdistro reads the historical resource revision referenced by the
audit or lineage record.

Mutable-by-default:

- descriptive labels;
- annotations;
- owner-facing display names;
- current status conditions.

Policy-controlled mutation:

- sensitivity;
- guards;
- compartments;
- conflict classes;
- authority;
- declassification state.

Immutable after record creation:

- audit decision rows;
- lineage edges;
- workflow-run input/output refs;
- artifact hashes in receipts;
- declassification evidence.

## MCS Mapping

SELinux MCS categories may back selected compartment decisions at runtime, but
they are allocated from a small implementation pool and are not the taxonomy.
qdistro maps durable compartments to MCS categories only for active processes,
mounts, or attachments that benefit from kernel enforcement.

Allocation rules:

- allocate categories per active attachment or session, not per historical
  label;
- keep the broker and other privileged services out of broad category ranges
  except where strictly required;
- never let a workload accumulate unrelated categories merely to make sharing
  convenient;
- on exhaustion, fail closed or fall back to broker-only access with an
  explicit warning, never silently widen access;
- release category allocations when the attachment/session finalizer completes.

MCS labels are receipts of runtime confinement. Durable policy still comes
from `security.compartments`, `security.conflictClasses`, `security.guards`,
lineage, and audit.

## Lineage And Audit

Lineage is relational history, not mutable selection state. It uses
PROV-style vocabulary in [lineage.md](lineage.md). Audit records are
append-only broker decisions and observed events. PROV gives useful terms; it
does not provide immutability or enforcement by itself.

Artifact-adjacent carriers such as xattrs, git trailers, sidecars, export
manifests, and upload receipts are useful receipts. They point back to central
lineage/audit where possible and are not authoritative on their own.

Import, archive extraction, export, upload, paste, and commit creation are
brokered chokepoints. They create new lineage edges instead of relying on
filesystem xattr propagation, which varies by tool and storage backend.

## Open Decisions

- Exact validation schema for each `qdistro.io/*` selector family.
- Whether guard vocabulary lives in a static registry file, compiled broker
  schema, or both.
