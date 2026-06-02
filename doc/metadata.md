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

System-defined labels use a reserved prefix such as `qdistro.io/*`.
Unprefixed labels are private to the owner, workflow, or local tool. Exact
prefix naming remains open, but the collision boundary is required.

Examples:

```yaml
metadata:
  labels:
    qdistro.io/kind: silo
    qdistro.io/project: qdistro
    client: acme
    purpose: commit
```

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
  declassification: none       # none | requested | approved
```

SELinux MCS categories may enforce some compartment decisions at runtime, but
they are not the durable taxonomy. MCS categories are scarce, checked after
DAC and type enforcement, and a process that accumulates enough categories can
become a bridge unless broker policy prevents category mixing.

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

- Exact reserved label prefix.
- Category allocation and exhaustion for MCS-backed compartments.
- Mutability rules for labels that affect historical audit queries.
- User-visible conflict taxonomy and declassification authority.
