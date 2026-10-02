# tier3s — gVisor (`runsc`) application-kernel tier (Experimental, dev only)

Results, evidence boundaries, deviations and the Phase A/B handoff:
**[`spike/RESULTS.md`](spike/RESULTS.md)** (self-contained). Provenance only,
not needed to read this tree: the qdistro planning tracker
`todo/paravirt/` (plan `03`, Track A; decisions D1 = (a), O1–O9; write-up
`05`; reviews).

Status: **Phase 0** (provision + prerequisite screen) and **Phase S**
(feasibility spike, `spike/README.md`): GO, with three conditions: a runsc
state root, an admin-delegated owning scope, a secctx-wrapped bridge client.
runsc is not in the image, kiwi config or any installer (provisioned on
demand, D1), and nothing in qdistro selects this tier automatically (O6:
explicit launch, no fallback). Dev profile only
(O4); `probe.sh` refuses on any other profile. No KVM claims (O5).

**Phase A** (headless launch path), milestone A-i: the lifecycle contract
**[`CONTRACT.md`](CONTRACT.md)** and the launch-path scripts below, tested on
the host against fakes (`tests/unit/test_tier3s_spawn.py`) with feasibility
evidence in `spike/logs/phase-a-20261002/`. Milestone A-ii wires it in:
the `tier3s` silo kind in the session manager (`CreateTier3sSilo`, the
`qdistro-tier3s-silo@.service` unit and its root launch helper), the broker's
rules-only `qdistro.tier3s.spawn:` prefix, and `install-session-manager.sh`
(scripts, units, seccomp profiles and tmpfiles; **not** runsc, which stays on
demand). The A-ii VM smoke is `spike/run-phase-a-ii-smoke.sh`. Milestone
A-iii adds the acceptance drivers (`tests/integration/vm/s120`–`s122`, run in
the qci VM lane), the owner answers O10 (the installer installs tier 3s only
with `QDISTRO_TIER3S=1`) and O11 (a session-manager stop tears every tier 3s
launch down), and the operator page below.

## Files

| File | Role |
|---|---|
| `RUNSC_RELEASE` | The pin: dated release, base URL, tarball sha512, per-file sha512 for `runsc` and every `gvisor-bin/` sidecar, expected `runsc --version` |
| `provision-runsc.sh` | Root, on demand. Installs the pinned bundle to `/usr/libexec/qdistro/runsc/{runsc,gvisor-bin/…}`, the wrapper to `/usr/libexec/qdistro/tier3s-runsc`, the pin to `/etc/qdistro/runsc-release`. Fails closed on any hash, file-set or version mismatch (version: exact text **and** exit 0); idempotent; `--offline` takes the tarball only from the cache. One exclusive lock (`/run/qdistro-runsc/provision.lock`) covers inspect → swap → verify → rollback; the cached tarball is verified and extracted from a private copy staged under a checked `/var/tmp`; parents of the install/stamp/lock paths must be root-owned and not group/other-writable |
| `tier3s-runsc` | The runtime path given to `podman --runtime`. `env -i` + constant flags `--ignore-cgroups --platform=systrap --oci-seccomp` + the fixed state root `--root=/run/qdistro-tier3s-runsc/<host uid>`, which it never creates and refuses when missing or loose (`CONTRACT.md` D-A1) |
| `probe.sh` | Prerequisite screen, one line per check, exits 1 naming the first missing prerequisite, 2 on a non-dev profile. Executes nothing from the install until stamp, trusted ancestors, exact file set and every sha512 pass; then runs `runsc --version` through the verified open fd (`/proc/self/fd/N`) |
| `CONTRACT.md` | Phase A lifecycle contract: state-root, scope, record/cleanup decisions; spawn order; session-manager interface |
| `spawn-tier3s.sh` | Root supervisor of one launch (dev only, root launcher, probe, broker gate, record, scope, podman as admin) |
| `qdistro-tier3s-scope` | Root-owned first process of the launch scope: verifies it, delegates it to admin selectively, execs podman as admin |
| `qdistro-tier3s-cleanup` | The only teardown path (`<token>`, `--unit`, `--reap-stale`); preserves the record on any failure |
| `tmpfiles/qdistro-tier3s.conf` | Creates the runsc state root and the control/per-launch parents |
| `seccomp/make-profiles.py`, `seccomp/<workload>.json` | Per-workload profiles derived from tier 2, with explicit decisions |
| `Containerfile.headless-smoke`, `headless-smoke.sh`, `make-tier3s-image.sh`, `configure-snapshot-repos.sh` | The headless smoke workload image on the snapshot pin |
| `cache-image-archive.sh` | Host side: build the workload image once in a dev VM and keep its OCI archive for the qci workers (`--key`, `--dir`) |
| `spike/` | Throwaway Phase S scripts, the evidence logs (`spike/logs/`), `RESULTS.md` |

