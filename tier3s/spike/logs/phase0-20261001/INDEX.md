# Phase 0 evidence — 2026-10-01, VM tier3s-261001-221428-2070491-12685 (dev profile)

Logs 00–09 except 04a were re-run with the sol-r1-revised scripts (commit
noted in the review brief). All transcripts are `scripts/vm/vm-exec` captures run from the host; every
command executed inside the VM. Source staged from branch claude/tier3s via a
host HTTP server (git archive) to /root/qdistro-src; the runsc tarball was
downloaded once on the host to ~/.cache/qdistro/runsc/20260928.0/ (sha512
checked there against upstream's .sha512) and re-checked in the guest.

| Log | What | Verdict |
|---|---|---|
| 00-stage.log | stage src (guest sha256 of scripts = host HEAD), guest tarball sha512, profile=dev, prior install removed | info |
| 01-probe-before-provision.log | probe names `runsc` (not provisioned) first, exit 1 | PASS (negative) |
| 02-provision-offline.log | offline provision from cache, 6 files verified, version matches | PASS |
| 03-provision-idempotent.log | second run: nothing to do | PASS |
| 04a-probe-rootfs-label-dropped.log | (historical, from the FIRST probe version ca63337ad) first probe build: `--rootfs /` create drops `label=disable` (podman 6.0.2) — probe fixed to an image-backed create | finding |
| 04-probe-pass.log | full probe PASS, exit 0 | PASS |
| 05-negative-runsc-removed.log | runsc binary moved aside: probe exit 1 naming `runsc` | PASS (negative) |
| 06-negative-tampered-cache.log | tarball +1 byte: provision exit 1, nothing installed | PASS (negative) |
| 07-negative-hardened-profile.log | profile=release: probe REFUSE, exit 2 | PASS (negative) |
| 07b-negative-tampered-install-then-repair.log | installed sidecar +1 byte and an extra symlink: probe names `bundle`; provision repairs via `mv -T --exchange` swap, no leftovers, probe PASS | PASS (negative + repair) |
| 07c-negative-prefix-hook-refused-for-root.log | root with QDISTRO_RUNSC_PREFIX, or with --pin: refused, exit 1 | PASS (negative) |
| 08-probe-pass-after-negatives.log | restored state: idempotent provision + probe PASS | PASS |
| 09-unit-tests.log | tests/unit/test_tier3s_{provision,probe}.py: 12 passed (pytest installed in VM via zypper) | PASS |

Host deviation (disclosed): while computing per-file hashes on the host the
extracted `runsc --version` was executed once on the host (prints version, no
sandbox). No podman/runsc sandbox step ran on the host.
