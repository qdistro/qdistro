# Lineage

Lineage records where data, authority, UI actions, and generated outputs came
from. It is used for audit, contamination control, export review, and forensic
queries.

qdistro borrows W3C PROV vocabulary, not the full RDF/OWL stack and not an
enforcement model. Enforcement comes from the broker, compositor, SELinux,
resource handles, and workflow policy.

Enterprise data-lineage systems use similar ideas with different names:
source/target, upstream/downstream, dataset/job/run, process, column or
attribute lineage, transformation mapping, impact analysis, and data catalog.
qdistro keeps the PROV vocabulary for precision but should expose these
enterprise terms in UI and reports where they are clearer.

## Vocabulary

| qdistro | PROV term |
| --- | --- |
| resource, file, transfer payload, token, export, commit | Entity |
| workflow run, approval, paste, import, export, sanitize step | Activity |
| admin, app, agent, silo, credential authority | Agent |

Important edges:

- `used`: an activity consumed an entity.
- `wasGeneratedBy`: an entity was produced by an activity.
- `wasDerivedFrom`: an entity is a derivative or contaminated descendant.
- `wasAttributedTo`: an entity is attributed to an agent.
- `wasAssociatedWith`: an activity ran with an agent or plan.
- `actedOnBehalfOf`: one agent acted under another's authority.

Bundles describe provenance-of-provenance: who asserted a lineage statement,
when, and under which signing or audit authority.

Enterprise mapping:

| Enterprise term | qdistro meaning |
| --- | --- |
| Data asset, dataset | Entity, usually a file, resource, payload, archive, Recall result, or commit |
| Source / upstream | Entity or activity that feeds the current entity |
| Target / downstream | Entity or activity that consumes or derives from the current entity |
| Process, job, task | Activity, often a workflow step or brokered chokepoint |
| Run | One execution of an activity or workflow step |
| Data mapping | Declared input-to-output field, file, payload, or artifact mapping |
| Column/attribute lineage | Fine-grained lineage inside a structured payload |
| Impact analysis | Forward query: what downstream artifacts depend on this entity |
| Root-cause analysis | Reverse query: what upstream entities influenced this result |
| Data catalog | Searchable index over entities, labels, security fields, lineage, and audit |
| Business glossary | Owner-facing names for compartments, conflict classes, and policies |

qdistro should record entity-level lineage first. Attribute-level mapping is
optional and trusted only when produced by a broker-approved action handler or
parser. If attribute mapping is missing, policy falls back to whole-entity
inheritance.

## Authority

Security-relevant lineage is broker/compositor-derived. Apps may contribute
useful descriptive facets, but app-reported lineage cannot authorize
declassification, same-silo shortcuts, or audit identity.

Authoritative identity comes from broker decisions, kernel peer credentials,
SELinux labels, compositor endpoint identity, process snapshots, resource
handles, and workflow run records.

## Storage

The central lineage store is authoritative for policy and audit queries. It
must be append-only or tamper-evident enough for the threat it claims to
cover. Forward-secure journal sealing is only a possible primitive, and only
helps materially when verification keys are kept outside the compromised host.

The store should be local-first and relational enough to answer forward and
reverse queries:

- which guarded inputs contributed to this output;
- which outputs derive from a guarded source;
- which exports or declassifications involved a guard;
- which artifacts need review after reclassification;
- which app-reported facets are descriptive versus broker-derived authority.

Minimum logical tables:

- `entities`: resource/file/payload/artifact id, kind, digest or locator,
  current security snapshot, status.
- `activities`: workflow step, paste, import, export, upload, commit,
  sanitize/declassify, with action version and effective processing host.
- `agents`: silo, app, broker, workflow, credential authority, admin approval.
- `edges`: `used`, `wasGeneratedBy`, `wasDerivedFrom`, `wasAttributedTo`,
  `wasAssociatedWith`, `actedOnBehalfOf`.
- `assertions`: who asserted each lineage/security fact and under what
  authority.
- `receipts`: sidecar, xattr, git trailer, export manifest, upload receipt, or
  attestation pointer.

