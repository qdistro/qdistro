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
demand). The A-ii VM smoke is `spike/run-phase-a-ii-smoke.sh`. No acceptance
drivers yet (A-iii).

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

Not done in Phase 0, by design (kickoff `04`): no `QDISTRO_TIER3S` wiring into
`install-deps.sh` / `image/config.sh` (D1: optional, on demand), no launch path.
