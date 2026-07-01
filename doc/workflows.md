# Workflows

A workflow is a policy-checkable plan that coordinates resources, actions,
approvals, cleanup, and lineage. Workflows are not hardcoded product flows.

## Status

This document describes the workflow contract qdistro is building toward. The
current v1 engine is a narrow execution substrate for approved, fail-closed
steps such as secret delivery, broker / D-Bus calls, hooks, and related
automation glue. It does not yet implement the full contract below.

Not yet implemented in v1: declared compensation execution, cleanup-specific
terminal states such as `cleanup_failed`, `compensation_failed`,
`forced_release`, and `requires_human_review`, durable crash resume for running
workflows, first-class data-flow records, and workflow-run / step / artifact
lineage edges. Side-effecting actions such as browser upload, export, VM spawn,
or remote mutation must not be treated as covered by this document's
compensation guarantees until those mechanisms exist in code.

## Design Posture

qdistro workflows are an implementation-agnostic orchestration contract. They
describe what resource relationships, data flows, approvals, and cleanup must
happen; they do not replace shell scripts, package recipes, systemd units,
NixOS modules, browser automation libraries, or native application APIs.

Native mechanisms keep their native jobs:

- shell commands and systemd units run processes;
- package managers and image builders provide software artifacts;
- browser extensions and accessibility APIs expose UI operations;
- libvirt, Podman, waypipe, and qdwin provide runtime substrate;
- action handlers perform side effects through their native API.

The workflow layer coordinates those mechanisms through broker-approved
actions. It gives qdistro one place to validate intent, collect approvals,
attach resources, propagate guards, record data lineage, and release
capability handles. Implementation-specific details belong in action handlers,
not in the workflow core language.

## Two Layers

Workflows use two linked layers:

- **Markdown plan** — predictable human and agent readable intent: what the
  owner wants, why, expected interactions, data-flow expectations, stop
  conditions, and review notes.
- **Strict execution manifest** — normalized machine contract: typed inputs,
  resources, roles, steps, conditions, data flows, cleanup, compensation,
  outputs, lineage, and audit fields.

The strict manifest is the source of truth for execution. The Markdown plan is
retained for review and traceability. A run records hashes of both so drift is
visible.

This differs from owner-facing resource authoring, where predictable Markdown
may be the higher-level source of intent. In both cases, the broker executes
only the strict manifest form; prose is retained for human/agent review and
drift detection.

## Markdown Plan Rules

Workflow Markdown should be easy to inspect and easy for agents to transform
into a strict manifest. Use stable sections:

- intent and non-goals;
- actors and roles;
- inputs, attached resources, and required authority;
- data flows and expected outputs;
- steps and stop conditions;
- approvals and lock/unlock requirements;
- cleanup, compensation, and failure handling;
- lineage and audit notes.

Policy-relevant prose should be made explicit with key/value fields or bounded
lists. If a plan says "summarize this file and upload the result," it must also
state which file is input, which action transforms it, where the output goes,
whether a remote service is involved, and which guards should be inherited or
narrowed.

Secrets, tokens, cookies, and passwords do not belong in workflow definitions.
Workflow plans may reference credential resources, but delivery happens only
through broker-approved handles.

## Manifest Principles

The exact manifest field names are not finalized. The stable principles are:

- A workflow template declares reusable intent and typed parameters.
- A workflow run records one concrete execution with input values, approvals,
  action versions, resource handles, output refs, status, audit, and lineage.
- Conditions are structured, not prose.
- Steps are declared actions implemented by broker-approved handlers.
- Data flows are first-class declarations, not comments hidden inside step
  descriptions.
- Cleanup and compensation are declared, best-effort actions with their own
  terminal states.
- The broker validates the manifest before execution and records the Markdown
  source ref/hash so drift is visible.

Illustrative shape:

```yaml
# Illustrative only; exact field names are still open.
apiVersion: workflows.qdistro.io/v1alpha1
kind: WorkflowTemplate
metadata:
  name: claude-code-browser-auth
spec:
  inputs: []
  resources: []
  dataFlows: []
  steps: []
  cleanup: []
  outputs: []
```

Step fields are typed, but not a general-purpose programming language:

```yaml
# Illustrative only; exact field names are still open.
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
    dataFlows:
      uses: []
      generates: []
```

Conditions are structured, not prose. Reuse happens through typed templates,
declared inputs, and structured overlays, not Helm-style text substitution or
YAML-anchor cleverness.

## Data Flows

A workflow must declare material data movement and transformation. Data flows
are the bridge between workflow execution, guard propagation, and lineage.

Each material flow should identify:

- source entities or resources;
- destination entities, resources, or remote endpoints;
- action or transform that moves or changes the data;
- effective processing host: local, local VM/container, remote service, or
  unknown;
- whether payload, telemetry, prompts, outputs, or derived summaries leave the
  local qdistro-controlled boundary;
- expected output artifacts and their security inheritance;
- sanitizer/declassification authority when a guard is narrowed.

The default policy is conservative: every output inherits the union of input
guards, compartments, and conflict classes unless a trusted action handler
records a narrower mapping. This matches [guards.md](guards.md) and
[lineage.md](lineage.md).

Illustrative flow:

```yaml
# Illustrative only; exact field names are still open.
dataFlows:
  - id: summarize-local-note
    from:
      - resource: file:work-note
    action: text.summarize
    processing:
      hostClass: local
      payloadSubmitted: false
      telemetrySubmitted: false
    to:
      - output: summary-markdown
    security:
      inherit: guards-and-compartments
```