## The pin

- Release **20260928.0** (x86_64), `runsc version release-20260928.0`, OCI spec 1.2.1.
- Why that date: it was the newest dated release in the upstream bucket
  (`gs://gvisor/releases/release/`) at kickoff on 2026-10-01, and
  `releases/release/latest/` pointed at the same tarball (identical sha512).
  It is a post-2026-07 multi-file release: `runsc` plus `gvisor-bin/`
  sidecars (`gvisor_sentry`, `checkpointgofer`, `gvisor-sentry-prewarmer`,
  `runsc-fd-parking`, `runsc-metric-server`). `containerd-shim-runsc-v1` is in
  the tarball but not installed (podman does not use it).
- Upstream publishes only a whole-tarball `.sha512`; the per-file hashes in
  the pin were computed by us from that verified tarball.
- Rotation: edit all of `RUNSC_RELEASE` together, re-run provision + probe in
  a dev VM, update this section (the qdistro tracker also logs it with the
  snapshot pin, `todo/monorepo-after/14`).

## Use (dev VM, as root)

Run both scripts from a root-owned checkout: as root they refuse one that
another uid could modify (script, pin, wrapper or any ancestor directory).

```sh
# host: cache the tarball once (the guest may have no egress)
#   ~/.cache/qdistro/runsc/20260928.0/gvisor.tar.zstd   (sha512 checked)
# guest:
tier3s/provision-runsc.sh --offline --cache-dir /var/cache/qdistro/runsc
tier3s/probe.sh --user admin
```

Tests: `python3 -m pytest tests/unit/test_tier3s_*.py` (real scripts,
synthetic bundles, no podman/runsc); `spike/mutate-guards.py` shows each
guard's test failing when the guard is broken.

Not wired, by design (D1: optional, on demand): runsc is in no image, kiwi
config or installer; `QDISTRO_TIER3S=1` gates only `install-session-manager.sh`
(owner O10), not `install-deps.sh` / `image/config.sh`.

## Operator page (Phase A: headless launch path)

### What it is

A tier-3s silo runs one headless workload under gVisor (`runsc`, platform
systrap) in rootless podman as admin (uid 1000, `--userns=keep-id`), with no
network (podman `--network=none` and runsc `network=none`), a read-only root,
no capabilities, no-new-privileges, `label=disable` and a per-workload seccomp
file. A root supervisor (`spawn-tier3s.sh`, run by
`qdistro-tier3s-silo@<name>.service`) gates the launch through the broker,
records it under `/run/qdistro-tier3s-ctl/<token>/`, and places every runtime
process in a root-created, admin-delegated scope
`qdistro-tier3s-<token>.scope`. `qdistro-tier3s-cleanup` is the only teardown
path. The lifecycle is in [`CONTRACT.md`](CONTRACT.md).

It is **Experimental and dev-profile only**. Nothing selects it automatically:
a silo is tier 3s only because it was created with `CreateTier3sSilo`, and a
refused launch never falls back to tier 2 or 3.

### Enabling it on a dev VM

Every command below runs **as root** in the guest; the steps that must run
as admin say so with `runuser -u admin --`. The checkout is staged root-owned
and world-readable at `/var/tmp/qdistro-src` (`chown -R root:root`,
`chmod 0755` on the top), so both root and admin can read it and the
provisioner accepts it; a checkout under `/root` would be unreadable to admin.

