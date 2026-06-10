# Glossary

qdistro uses normal Linux users as one implementation tool, but the product
model is not "one Linux user equals one human" or "one Linux user equals one
session." These terms are the conceptual model.

## Owner

The one physical person who owns the machine. The owner authenticates as admin,
controls hardware, sets policy, and can override the system because they have
physical and root access.

There is one owner. qdistro is not a multi-human workstation.

## Admin

The trusted control-plane identity for the owner. Admin owns hardware, policy,
the lock/unlock path, approval UI, settings authority, and session lifecycle.

Admin is the closest qdistro equivalent to Qubes `dom0`, but qdistro does not
require admin to be a no-work zone in early versions. Admin-owned windows must
remain visually unmistakable because they are part of the trusted base.

## Resource

A resource is any named thing a session, workflow, or app can request and
policy can grant. The term is intentionally broad, closer to Kubernetes
resources than to Unix users. Examples:

- a filesystem tree or selected directory;
- a browser profile or logged-in web session;
- a running app surface from an embedded compositor;
- an SSH key, signing key, vault item, token, or agent socket;
- a VM, container, network namespace, camera stream, microphone stream, or
  printer;
- a one-shot transfer payload such as clipboard content, exported files, or a
  rendered image.

Resources carry labels, annotations, security metadata, audit identity, and
lineage references. A resource may be backed by a Linux uid, file, D-Bus
service, VM, browser profile, or something else.

`Resource` is a precise policy/API noun, not necessarily a simplified UI noun.
The owner may ask an agent for a human-level task; the agent can translate that
request into a specific resource plan.

Resources have a **kind**. Open candidate kinds include:

- filesystem tree or directory grant;
- browser profile / authenticated web session;
- credential, token, key, vault item, or agent socket;
- app surface / window / embedded compositor;
- VM, container, or process namespace;
- device stream such as microphone, camera, audio, printer, or Bluetooth;
- network route / namespace / proxy;
- one-shot transfer payload.

A resource **definition** has two layers: predictable owner-facing Markdown for
authored intent, and a normalized manifest the broker validates and applies.
The exact manifest fields are still open, but the stable principles are:
schema/version family, kind, metadata, requested state, observed status, typed
security fields, lineage references, and audit references. See
[resources.md](resources.md).

Labels are small indexed selectors; annotations are non-selector descriptive
metadata; security fields are typed policy inputs. See
[metadata.md](metadata.md).

## Silo

A qdistro resource kind that packages an isolated program context with its
state, data, expected behavior, health checks, actions, rollback policy, and
guarded capabilities. A silo may be a single program, minimal VM, container,
uid-backed process group, browser profile logged into Gmail, or similar
self-contained desktop workload.

A silo is not a session. A silo can be attached to different sessions at
different times, and some attachment types may allow the same silo to be used
by multiple live sessions at once. For example, a source-code silo may be
attached to a development work session that lacks commit keys, then later or
simultaneously attached to a commit work session that has access to signing or
GitHub credentials.

Some silos contain authority, not only data. A browser profile logged into
Gmail can authenticate other services; a signing-key silo can publish commits;
a cloud-CLI silo can operate on remote infrastructure. Treating these as silos
keeps the policy and lineage model honest: "attach data" and "attach
authority" are separate decisions.

Current code often backs a silo with a Linux uid, home subvolume, runtime dir,
and cgroup scope. That is an implementation shape, not the whole concept.
Containers, VMs, credential stores, and browser profiles can also be part of
the silo model.

Silo definitions are predictable Markdown for now. They describe parameters,
bootstrap steps, health checks, recovery actions, and agent/user guardrails in
a form humans can read and agents can transform into a stricter broker
manifest. See [silos.md](silos.md).

`Silo` is qdistro's term for this resource kind. Qubes uses "qube" / "domain"
for isolated compartments; Kubernetes has "pods" as runnable groups of
containers plus shared resources. qdistro keeps `silo` to emphasize isolated
state and cross-contamination boundaries without implying a VM or container
implementation.

## Template

A versioned, cloneable software installation with no configuration and no
user data. A silo references a template plus its own config and state; the
analogy is the Qubes TemplateVM / AppVM split and the container image /
volume split. See [templates.md](templates.md).

Associated terms:

- **Generation** — one immutable, digest-identified version of a template.
  Bindings reference digests, never mutable names.
- **Candidate** — a generation built or cloned for an update, validated in
  isolation from real silo state. Candidates contain no secrets or user
  data; untrusted installer code runs only here.
- **Binding** — the per-silo TOML file mapping the silo to its active and
  rollback generations. Promotion is an atomic rewrite of this file.
