# Workflows

A workflow is a policy-checkable plan that coordinates resources, actions,
approvals, cleanup, and lineage. Workflows are not hardcoded product flows.

## Two Layers

Workflows use two linked layers:

- **Markdown plan** — human and agent readable intent: what the owner wants,
  why, expected interactions, and review notes.
- **Strict manifest** — YAML or JSON execution contract: typed inputs,
  resources, steps, conditions, cleanup, compensation, outputs, lineage, and
  audit fields.

The strict manifest is the source of truth for execution. The Markdown plan
is retained for review and traceability. A run records hashes of both so drift
is visible.

## Manifest Sketch

```yaml
apiVersion: workflows.qdistro.io/v1alpha1
kind: WorkflowTemplate
metadata:
  name: claude-code-browser-auth
spec:
  inputs: []
  resources: []
  steps: []
  cleanup: []
  outputs: []
```

Step fields are typed:

```yaml
steps:
  - id: open-auth-url
    action: browser.open
    resources:
      browser: resource:browser-profile:gmail-work
    conditions: []
    retry:
      maxAttempts: 2
    onFailure:
      action: fail
    compensation:
      action: browser.close-tab
    lineage:
      uses: []
      generates: []
```

Conditions are structured, not prose. Reuse happens through typed templates,
declared inputs, and structured overlays, not Helm-style text substitution or
YAML-anchor cleverness.

## Actions

The manifest stays declarative. Side effects happen through declared actions
implemented by broker-approved handlers: attach resource, deliver secret,
transfer clipboard, start browser, click accessibility ref, run command,
spawn VM, export artifact.

Each action declares whether it is:

- idempotent;
- reversible;
- compensatable;
- terminal;
- allowed while locked;
- allowed to produce, contaminate, sanitize, or declassify data;
- local, local-VM/container, remote-service, or unknown processing.

Arbitrary Python belongs in explicit hooks or action handlers, not inside the
policy-checkable manifest core.

## GUI-Assisted Steps

GUI automation should prefer accessibility-tree references, roles, names, and
auto-wait/actionability semantics. Coordinate and screenshot actions are a
fallback and should carry stronger review requirements.

For each GUI action, lineage records the snapshot version, role/name/ref used,
the owning resource/session, and post-action evidence.

## Cleanup And Failure

Cleanup is declared; it is not promised as magic rollback. Workflows run
cleanup and compensation on normal completion and failure where the system is
able to do so. Terminal states include:

- `succeeded`;
- `failed`;
- `cleanup_failed`;
- `compensation_failed`;
- `forced_release`;
- `requires_human_review`.

Compensation is not time travel. It is a declared follow-up action in a world
that may have moved on.

Workflow-owned resources use owner references and finalizers so grants,
mounts, sockets, streams, temporary browser state, and audit receipts are
released before the run is considered closed.

## Run Records

A `WorkflowRun` records:

- template and plan identity;
- plan hash and manifest hash;
- input values;
- resource refs and capability handles;
- approval refs;
- action versions;
- outputs;
- cleanup/compensation state;
- lineage refs and audit refs.

Security-relevant lineage is derived by the broker, compositor, and kernel
identity paths. App-reported fields are advisory.

## Browser Auth Example

Intent: authenticate a dev tool to Anthropic using an existing Google login.

1. Attach a Gmail browser profile as an authority-bearing resource.
2. Temporarily route the terminal auth URL into that browser.
3. Use accessibility-based agent actions to complete the login if needed.
4. Return only the callback code or token to the dev workflow.
5. Restore the normal link handler and detach the browser resource.

The resulting token is a credential resource. Future uses of it carry lineage
back to the workflow run, browser profile, and approval that minted it.

## VM Build Workflow

Building or updating a VM-backed silo is a workflow. For new Linux VM images,
the input should be a NixOS module or flake output as described in
[vm-definitions.md](vm-definitions.md). The run records the definition ref,
lock ref, builder, build command, output digest, image manifest, and health
checks.

The built image is an artifact, not authority by itself. Runtime permission
still comes from qdistro resource grants, secctx identity, and broker policy.

## Export And Declassification

Export is a workflow, not a viewer-only action. A sanitize/export step creates
a tracked derivative with lineage, labels/security changes, destination
metadata, and approval evidence.

Recall export, rich clipboard transfer, browser upload, and commit creation
all use this shape.

For guarded inputs, the workflow records:

- source entity refs and hashes;
- requested destination and effective processing host;
- transform or sanitizer action version;
- output entity refs and hashes;
- inherited guards, compartments, and conflict classes;
- any narrowed guard set and the authority that approved it.

Sanitization is a transform. Declassification is the authority-bearing decision
that narrows security fields on the transformed output. The source remains
guarded and the output remains derived from it.

## See Also

- [resources.md](resources.md)
- [attachments.md](attachments.md)
- [metadata.md](metadata.md)
- [lineage.md](lineage.md)
- [guards.md](guards.md)
- [vm-definitions.md](vm-definitions.md)
- [permissions.md](permissions.md)