```sh
cd /var/tmp/qdistro-src
# 1. install the launch path (owner O10: opt-in)
QDISTRO_TIER3S=1 scripts/install/install-session-manager.sh "$PWD/session_manager"
# 2. provision the pinned runsc (D1: on demand; the host cache must hold the tarball)
tier3s/provision-runsc.sh --offline --cache-dir /var/cache/qdistro/runsc
/usr/lib/qdistro/tier3s/probe.sh --user admin          # must print RESULT PASS
# 3. the workload image, as admin: build it (needs registry access) ...
runuser -u admin -- bash /var/tmp/qdistro-src/tier3s/make-tier3s-image.sh headless-smoke
#    ... or load the archive tier3s/cache-image-archive.sh <vm> keeps on the
#    host, copied to a path admin can read (what the qci worker setup does)
runuser -u admin -- podman load -i /var/tmp/tier3s-headless-smoke.oci.tar
# 4. a broker rule (qdistro.tier3s.spawn: is rules-only: no rule = refused),
#    e.g. /etc/qdistro/rules.d/50-tier3s.yaml:
#    - decision: allow, match: {uid: 1000, action: "qdistro.tier3s.spawn:headless-smoke/qdistro-tier3s-smoke"}
# 5. the silo, as admin over D-Bus (the manager accepts admin, uid 1000, only).
#    StartSilo returns once the launch runs, or fails with the refusal; the
#    manager can take up to ~255 s in the worst case (CONTRACT §6), so give
#    busctl more than its default 25 s.
runuser -u admin -- busctl --system --timeout=300 call org.qdistro.SessionManager1 \
    /org/qdistro/SessionManager1 org.qdistro.SessionManager1 CreateTier3sSilo ssss smoke headless-smoke smoke none
runuser -u admin -- busctl --system --timeout=300 call org.qdistro.SessionManager1 \
    /org/qdistro/SessionManager1 org.qdistro.SessionManager1 StartSilo s smoke
```

