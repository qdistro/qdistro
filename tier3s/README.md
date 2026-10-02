# tier3s — gVisor (`runsc`) application-kernel tier (Experimental, dev only)

Results, evidence boundaries, deviations and the Phase A/B handoff:
**[`spike/RESULTS.md`](spike/RESULTS.md)** (self-contained). Provenance only,
not needed to read this tree: the qdistro planning tracker
`todo/paravirt/` (plan `03`, Track A; decisions D1 = (a), O1–O9; write-up
`05`; reviews).

Status: **Phase 0** (provision + prerequisite screen) and **Phase S**
(feasibility spike, `spike/README.md`): GO, with three conditions: a runsc
state root, an admin-delegated owning scope, a secctx-wrapped bridge client. Nothing here is wired
into the image, kiwi config or any installer, and nothing in qdistro selects
this tier automatically (O6: explicit launch, no fallback). Dev profile only
(O4); `probe.sh` refuses on any other profile. No KVM claims (O5).

## Files

| File | Role |
|---|---|
| `RUNSC_RELEASE` | The pin: dated release, base URL, tarball sha512, per-file sha512 for `runsc` and every `gvisor-bin/` sidecar, expected `runsc --version` |
| `provision-runsc.sh` | Root, on demand. Installs the pinned bundle to `/usr/libexec/qdistro/runsc/{runsc,gvisor-bin/…}`, the wrapper to `/usr/libexec/qdistro/tier3s-runsc`, the pin to `/etc/qdistro/runsc-release`. Fails closed on any hash, file-set or version mismatch (version: exact text **and** exit 0); idempotent; `--offline` takes the tarball only from the cache. One exclusive lock (`/run/qdistro-runsc/provision.lock`) covers inspect → swap → verify → rollback; the cached tarball is verified and extracted from a private copy staged under a checked `/var/tmp`; parents of the install/stamp/lock paths must be root-owned and not group/other-writable |
| `tier3s-runsc` | The runtime path given to `podman --runtime`. `env -i` + constant flags `--ignore-cgroups --platform=systrap --oci-seccomp` |
| `probe.sh` | Prerequisite screen, one line per check, exits 1 naming the first missing prerequisite, 2 on a non-dev profile. Executes nothing from the install until stamp, trusted ancestors, exact file set and every sha512 pass; then runs `runsc --version` through the verified open fd (`/proc/self/fd/N`) |
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