- **Promotion** — flipping a silo's binding to a validated candidate at the
  next restart. A candidate that fails pre-promotion checks never becomes
  the user-visible launch target.
- **Templates are `derived`** (recipe-backed, reproducible, backed up as
  recipes) **or `artifact`** (golden images, irreproducible, backed up as
  bytes and sealed before promotion).

## Session

A dynamic Linux process and UI/execution context. A session begins when the
owner or a headless agent starts a set of processes. It changes over time with
user actions, workflow steps, process state, and attached or reserved
resources. A session is an eventful runtime context, not a static profile.

A session may reserve or attach resources such as a TTY, compositor, shell,
panel, clipboard surface, notification surface, sound device, browser profile,
directory mount, VM, credential, or silo. The session is where work happens;
the silo is the data/state that work is allowed to touch.

A session may run as:

- a full TTY session with its own compositor, shell, panel, clipboard surface,
  and notifications;
- a mixed desktop session where windows from multiple silos share one
  compositor and are separated by visual identity and brokered actions;
- a task-specific work session, such as a development session or commit
  session;
- a nested or VM-backed session, depending on the isolation tier;
- a headless session used by a workflow or agent without a normal human-facing
  shell.

Use **work session** when emphasizing a task-oriented human session, such as a
dev work session or commit work session. Use **desktop workspace** only for the
traditional Linux desktop concept: virtual desktops shown by a workspace pager.

## Attachment

A controlled relationship that lets a session use a resource. Attachment is
intentionally broad and still being refined. Examples include:

- showing a silo's windows in the session compositor;
- mounting selected directories from a silo into the session;
- granting a program temporary access to a credential or key from a silo;
- making a browser profile or logged-in web session available to the session.

Attachment should be explicit, visible, policy-governed, and revocable. The
exact attachment type matters because "can see windows," "can edit files," and
"can use commit keys" are different capabilities.

Attachment is not the same as ownership or permanent migration. A session can
receive a UI surface, a directory mount, a credential grant, or a one-shot
transfer without becoming the silo's home. Detach semantics, sharing rules, and
exclusive-use rules are per attachment type.

Attachment subtypes are UI, filesystem, credential, app-state, and one-shot
transfer. The broker records every attach/detach and the resulting capability
handle. See [attachments.md](attachments.md).

## Workflow

A policy-checkable orchestration plan that coordinates resources, actions, data
flows, approvals, cleanup, lineage, and audit records. Workflows can be
interactive (driven by the owner in a TTY session), GUI-assisted (agent clicks
through a browser login), or headless (a compositor or VM runs only to execute
the plan).

For example, a Claude Code authentication workflow might declare:

- intent: authenticate a dev tool to Anthropic;
- dependency: a browser resource already logged into Google;
- action: temporarily route the terminal's auth URL into that browser;
- transfer: return the generated code or callback result;
- cleanup: detach the browser resource and restore the normal link handler.

Workflow definitions live in the workflow model rather than being hardcoded
product flows. See [workflows.md](workflows.md).

Workflows can attach their own labels and security metadata in addition to
metadata inherited from resources. Labels may describe project, client, or
workflow intent; typed security fields describe sensitivity, authority,
contamination, conflict classes, and export state.

Workflow definitions have two layers: predictable Markdown for human/agent
review and a strict manifest the broker executes. They may include conditions,
agent-assisted GUI steps, data-flow declarations, approvals, cleanup, and
compensation. They are not static deployment bundles, package recipes, or shell
scripts.

## Data lineage

The record of where data, authority, UI actions, and generated outputs came
from. Lineage tracks source resources, attached authority resources,
declared data flows, transformations, workflow steps, approvals, and
destinations.

Lineage is used both for warnings and for audit. A paste from home into work is
one visible case, but the broader goal is scriptable provenance at useful
chokepoints: commits, file exports, uploads, workflow runs, paste operations,
and generated artifacts. Future versions may track finer-grained provenance
such as file lines and individual UI actions. For example: "this commit was
generated with Claude Code, authenticated through company Gmail, using source
from project X and commit authority Y."

qdistro uses W3C PROV vocabulary as a naming model: resources and artifacts are
Entities, workflow runs and transfers are Activities, and admin, apps, agents,
silos, and credential authorities are Agents. The authoritative store and
enforcement are qdistro's responsibility. See [lineage.md](lineage.md).

## Clipboard

The clipboard belongs to the session surface, not to a silo as
persistent state. A silo may receive a transient compatibility clipboard item
while content is being delivered into an app, but qdistro should clear that
state after transfer so clipboard contents do not travel with the silo when it
is reattached elsewhere.

Clipboard history, if any, is a session feature and must be treated as
sensitive data.
