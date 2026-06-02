# Lineage

Lineage records where data, authority, UI actions, and generated outputs came
from. It is used for audit, contamination control, export review, and forensic
queries.

qdistro borrows W3C PROV vocabulary, not the full RDF/OWL stack and not an
enforcement model. Enforcement comes from the broker, compositor, SELinux,
resource handles, and workflow policy.

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

Artifact-adjacent records are portable receipts:

- git commit trailers;
- sidecar files;
- export manifests;
- upload receipts;
- xattrs where available;
- in-toto-style statements for digest-addressed artifacts.

They should reference central records when possible. They are not sufficient
integrity by themselves.

## Contamination

When data merges, contamination labels are conservatively unioned. qdistro
does not claim full fine-grained information-flow noninterference from coarse
chokepoints; chokepoints are pragmatic audit and policy boundaries.

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

- Central store schema and retention.
- Verification-key custody for tamper-evident logs.
- Exact artifact-adjacent schema names.
- Policy semantics for deny, warn, contaminate, and declassify.
