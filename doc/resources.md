# Resources

A resource is anything qdistro policy can grant. Files, browser profiles,
credential handles, windows, VMs, devices, silos, and transfer payloads all
use one manifest-shaped model so the broker can validate, grant, revoke, and
audit them consistently.

## Manifest Shape

The default shape borrows the useful parts of the Kubernetes resource model
without importing its distributed-cluster machinery:

```yaml
apiVersion: resources.qdistro.io/v1alpha1
kind: Silo
metadata:
  name: dev
  uid: 8f6c2d10-...
  generation: 3
  revision: 17
  labels: {}
  annotations: {}
  ownerReferences: []
  finalizers: []
spec: {}
security: {}
lineageRefs: []
auditRefs: []
status:
  observedGeneration: 3
  conditions: []
```

`apiVersion` names the schema family. If qdistro later chooses
`schemaVersion`, it must still carry the same routing and migration meaning.

`kind` identifies the resource type: `Silo`, `DirectoryGrant`,
`Credential`, `DeviceStream`, `BrowserProfile`, `WindowSurface`,
`WorkflowRun`, or `TransferPayload`.

`Silo` is the managed desktop workload kind. Its owner-facing definition is
kept in semistructured Markdown for now: desired state, parameters, bootstrap
steps, health checks, actions, rollback policy, and capability guardrails. See
[silos.md](silos.md).

`metadata.name` is human-facing and may be reused after deletion.
`metadata.uid` is broker-assigned, immutable, and used by audit, lineage,
owner references, and capability handles.

`metadata.generation` increments when `spec` changes.
`metadata.revision` is a local compare-and-swap token for concurrent edits by
admin UI, agents, workflows, and daemons. qdistro does not need Kubernetes
watch/informer machinery, but it does need a version token.

`spec` is requested state. `status` is observed state. The broker can often
apply synchronously, but `status.observedGeneration` still states which spec
generation the observation reflects.

`status.conditions[]` is the normal place for readiness, blocked cleanup,
policy denial, stale reconcile, and forced release records.

## Metadata And Security

Labels, annotations, security fields, lineage references, and audit
references are separate. See [metadata.md](metadata.md). Owner references are
lifecycle links, not data provenance.

## Kind Registry

Kinds are registered as data rather than scattered ad hoc code. A kind
registration declares:

- manifest schema and status schema;
- allowed verbs;
- policy hooks and admission checks;
- apply/reconcile handler;
- attachment and grant semantics;
- supported finalizers.

Validation is in-process and compiled where practical. qdistro does not need
OpenAPI admission webhooks to validate local desktop grants.

## Lifecycle

Create/update/delete flows are brokered:

1. caller submits a manifest or patch;
2. broker validates schema and policy;
3. broker writes the new generation/revision;
4. kind handler applies or reconciles;
5. status and audit records are written.

Deletion is two-phase when cleanup matters. A delete marks the resource as
deleting, finalizers revoke grants, detach mounts, close handles, flush audit
receipts, and remove sidecars. The row is removed only after finalizers
complete or after a policy-approved forced release.

## Capability Handles

An approved grant is a scoped, revocable capability handle recorded by the
broker. Holding the handle is not enough to bypass policy forever: revocation,
lock state, workflow scope, and finalizers can invalidate it. Every handle has
lineage and audit identity.

## Examples

```yaml
apiVersion: resources.qdistro.io/v1alpha1
kind: DirectoryGrant
metadata:
  name: qdistro-source-readonly
  labels:
    qdistro.io/project: qdistro
spec:
  sourceResource: silo:dev
  path: /home/dev/src/qdistro
  mode: readOnly
security:
  sensitivity: internal
```

```yaml
apiVersion: resources.qdistro.io/v1alpha1
kind: Credential
metadata:
  name: github-signing-key
spec:
  vaultRef: vault:work/github-signing-key
  deliveryModes: [agent-socket, fd]
security:
  authority: [github-signing]
  sensitivity: restricted
```

## Open Decisions

- Exact schema/version field spelling.
- Exact registry file/API shape.
- YAML, JSON, TOML, or multiple serializations.
- Which kinds support synchronous apply versus asynchronous reconcile.
