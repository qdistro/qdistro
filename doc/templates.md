# Templates

A template is a versioned, cloneable software installation with no
configuration and no user data. A silo references a template plus its own
config and persistent state. This is qdistro's equivalent of the Qubes
TemplateVM / AppVM split and the container image / volume split: software is
validated and promoted as a unit; state is snapshotted and rolled back as a
separate unit.

The split exists because software and state cannot be safely validated by the
same mechanism. Software candidates can be built and tested in empty,
disposable environments with no risk to real data. State can only be
protected by snapshots, migration policy, and honest claims about what is and
is not reversible.

## The promise — and its exact boundary

The load-bearing guarantee is narrow and mechanical:

> **A candidate that fails pre-promotion checks never becomes the
> user-visible launch target. Failed pre-promotion checks require no recovery
> of the active silo.**

Worked example: MS Office 2019 in a wine VM. A vendor update removes the Save
function. The update runs in a candidate clone against a disposable overlay
and a dedicated test account; the save-reopen validation fails; the failure
is recorded; the binding still points at the old generation. The desktop icon
keeps opening the version that can save.

What is **not** promised:

- Validation completeness. Save-reopen passing does not prove macros,
  add-ins, or cloud paths work.
- Remote rollback. Cloud accounts, licenses, and synced data are outside
  qdistro's reach (see [silos.md](silos.md) data-rollback rules).
- Reversibility of first-launch migrations beyond the local state snapshot
  taken at activation.

A candidate must be mechanically unable to launch against the real silo
state before promotion. This is enforced by the binding-file resolution path
(below), not by convention.

## Template classes

```toml
[template]
class = "derived"   # or "artifact"
```

- **derived** — recipe-backed: a Nix closure, a Containerfile build, a
  lockfile-pinned toolchain. Reproducible from inputs. Backup unit is the
  *recipe* (lockfiles, flake refs, Containerfile hashes), kilobytes not
  gigabytes. Eligible for opportunistic rebuild-from-scratch freshness
  checks.
- **artifact** — golden image: a hand-installed VM (wine + MS Office), a
  vendor appliance. Not reproducible. Backup unit is the *bytes*, with the
  same care as user state. Promotion requires a **seal step**: secret scan,
  verify no user documents/profiles present, record image digest and
  software inventory, quiesced/offline snapshot evidence, and a
  license-binding note (machine-bound artifacts require manual delete).

## Boundary classes

Not every workload can keep software and state separate. The template
manifest declares how well the boundary holds, and the claims scale down
honestly with it:

```toml
[template.state_boundary]
class = "recipe-derived-toolchain"  # see table
enforced = "true"                   # true | partial | false
```

| Workload | Class | `enforced` |
|---|---|---|
| Python/dev toolchain | `recipe-derived-toolchain` | true |
| LibreOffice | `mostly-split-desktop-app` | true (base) / partial (profile) |
| Firefox | `split-app-profile` | partial |
| VS Code / JetBrains | `stateful-plugin-host` | false unless plugins pinned into recipe |
| wine + Office VM | `artifact-vm-with-state-overlay` | true only with sealed base + overlay |

`enforced = false` workloads do not get the template promotion path. They
use the in-place managed/observed update path in [silos.md](silos.md), with
snapshots, drift reporting, and narrower claims. Templates are strong
infrastructure for dev toolchains, containers, VMs, and cleanly packaged
desktop apps — not a universal update model, and the docs must not pretend
otherwise.