The workload's output is in `journalctl _SYSTEMD_UNIT=qdistro-tier3s-<token>.scope`
(the token is the `LAUNCH_TOKEN=` line in the launch unit's journal).

### Flags and knobs

| Knob | Where | Effect |
|---|---|---|
| `QDISTRO_TIER3S=1` | `install-session-manager.sh` | installs the tier 3s files (CONTRACT §1). Unset/empty/`0`: nothing tier 3s; any other value: exit 2 |
| `CreateTier3sSilo(name, workload, template_silo, network)` | session manager | `network` must be `none`; `template_silo` names the binding to resolve (none = untemplated, image `localhost/qdistro/tier3s-<workload>:latest`) |
| silo row `launch.argv` | `/etc/qdistro/silos.yaml` (manager stopped) | the workload argv; empty = the workload default (`headless-smoke` → `qdistro-tier3s-smoke`; `--hold N` keeps it live) |
| `FreezeSilo`/`ResumeSilo` | session manager | refused for tier 3s silos |
| `TIER3S_DEBUG_LOG_DIR` | spawn env (dev diagnostics) | runsc `--debug --debug-log=<dir>/`, where seccomp denials show. Not reachable through the launch unit: the stanza's key set is fixed and the helper execs the spawn with `env -i` |
| `TIER3S_SECCOMP_PROFILE`, `TIER3S_ALLOW_PRIVESC`, `TIER3S_KEEP_CAPS`, `TIER3S_RUNTIME`, `TIER3S_CGROUP_PARENT` | spawn env | **refused** (exit 2): posture is not configurable per launch |
| `probe.sh --user <name>` | prerequisite screen | exit 0 PASS, 1 missing prerequisite, 2 non-dev profile |
| `qdistro-tier3s-cleanup <token> \| --unit <unit> \| --reap-stale` | root | the only teardown path; a failure exits non-zero and preserves the record. Every external call is bounded and supervised: its process group, and for admin's podman its own transient `qdistro-t3s-call-*.scope`, is SIGKILLed when the call ends or times out, and a timed-out query is never evidence (CONTRACT §4 "Locks and bounds"). Locks are per token. `--deadline <s>` (default 90) bounds a batch by the clock, plus the kill grace of the call in flight |
| `cache-image-archive.sh <vm> \| --key \| --dir` | host | builds the workload image once in a VM and keeps the OCI archive for the qci workers |

### Lifecycle guarantees that are tested (A-iii, qci VM lane)

`tests/integration/vm/phase7-tier3s-{headless,denied,sigkill-cleanup}.bats`
run the drivers `s120`–`s122` on fresh workers; the evidence table is
[`spike/logs/phase-a-20261002/INDEX.md`](spike/logs/phase-a-20261002/INDEX.md)
("Milestone A-iii"). In short: every runtime process class is in the owning
scope; normal exit, plain `podman stop`/`rm -f`, `StopSilo`, launcher SIGKILL,
a session-manager stop (O11), crash and restart each leave no process, scope,
`/run/qdistro-tier3s/<token>` or `/run/qdistro-tier3s-ctl/<token>`; a lost or
replaced runsc state root makes a stop or a cleanup fail visibly with the
record and scope preserved, and the restored root tears it down; restart
reconciliation reaps launches and labelled containers the manager does not
know; broker denial, a non-dev profile and a probe failure refuse with no
`podman run`, no activation record and no fallback, and the refused
`StartSilo` fails with the reason and leaves the silo Stopped (astra+fable
r1, run `a-r1-qci/`); an admin process inside the launch unit cannot
acknowledge a launch (`NotifyAccess=main`, s121 step 6 with a positive
control, astra+fable r2, run `a-r2-qci/`). The reaper's refusal of a stale unit name on a live
scope owned by another unit is host-tested (both the record and the label
case) and was reproduced once on a dev VM for the record case
(`a-r1-dev/dev-r4-repro.log`); it is not a DONE-bar driver.

### What is NOT claimed

- **Dev profile only.** There is no hardened or release launch path; the
  manager, the spawn and the probe all refuse it (O4).
- **No KVM claim** (O5): systrap only; nothing here measures or relies on
  gVisor's KVM platform.
- **No network** (O3): `network=none` is the only mode.
- **Not a containment proof.** The scope's `TasksMax`/`MemoryMax` are set by
  root and admin cannot raise them, but enforcement is Phase C. `--pids-limit`
  is parity with tier 2 only (runsc `--ignore-cgroups` does not enforce it).
- **Identity is hash-based, not name-based.** The Sentry and gofer are
  identified by their `/proc/<pid>/exe` sha512 against the pin; process names,
  the gVisor `dmesg` banner and `/proc/version` are corroboration only.
- **`fchmodat2` is denied by the pin**, not by choice: runsc 20260928.0's
  seccomp converter drops the name, so `chmod -h`/`lchmod` get EPERM.
- **No GUI, no bridge, no pod apps.** Phase B adds the waypipe bridge; the
  `qdistro-tier3s-app@` unit is not shipped and the spawn refuses a launch
  without a silo.
- **Under SIGKILL of the launch service** teardown is systemd killing the
  scope's cgroup, then verification; it is not a graceful `podman stop`.
- **A refused launch fails the start.** The launch unit is `Type=notify`
  with `NotifyAccess=main`: `StartSilo` returns once the spawn itself reports
  the launch recorded running, and a refusal (broker, profile, probe, image)
  fails it with the reason; the silo reads Stopped, so a retry after the fix
  is a real start. `StartSilo` blocks the manager's main loop, and every
  other manager D-Bus call, for the whole start path: seconds measured; the
  `systemctl start` alone is bounded at 135 s, the whole path at about
  165 s (start timeout) or 255 s (failed start) in the worst case
  (CONTRACT §6). A client with busctl's default 25 s timeout sees its own
  timeout for a slower start.
- **Cleanup bounds.** A batch (`--unit`, `--reap-stale`) ends at its
  deadline plus the kill grace; one token's teardown has per-call bounds
  only and can take a few minutes in the worst case, record preserved.
  Call supervision kills what stays in a call's process group or call
  scope; it does not cover a process root moves out of that cgroup.
- **Templated tier 3s silos** are exercised only through a hand-written
  binding fixture (s121); no tier 3s template recipe or promotion flow exists.

