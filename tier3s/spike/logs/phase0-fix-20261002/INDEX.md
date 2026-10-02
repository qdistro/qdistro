# Phase 0 fix evidence — 2026-10-02, VM tier3s-261002-075043-707631-1147 (dev profile)

Re-run of Phase 0 after the full-branch codex astra review (REVISE: the probe
executed the install before verifying it; no provisioning transaction lock;
guard tests that never reached their guards) and its re-reviews r1 (REVISE: a download was published into the cache through an unchecked path) r2 (REVISE: the probe's scratch-image import used a temporary directory under the caller's `$TMPDIR` and chmod'ed it as root) and r3 (REVISE: caller-`PATH` tools ran before the `PATH` pin and the wrapper found `env` through the caller's `PATH`; the stage parent's ancestors were unchecked). One run of
`tier3s/spike/run-phase0-fix.sh` (host) at commit `093f8a987`, every step a
`vmlog.sh` capture of `scripts/vm/vm-exec`: the log starts with the exact
command and ends with `### exit=<rc>`; every command ran inside the VM as
guest root unless it says `runuser -u admin`. Steps are **asserting**: they
source `tier3s/spike/phase0-fix-lib.sh`, print `CHECK <name>: OK|BAD (…)`,
and exit with their BAD count, so `### exit=0` here does mean every check
passed (unlike the observational Phase S scripts). The host served
`git archive HEAD` and the sha512-checked tarball on 127.0.0.1 (the guest's
10.0.2.2) only for the run; `00-stage.log` starts with the host sha256 of the
staged files and shows the guest's matching sha256.

Non-execution oracle: the probe's own `FAIL runsc_version: not executed:`
line **and** an `strace -f -e trace=execve` of the whole probe, counting
successful execs of a runsc-bundle path or of `/proc/self/fd/N`. Positive
controls: 04 and 08 see exactly one exec (`/proc/self/fd/N --version`); 12
shows the fake runsc writes its marker when executed directly.

| Log | What | Verdict |
|---|---|---|
| 00-stage.log | staged commit; the checkout dir as the VM spin left it (`/root/qdistro-src` owned by host uid 1007) is made root-owned, logged, because root-run tier3s scripts now refuse a checkout another uid owns (step 20); guest sha256 = host sha256 of the scripts/tests, cache tarball sha512 = pin, profile=dev, pytest + strace installed by zypper from the snapshot repo, logged reset of any prior install | info |
| 01-probe-before-provision.log | probe names `runsc` (not provisioned) first, exit 1; `runsc_version` not executed, 0 runsc execs | PASS (negative) |
| 02-provision-offline.log | offline install from the cache: lock `/run/qdistro-runsc/provision.lock` taken (dir root 0700), verified private copy of the tarball, 6 files, version, no leftovers | PASS |
| 03-provision-idempotent.log | second run: nothing to do | PASS |
| 04-probe-pass.log | full probe PASS, exit 0; `runsc --version` ran exactly once, as `/proc/self/fd/N` (the verified inode), per strace | PASS (positive control for the exec oracle) |
| 05-negative-runsc-removed.log | runsc moved aside: exit 1 naming `runsc`, not executed; moved back, PASS | PASS (negative) |
| 06-negative-tampered-cache.log | cache tarball +1 byte: (a) with a differing live install, provision exit 1 and the live tree untouched (sidecar still 0644); (b) with no install, exit 1 and nothing installed; cache restored | PASS (negative) |
| 07a-negative-sidecar-mode-then-repair.log | sidecar 0644 left by 06a: probe names the mode in `bundle`, not executed; provision repairs; PASS | PASS (negative + repair) |
| 07-negative-hardened-profile.log | profile=release: REFUSE, exit 2, no runsc exec; profile restored | PASS (negative) |
| 07b-negative-tampered-install-then-repair.log | sidecar +1 byte + extra symlink: probe names the symlink (file-set branch), not executed; repair, no leftovers, PASS | PASS (negative + repair) |
| 07c-negative-root-refusals.log | as real root, exact messages, exit 1: `QDISTRO_RUNSC_PREFIX`, `--pin` with a **readable** alternate pin, `QDISTRO_RUNSC_FAIL_AFTER_SWAP`, `QDISTRO_RUNSC_PAUSE_AFTER_SWAP`; the probe refuses `QDISTRO_PROBE_PIN` without a test root (exit 2); install sha512 unchanged, no test prefix created | PASS (negative) |
| 11-negative-byte-only-sidecar-then-repair.log | one byte of `runsc-fd-parking` changed in place: size/mode/owner/inode identical, sha differs; probe fails in the **per-file hash loop** (`FAIL bundle: sha512:gvisor-bin/runsc-fd-parking`, no file-set message), not executed; repair, PASS | PASS (negative + repair) |
| 12-negative-replaced-runsc-marker.log | the review's scenario: runsc replaced by a root:root 0755 script that touches a marker and prints the pinned version, stamp intact: `FAIL bundle: sha512:runsc`, not executed, 0 execs, **marker absent**; control: executing the fake directly does create the marker; repair, PASS | PASS (negative) |
| 13-negative-foreign-owner-runsc.log | real runsc bytes, owner admin: file set names `f 755 admin:root runsc`, not executed, 0 execs; repair, PASS | PASS (negative) |
| 14-negative-symlinked-runsc-dir.log | `RUNSC_DIR` a symlink to a perfect bundle elsewhere: `FAIL install_path: … is a symlink`, not executed, 0 execs; restored, PASS | PASS (negative) |
| 15-negative-writable-ancestor.log | `/usr/libexec/qdistro` 0775: `FAIL install_path`, no content read (`sha512 not checked`, wrapper not compared, podman not given the wrapper), not executed; provision refuses (`untrusted path`); restored, PASS | PASS (negative) |
| 16-concurrent-provisions.log | two real-root provisions on a damaged install: A holds the lock and repairs; B logs `waiting for the provisioning lock`, then sees `already installed`; both rc 0; final state PASS, no leftovers | PASS |
| 17-stage-parent.log | hostile `TMPDIR` (0777, not sticky): strace shows the stage made under `/var/tmp`, nothing under TMPDIR; `/var/tmp` briefly 0777 (restored 1777 by trap): provision refuses before staging; `/var` briefly 0775 while `/var/tmp` stays root 1777: provision refuses (`untrusted path: /var …`) before staging; live install untouched throughout; repair, PASS | PASS (negative) |
| 18-cache-trust.log | real root, online: (a) cache under sticky `/var/tmp` (1777) refused **before any download** (`untrusted path: /var/tmp …`), cache empty, live install untouched; (b) cache release dir a symlink into an attacker dir that links a root 0600 sentinel: refused before download, attacker dir and sentinel (mode, owner, size, sha256) unchanged; (c) positive control: fresh root-owned cache, real download from the pinned URL, sha512 = pin, published 0644 root with no temporaries left, installed, probe PASS | PASS (negative + positive) |
| 19-probe-scratch-image.log | probe as real root with admin's scratch image removed (so the import branch runs), a hostile `TMPDIR` (0777, not sticky) and a root 0600 sentinel: PASS with the real podman import; strace of mkdir/chmod/fchmod/fchmodat/fchmodat2 across the whole probe shows **no** call naming TMPDIR; TMPDIR empty, sentinel unchanged, scratch image present afterwards | PASS (negative + positive) |
| 20-untrusted-checkout.log | as root, both scripts run from an admin-owned copy of `tier3s/`: provision exits 1 and the probe prints `REFUSE checkout` (exit 2), exact messages naming the uid-1000 script; the install is unchanged; from the root-owned checkout the probe then PASSes | PASS (negative) |
| 21-caller-path-shadow.log | as real root with marker-writing, delegating shadows of 38 tools (dirname, id, stat, sed, env, tar, curl, runuser, podman, …) first on `PATH`: the full real probe PASSes, provision runs (idempotent) and the wrapper prints the pinned runsc version, and **no** shadow ran; positive control: a shadow called directly does record | PASS (negative + control) |
| 08-probe-pass-after-negatives.log | idempotent provision + probe PASS, exactly one exec via the fd, no leftovers | PASS |
| 09-unit-tests.log | `tests/unit/test_tier3s_{probe,provision}.py` as root: 34 passed, 19 skipped (the prefix hook is refused for root by design; the root-only tests — foreign owner, `--pin`/prefix/test-hook refusals with euid 0 — run for real); as admin on a copy: 52 passed, 1 skipped (foreign owner needs root). The one warning is the repo's `qt_api` pytest option without pytest-qt | PASS |
| 10-mutation-harness.log | `tier3s/spike/mutate-guards.py` as admin: 29 mutations of the real probe/provision/wrapper, each caught by every named test, files restored byte-identical (sha256), baseline and after-restore green; as root: V2–V4 caught with real euid 0 | PASS |

Order: logs ran 00–07c, 11–21, 08, 09, 10 (numbers keep the Phase 0 names
for the steps that repeat `phase0-20261001/`).

Superseded: the runs committed at `88e4d84a2` (staged `bf2b7a6f9`, reviewed
in astra fix r1), `1f6311300` (staged `88425f4ea`, r2) and `354895156`
(staged `f3ecdf9aa`, r3) are in history; this run replaces them after the
`PATH`-bootstrap and stage-ancestor fixes.
Not kept (scratch, outside the tree): earlier runs that differed only by
driver/check bugs fixed in the branch history (a relative lib path; a
stage-dir count that also matched `tree/`; a mutation anchor left stale by
the `trusted_chain` signature change, which the harness reported as a
HARNESS ERROR), and runs superseded by later hardening before they were
committed (stage parent/untrusted path; the r2 scratch-image fix before the
checkout check). One of them was disturbed by an edit to the running driver (bash
reads scripts incrementally); it stopped at a parse error after its last
step, executed nothing extra, and was discarded.

Not shown here: SIGKILL/power-loss recovery of the provisioner, and the
probe's fd-swap windows under real root (the unit tests drive those windows
with a TEST-only pause hook; the hook is refused outside a test root).