Software a user installs into silo state (`pip install` into home, vendored
code, IDE plugins) is **stateful software drift**: allowed, recorded,
rolled back with state snapshots, never covered by template validation. A
silo with executable drift reports degraded reproducibility. Observed
installs are offered for pinning into the template recipe ("your session
fetched 3 artifacts not in the template — pin them?").

## Promotion pipeline

```
build → probes → validations → audit gate → parked-ready → flip at restart
```

1. **Build.** A candidate generation is built from the recipe (derived) or
   by cloning the active generation and running the vendor updater inside it
   (artifact). The build environment contains no secrets, no credentials,
   and no user data — untrusted installer code (npm postinstall, vendor
   updaters) executes only in this empty room. It can poison the candidate
   — the audit gate's job — but it cannot reach user documents, live
   sessions, or registry tokens, because none exist in the build
   environment. Credential-bearing deputies (the recording proxy) stay
   outside the candidate, scoped and audited.
2. **Probes.** Hermetic checks against the candidate in a minimal disposable
   runtime (not just a temp dir: GUI apps need D-Bus, fonts, GL, portals,
   a fake home). Process starts, window appears, basic smoke.
3. **Validations.** Agent-driven functional checks (save-reopen,
   profile-migration dry run against a *cloned* state copy) — see
   [silos.md](silos.md) probe/validation split. Account-bearing validations
   use dedicated test accounts, never live accounts: cloned profiles that
   refresh rotated tokens kill the real session. Real accounts get passive
   read-only liveness checks only.
4. **Audit gate.** Required for untrusted sources (GitHub installs, npm
   postinstall, unsigned plugins — the `agent_review` decision record in
   [silos.md](silos.md)); log-only for trusted package repos.
5. **Parked-ready.** The validated candidate waits; nothing is mutated.
6. **Flip.** The binding file is atomically rewritten; the silo resolves the
   new generation at its next natural restart. The user never waits for an
   upgrade.

Failure at any step — probe, validation, audit, permission, timeout — lands
in the same terminal handling: candidate payload discarded or parked per
retention policy, evidence preserved, report in the admin queue, active
binding untouched.

## First activation

The one step that cannot be data-free is the first launch of real state
under a promoted generation (profile schema migration). Mitigations, in
order:

- A state snapshot is taken at **first activation**, not just at update
  time, so the rollback unit covers the migration: old generation +
  pre-migration snapshot = the full local undo.
  - **Snapshot storage (decision D9,
    `todo/decisions/v1-release-scope-2026-06-12.md`):** the pre-migration
    snapshot is a **sibling directory** of the state subvolume, not a
    `.snapshots` child of it. A child snapshot directory cannot survive the
    `RENAME_EXCHANGE` the promotion flip uses to swap state generations
    atomically — the exchange would carry the child away with the old
    generation. A sibling subvolume is outside the exchanged path, so it
    remains a stable rollback target. This is the committed v1 mechanism; the
    earlier snapper-first instruction is retired (qdistro drives the btrfs
    subvolume snapshot directly rather than going through Snapper for the
    template state path).
- First launch runs under the narrowest declared network mode the app is
  expected to survive (migrations can write remote state; sync clients can
  propagate corruption to the cloud before local validation fails):

```toml
[silo.activation_migration]
expected = "true"                 # true | false | unknown
remote_writes = "forbidden"       # forbidden | possible | expected | unknown
downgrade_supported = "false"
first_launch_network = "allowlisted"   # disabled | allowlisted | normal
allowlist = ["https://login.example.com"]  # purpose-tagged origins
offline_tolerant = "false"
fallback_on_offline_failure = "needs_user_action"
```

Defaults: `offline_tolerant = true` apps launch with network disabled;
license/SSO-bound apps get an allowlist with unknown egress denied; an
account-bound app with no allowlist parks in `needs_user_action` rather
than producing a spurious `failed`. `normal` requires explicit policy
because it abandons migration containment.

## On-disk model — files, not a control plane

There is no registry, no API server, no reconciler, no watch loop. Plain
TOML in known directories, read synchronously by the broker and one-shot
systemd services; decisions append to the existing audit journal.

```text
/etc/qdistro/templates/<template>.toml               authored policy
/etc/qdistro/template-retention.toml                 global retention
/var/lib/qdistro/templates/<t>/generations/<g>/manifest.toml
/var/lib/qdistro/templates/<t>/generations/<g>/evidence/
/var/lib/qdistro/templates/<t>/candidates/<run-id>/
/var/lib/qdistro/bindings/<silo>.toml                active + rollback generations
/var/lib/qdistro/pins/<template>/<gen>/*.toml        pin receipts
/var/lib/qdistro/identity/<silo>/<app>.toml          generation-relative selectors
```

- **Binding** (`bindings/<silo>.toml`): `silo`, `template`,
  `active_generation`, `previous_generations`, `backend`, `state_path`,
  `activation_policy`, `identity_revision`. Promotion is an atomic
  write-rename of this file. The silo's `qdistro-silo@.service` resolves it
  at start. Generations are referenced by **immutable digest** (podman image
  digest, qcow2/subvolume content digest, Nix store path) — never a mutable
  tag or name.
- **Pin receipts** (`pins/`): one file per reason — `active`,
  `rollback-window`, `pre-migration-snapshot`, `in-flight-workflow`,
  `manual-hold` — with `owner_type`, `owner_id`, `reason`, `expires_at`.
  GC refuses to delete any payload with an unexpired receipt. GC is
  security-critical: collecting a generation deletes someone's rollback
  target.
- **Evidence outlives payload.** Candidate images and layers may be GC'd;
  the recipe ref, lock hash, build command, builder identity, network mode,
  fetched-artifact manifest, candidate digest, validation report, and
  promotion/denial decision remain in `evidence/` and the audit journal.
- **Retention defaults:** keep 3 promoted generations (2 for VM artifacts),
  failed candidate payloads 7 days, build logs 180 days, audit evidence 3
  years, security-decision evidence indefinitely. License-bound artifacts
  require manual delete.

systemd surface:

```text
qdistro-template-build@<template>.service     candidate build run
qdistro-template-validate@<run-id>.service    probes + validations
qdistro-template-promote@<silo>.service       gated binding flip + selector revalidation
qdistro-template-gc.timer                     unpinned-payload cleanup
qdistro-template-freshness.timer              opportunistic rebuild checks
```

Files stop scaling when there are concurrent third-party controllers,
cross-machine convergence, or thousands of objects with transactional
multi-resource updates. A one-owner machine has none of these; if that
changes, the migration target is a SQLite-backed object store, not an API
server.

## App identity across promotions

The password manager and browser bridge verify app identity by executable
path, SELinux label, and cgroup ([password-manager.md](password-manager.md)).
A promotion changes executable provenance, so selectors are
**generation-relative**, never absolute host paths:

```toml
[identity.executable]
path_in_template = "/usr/bin/firefox"
expected_package = "firefox"
selinux_type = "qdistro_firefox_t"
cgroup_unit_pattern = "qdistro-silo@firefox-work.service"
```

`qdistro-template-promote` re-resolves the selector against the candidate,
records the new executable digest, and bumps `identity_revision`. Routine
updates (same package, same label class) revalidate automatically; identity
*class* changes (different package name, SELinux type change, binary
replaced by a wrapper from an untrusted path) fail closed — the binding is
not flipped, or secret delivery is suspended pending admin re-approval. The
`TemplateBindingActivated` audit event is a cache hint for the consuming
daemons, never the correctness mechanism.

## Network modes and the recording proxy

Template builds and silo runs declare a network mode:

- `record` — egress only through the content-addressed recording proxy,
  which fetches upstream and stores `(url, hash, artifact)`. The proxy
  operates at the package-registry protocol layer (pip/npm registry mirrors)
  — no TLS interception, no command interception.
- `replay` — the proxy serves only from the store; a cache miss fails the
  run loudly. Replay is enforced by the run's network namespace ("no egress
  except declared replay stores and declared live endpoints"), not by the
  proxy alone — postinstall scripts fetch arbitrary HTTPS and must hit the
  namespace wall.

Artifact fetches are replayable. Live API traffic (OAuth, dynamic APIs) is
declared separately per workflow, audited, and explicitly **not**
replayable; a workflow using live endpoints is flagged as not fully
reproducible.

Private-registry credentials live in the proxy, which authenticates
upstream on the silo's behalf — silos never hold registry tokens. The proxy
is therefore a credential-bearing deputy and is treated like the password
manager, not like a cache: per-workflow scoped fetch capability, every
private fetch audited, no ambient access.

Run mode is a property of the **run**, not the actor. Attended runs default
to `record`; unattended runs default to `replay` with capability pre-flight
at step 0. Same rule set for human and agent; presence changes defaults and
stop behavior only, and any divergence is visible in the run record.

## Freshness

Derived templates are rebuilt from scratch opportunistically, not on a
wall-clock schedule — a laptop suspends, runs on battery, and sits behind
captive portals. Conditions: AC power, idle ≥ 20 min, trusted non-metered
network, normal thermals, ≥ 20 GiB free, advisory night window. A missed
window is **not** a failure; status surfaces staleness instead: "freshness
check last succeeded 9 days ago", warn after 7 days, needs-attention after
30.

## Backends

All backends implement the same contract: immutable generation digests, a
binding file, candidate isolation from real state, and the promotion
pipeline.

- **podman-image** — the reference backend. Image-per-workload from
  [containers.md](containers.md); state in named volumes / host state dirs
  outside the image; binding points at an image digest, never a tag. First
  workload: `tier2-dev` (cleanest boundary, exercises the replay path).
- **nix-closure** — derived dev toolchains via Nix-on-Tumbleweed.
  `/nix/store` coexists with the mutable zypper root; no NixOS, no
  immutable-root requirement. Generations are store paths with GC roots as
  pins.
- **vm-artifact** — golden VM base disks (btrfs subvolume or qcow2) with
  per-silo overlay/state disks. Requires the seal step. The wine/Office
  case.
- **host packages** (tier 0/1 silos using zypper-installed apps) do *not*
  get per-app templates — there is one `/usr`, and Snapper rollback is
  whole-root. Host-tier workloads share one coarse "template": the root
  snapshot pair around a zypper transaction, validated once for all of
  them. Apps that need real per-workload template semantics belong in
  tier 2.

## Status

This document is the specification; code follows it. The first slice under
implementation (tracked in `todo/fableplan/`) is the podman-image backend
with the `tier2-dev` workload: build/validate/promote one-shot services,
digest-resolved bindings in `spawn-tier2`, pin-receipt GC, the
opportunistic freshness timer, and audit events, with integration tests
covering digest pinning, failed-validation non-flips, and GC pin safety.
Promotion is **manual** in this slice: candidates park with a validation
summary and the owner flips. First-activation state snapshots and
activation network policy, the recording proxy, the nix-closure and
vm-artifact backends, automated promotion policy, and the GUI validation
framework follow the same contract and are deferred to later slices. The
OAuth grant vocabulary for auth-bearing **workflows** is schema-only — see
[auth-grants.md](auth-grants.md); validations never drive live auth.
