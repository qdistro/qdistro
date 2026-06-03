# Resources

A resource is anything qdistro policy can grant. Files, browser profiles,
credential handles, windows, VMs, devices, silos, and transfer payloads all
use one manifest-shaped model so the broker can validate, grant, revoke, and
audit them consistently.

## Design Posture

qdistro resource and privilege definitions are an implementation-agnostic
control-plane API, like Kubernetes resources. They describe the desired
desktop/security object and its policy relationships; they do not replace the
native mechanism that installs packages, configures services, builds guest
images, or launches processes.

Native systems keep their native jobs:

- NixOS modules and flakes define NixOS systems and VM guests.
- RPM, zypper, OBS, Gentoo ebuilds, FreeBSD ports, Arch PKGBUILDs/AUR,
  Flatpak manifests, and container images provide software artifacts.
- systemd unit files describe service activation and dependency behavior.
- libvirt XML, QEMU arguments, Podman, and similar runtimes describe their
  own runtime substrate.

qdistro resources reference, constrain, activate, attach, revoke, audit, and
record lineage for those implementation artifacts. qdistro must not drift
into a universal package manager, a systemd replacement, or a NixOS
replacement. Implementation-specific details belong behind kind handlers and
adapters, while the resource model stays about qdistro policy.

This separation matters most for desktop software. A package recipe can
install Firefox. A qdistro `Silo` definition describes a managed desktop
workload built from that software: browser profile, expected account, state,
authority, isolation tier, grants, health checks, rollback policy, guards,
attachments, audit, and lineage.

## Two Definition Layers

qdistro should use two linked representations:

- **Owner-facing Markdown** — a predictable natural-language template with
  stable headings, simple key/value fields, and bullet lists. It should be easy
  for the owner and agents to read and write without losing intent.
- **Strict manifest** — a normalized machine contract produced from or linked
  to the Markdown. The broker validates this form, applies policy, records
  status, and audits changes.

The Markdown is the higher-level authoring layer. It is allowed to contain
runbooks, rationale, stop conditions, examples, and review notes. The template
should still be deliberately regular so agents can parse it with low ambiguity:
fixed section names, short field names, bounded value sets, and lists for
steps, checks, capabilities, and guardrails.

The strict manifest is the lower-level broker layer. It is not required to
mirror every sentence in the Markdown; it carries the normalized identity,
requested state, security fields, references, lifecycle hooks, and status the
broker needs. Exact field names are not finalized yet. Examples below are
illustrative, not a frozen schema.

This is not a contradiction: the Markdown may be the source of authored intent,
while the manifest is the source of execution. A broker or workflow should not
execute ambiguous prose directly. It should execute a strict manifest whose
source Markdown reference and hash are recorded.

## Markdown Template Rules

Owner-facing Markdown is only useful if it stays predictable. Resource
templates should follow these rules:

- Use stable heading names for identity, desired state, parameters,
  capabilities, security, health checks, actions, rollback/update policy, and
  guardrails.
- Put one concept in each bullet or key/value line.
- Use bounded value sets for policy-relevant fields where practical.
- State success, user-action, failure, stop, and forbidden conditions
  explicitly.
- Keep secrets, tokens, cookies, and passwords out of definitions.
- Keep implementation references short: package name, service name, flake ref,
  image digest, unit name, or domain id, not a full embedded native config.
- Prefer explicit "unknown" or "requires user action" over prose that lets an
  agent guess.

## Manifest Principles

The default manifest shape borrows the useful parts of the Kubernetes resource
model without importing its distributed-cluster machinery:

- a schema/version family plus a kind;
- stable metadata and broker-assigned identity;
- a requested-state area separated from observed status;
- labels for small indexed selectors and annotations for non-selector notes;
- owner/finalizer links for lifecycle cleanup;
- typed security fields, lineage references, and audit references;
- generation/revision tokens so agents, UI, workflows, and daemons can avoid
  silently overwriting each other.

`kind` identifies the resource type: examples include `Silo`,
`DirectoryGrant`, `Credential`, `DeviceStream`, `BrowserProfile`,
`WindowSurface`, `WorkflowRun`, or `TransferPayload`.

`Silo` is the managed desktop workload kind. Its owner-facing definition is
kept in predictable Markdown for now: desired state, parameters, bootstrap
steps, health checks, actions, rollback policy, and capability guardrails. See
[silos.md](silos.md).

VM-backed silos additionally carry a guest definition reference. New Linux VM
images should use the NixOS definition contract in
[vm-definitions.md](vm-definitions.md), while qdistro policy remains expressed
through the resource manifest.

A human-facing name may be reused after deletion. A broker-assigned immutable
identity is used by audit, lineage, owner references, and capability handles.

Requested state and observed state must remain separate. The broker can often
apply synchronously, but status still states which requested generation the
observation reflects. Conditions are the normal place for readiness, blocked
cleanup, policy denial, stale reconcile, and forced release records.

## Markdown To Manifest Example

An owner-facing silo definition can stay readable:

```md
# Silo: gmail-work

## Identity

type: browser-webapp
app: qdbrowser
profile: work-google
account_email: alice@example.com
purpose: Work Google account for Gmail and Calendar.

## Desired State

Open Gmail and Calendar using the configured work Google account.

## Capabilities

Required:
- network.https.google.com
- ui.browser

Forbidden:
- secret.agent_visible
- files.home.full_access

## Security

compartments:
- work

conflict_classes:
- home-work-separation

guards:
- no-cross-contaminate

## Health Checks

### Check: calendar account

Healthy result:
Google Calendar appears and the visible account is `alice@example.com`.

Needs user action:
A login page, MFA challenge, consent page, or session-expired message appears.

Failure:
The page cannot load, crashes, or shows a different account.
```

The broker-facing manifest generated from or linked to that Markdown should be
more regular:

```yaml
# Illustrative only; exact field names are still open.
apiVersion: resources.qdistro.io/v1alpha1
kind: Silo
metadata:
  name: gmail-work
  labels:
    qdistro.io/silo.family: work
spec:
  workload:
    type: browser-webapp
    app: qdbrowser
    profile: work-google
    expectedAccount: alice@example.com
  capabilities:
    required:
      - network.https.google.com
      - ui.browser
    forbidden:
      - secret.agent_visible
      - files.home.full_access
security:
  compartments: [work]
  conflictClasses: [home-work-separation]
  guards: [no-cross-contaminate]
status:
  conditions: []
```

The Markdown is not discarded. A strict manifest or run record should retain a
reference and hash for the source Markdown so drift is visible.

## Native Implementation References

A resource may point at an implementation artifact without adopting that
artifact's native schema. Examples:

```yaml
# Illustrative only.
spec:
  runtime:
    implementation: host-package
    package:
      manager: rpm
      name: firefox
    command: /usr/bin/firefox
```

```yaml
# Illustrative only.
spec:
  runtime:
    implementation: nixos-definition
    definitionRef: git+file:///srv/qdistro-vms#firefox-work
    command: /run/current-system/sw/bin/firefox
```

Both resources can describe the same qdistro workload. The package manager,
NixOS module, or VM builder supplies software. qdistro supplies policy,
attachments, grants, status, audit, and lineage.

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

The registry shape is not finalized. The important principle is that the core
schema stays small and kind-specific behavior is declared in one visible place,
not spread across UI code, daemon code, and workflow handlers.

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

## Open Decisions

- Exact manifest attribute names.
- Exact registry file/API shape.
- YAML, JSON, TOML, or multiple serializations.
- Exact schema validation language.
- Exact field ownership and conflict semantics for multiple writers.
- Which kinds support synchronous apply versus asynchronous reconcile.