Remote or unknown processing is policy-relevant:

```yaml
# Illustrative only; exact field names are still open.
dataFlows:
  - id: upload-report
    from:
      - output: redacted-report
    action: browser.upload
    destination:
      origin: https://example.com
    processing:
      hostClass: remote-service
      payloadSubmitted: true
```

For guarded inputs, a remote flow normally denies unless an export,
sanitize, or declassification workflow has produced an approved derivative.

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

Action handlers must report what actually happened in broker-consumable form:
input refs used, output refs generated, effective processing host, payload or
telemetry submission, app-reported facets, errors, and cleanup state. Handler
reports are evidence. Security-relevant authority still comes from the broker,
kernel credentials, compositor identity, SELinux labels, capability handles,
and workflow approvals.

## GUI-Assisted Steps

GUI automation should prefer accessibility-tree references, roles, names, and
auto-wait/actionability semantics. Coordinate and screenshot actions are a
fallback and should carry stronger review requirements.

For each GUI action, lineage records the snapshot version, role/name/ref used,
the owning resource/session, the intended data flow if one exists, and
post-action evidence. Screenshots are evidence, not authority: they can support
review, but broker-derived identity and resource handles drive policy.

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

Cancellation and timeout are workflow states, not just process signals. A
cancelled run must still attempt declared cleanup, mark any unreleased handle,
and record whether partial outputs exist. A timeout should say which step timed
out, whether the action handler can safely retry, and whether human review is
required before continuing.

## Run Records

A `WorkflowRun` records:

- template and plan identity;
- plan hash and manifest hash;
- input values;
- resource refs and capability handles;
- approval refs;
- action versions;
- data-flow records and handler reports;
- outputs;
- cleanup/compensation state;
- lineage refs and audit refs.

Security-relevant lineage is derived by the broker, compositor, and kernel
identity paths. App-reported fields are advisory.

Runs should also record drift checks:

- Markdown source ref and hash;
- strict manifest ref and hash;
- action handler names and versions;
- accessibility snapshot refs used by GUI actions;
- resource generations or revisions used as inputs.

If the Markdown plan, strict manifest, or action handler version changes after
approval, policy decides whether the run may continue, needs reapproval, or
must be cancelled.

## Lineage Contract

Workflow execution creates lineage at three levels:

- run level: template, plan, approvals, actors, and resources involved;
- step level: action handler, processing host, inputs, outputs, and status;
- artifact level: generated files, payloads, commits, credentials, exports,
  receipts, and derived security fields.

Every material input should become a `used` edge. Every material output should
be `wasGeneratedBy` the step that produced it. Derived outputs should carry
`wasDerivedFrom` links to their inputs, directly or through the generating
activity. Approvals and credential authorities are agents associated with the
activity, not silent ambient context.

When an action handler can prove a narrower mapping, such as one archive member
to one extracted file or one field to one output field, it may record data
mapping records. Without a trusted mapping, qdistro uses whole-entity lineage
and whole-entity guard inheritance.

## Browser Auth Example

Intent: authenticate a dev tool to Anthropic using an existing Google login.

1. Attach a Gmail browser profile as an authority-bearing resource.
2. Temporarily route the terminal auth URL into that browser.
3. Use accessibility-based agent actions to complete the login if needed.
4. Return only the callback code or token to the dev workflow.
5. Restore the normal link handler and detach the browser resource.

The resulting token is a credential resource. Future uses of it carry lineage
back to the workflow run, browser profile, and approval that minted it.

The data-flow boundary is narrow: the workflow attaches browser login
authority, opens the auth URL, and returns only the callback code or resulting
credential resource. It must not expose cookies, browser storage, or the
profile contents to the agent.

## VM Build Workflow

Building or updating a VM-backed silo is a workflow. For new Linux VM images,
the input should be a NixOS module or flake output as described in
[vm-definitions.md](vm-definitions.md). The run records the definition ref,
lock ref, builder, build command, output digest, image manifest, and health
checks.

The built image is an artifact, not authority by itself. Runtime permission
still comes from qdistro resource grants, secctx identity, and broker policy.

The build flow records definition inputs, package/image inputs where available,
builder identity, build command, output digest, guest health checks, and the
resource manifest that will activate the image. If the guest definition and
qdistro resource disagree about exposed services or publisher behavior, the
activation workflow fails closed.

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

## Workflow API Shape

Exact D-Bus methods remain open, but the broker surface should follow the same
separation as the model:

- validate or render a Markdown plan into a strict manifest;
- submit a strict manifest for policy/admission;
- start a run;
- inspect run status, data flows, outputs, lineage, and audit refs;
- cancel a run;
- retry or resume only when the action and policy allow it;
- force release only with explicit audit.

Planning and validation may be safe while locked. New grants, new cross-silo
approvals, sensitive exports, and authority-bearing runs require admin unlock
unless a previously approved workflow explicitly carries lock-continuation
semantics.

## Open Decisions

- Exact strict manifest attribute names and validation language.
- Exact `WorkflowTemplate` and `WorkflowRun` kind registrations.
- Exact D-Bus method names and return shapes.
- Exact cancellation, timeout, retry, and partial-detach state machine.
- Exact approval/signature model for generated manifests and drift checks.
- Exact versioning model for action handlers and accessibility snapshot refs.

## See Also

- [resources.md](resources.md)
- [attachments.md](attachments.md)
- [metadata.md](metadata.md)
- [lineage.md](lineage.md)
- [guards.md](guards.md)
- [vm-definitions.md](vm-definitions.md)
- [permissions.md](permissions.md)