Data mapping records belong beside lineage edges:

- `mapping_activity`: activity id, mapping kind, parser/handler version.
- `mapping_input`: source entity, optional field/path/range/MIME part.
- `mapping_output`: target entity, optional field/path/range/MIME part.
- `mapping_confidence`: broker-derived, trusted-tool, app-reported, or
  inferred.

Only `broker-derived` and explicitly trusted-tool mappings can narrow guard
propagation. App-reported and inferred mappings are useful for UI and search
but not for reducing inherited guards.

Retention is policy-based by record kind. Security decisions, declassification
evidence, and export receipts should outlive ordinary Recall TTL and UI event
history. When payloads expire, lineage may keep digests, source refs, guards,
and audit decisions without retaining the payload.

Artifact-adjacent records are portable receipts:

- git commit trailers;
- sidecar files;
- export manifests;
- upload receipts;
- xattrs where available;
- in-toto-style statements for digest-addressed artifacts.

They should reference central records when possible. They are not sufficient
integrity by themselves.

Use stable receipt names:

- sidecar: `<artifact>.qdistro-lineage.json`
- xattr: `user.qdistro.lineage`
- git trailer: `Qdistro-Lineage`
- export manifest: `qdistro-export-manifest.json`
- upload receipt: `qdistro-upload-receipt.json`
- attestation predicate prefix: `https://qdistro.io/attestation/`

Receipts identify immutable artifacts by digest where possible. Mutable
locators such as paths, URLs, and branch names are secondary.

## Contamination

When data merges, contamination labels are conservatively unioned. qdistro
does not claim full fine-grained information-flow noninterference from coarse
chokepoints; chokepoints are pragmatic audit and policy boundaries.

Guard propagation is defined in [guards.md](guards.md). In short, outputs
inherit the union of input guards, compartments, and conflict classes unless a
trusted action handler records a narrower input-to-output mapping.

Sanitize/export is not lineage erasure. It is a privileged workflow that
produces a tracked derivative or variant, with evidence of the transform and
the authority that approved declassification.

Archive extraction and import are brokered workflows. Extracted files get new
lineage edges recording the archive, extractor, tool version, and inherited
contamination. Do not rely on host xattr propagation.

## Chokepoints

Initial lineage should be captured at high-value boundaries:

- workflow run start/end;
- resource attach/detach;
- clipboard transfer and paste;
- file import/export and archive extraction;
- browser upload/download;
- commit creation and signing;
- Recall query/export;
- credential use.

Finer file-line and UI-action lineage is a future direction.

## Data Mapping Granularity

The default grain is whole-entity lineage. qdistro may add finer mapping where
the handler can make a defensible claim:

- archive member to extracted file;
- file path to copied file;
- MIME part to clipboard payload;
- structured field to structured field;
- source document to summary paragraph;
- Recall capture row to query result;
- commit tree/diff/message to commit object.

Column-level or field-level lineage is useful for impact analysis, but it must
not create false confidence. When qdistro cannot prove which field contributed
to which output, it maps the whole input to the whole output.

## App-reported Facets

Apps may report useful descriptive facets: page title, URL, editor buffer
name, selected MIME type, app-visible document id, or user-facing account
string. These facets help UI and audit search.

They cannot authorize policy. Security-relevant identity comes from broker
decisions, kernel peer credentials, SELinux labels, compositor endpoint
identity, resource handles, and workflow records.

If app-reported data conflicts with broker-derived identity, qdistro stores
both, marks the app facet as advisory, and evaluates policy against the
broker-derived identity.

## Commit Example

Commit trailers are useful parseable receipts, not integrity by themselves:

```text
Assisted-by: Claude Code
Lineage-Workflow: workflowrun:...
Lineage-Source: resource:...
Lineage-Authority: credential:github-signing-key
```

Signed commits and the central store provide the integrity story.

## Open Decisions

- Verification-key custody for tamper-evident logs.
- Exact SQLite schema and migration strategy for the logical store above.
- Whether qdistro uses in-toto envelopes directly for portable attestations or
  only for exported artifacts.
