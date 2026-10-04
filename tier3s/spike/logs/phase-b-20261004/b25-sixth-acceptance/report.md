# qdistro CI report: bats-20261004T171430Z-298415

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T17:14:30Z`
- **finished_utc**: `2026-10-04T17:48:24Z`
- **exit_code**: `35`
- **exit_class**: `bats`
- **workspace**: `/var/tmp/t3s-qci-b-4729fce58`
- **command**: `/var/tmp/t3s-qci-b-4729fce58/ci/bin/qci bats --file tests/integration/vm/phase7-tier3s-headless.bats --file tests/integration/vm/phase7-tier3s-denied.bats --file tests/integration/vm/phase7-tier3s-sigkill-cleanup.bats --file tests/integration/vm/phase7-tier3s-waypipe.bats --file tests/integration/vm/phase7-tier3s-app.bats --file tests/integration/vm/phase7-tier3s-lifecycle.bats --file tests/integration/vm/phase7-tier3s-chrome-secctx.bats --file tests/integration/vm/phase7-tier3s-clipboard-gate.bats --file tests/integration/vm/phase7-tier3s-lineage.bats --file tests/integration/vm/phase7-tier3s-hostile-stream.bats`
- **results**: fail=3, pass=7, skip=3
- **actionable failures**: 3 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 10 | 7 | 3 | 0 | 0 |
| vm | 3 | 0 | 0 | 0 | 3 |

## Failures and blocked work
### FAIL: bats / phase7-tier3s-chrome-secctx.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-chrome-secctx.bats.log)
- source: [ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-chrome-secctx.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-chrome-secctx.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-chrome-secctx.bats.scratch/s126.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-chrome-secctx.bats.scratch/s126.log)
- notes: VM=qci-bats-phase7-tier3s-chrome-secctx-261004-191713-308075-10171 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# [vm-exec] ERROR: command exceeded 1800s (elapsed 1800s); attempting identity-checked cleanup of guest PID 3800: cd /var/tmp/t3s-dl && for f in tier3s-guest-lib.sh s126-tier3s-chrome-secctx.sh; do curl -fsS -o $f http://10.0.2.2:36361/$f || exit 97; done && bash s126-tier3s-chrome-secctx.sh
#   `assert_success' failed
# --- command failed (exit=124) ---
# [vm-exec] ERROR: command exceeded 1800s (elapsed 1800s); attempting identity-checked cleanup of guest PID 3800: cd /var/tmp/t3s-dl && for f in tier3s-guest-lib.sh s126-tier3s-chrome-secctx.sh; do curl -fsS -o $f http://10.0.2.2:36361/$f || exit 97; done && bash s126-tier3s-chrome-secctx.sh
```

### FAIL: bats / phase7-tier3s-clipboard-gate.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-clipboard-gate.bats.log)
- source: [ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log)
- notes: VM=qci-bats-phase7-tier3s-clipboard-gate-261004-191713-308095-3559 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# PASS: qdshell denied a png-only tier3s offer before the broker (1)
# FAIL: no selection_set_source_peer_identity for pid 6201
# FAIL: no live cross-silo allow for s127a->s127b
# FAIL: live rule-driven allow: cross-silo s127a -> s127b (source-peer relay): got '0', want '1'
# PASS: cold-verify bound offers denied first (the allow was earned) (1)
# PASS: audit: denied transfer row(s) recorded (1)
# PASS: locker idle-timeout restored (ABSENT)
# [s127] 97 passes, 3 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# PASS: qdshell denied a png-only tier3s offer before the broker (1)
# FAIL: no selection_set_source_peer_identity for pid 6201
# FAIL: no live cross-silo allow for s127a->s127b
# FAIL: live rule-driven allow: cross-silo s127a -> s127b (source-peer relay): got '0', want '1'
# PASS: cold-verify bound offers denied first (the allow was earned) (1)
# PASS: audit: denied transfer row(s) recorded (1)
# PASS: locker idle-timeout restored (ABSENT)
# [s127] 97 passes, 3 failures
```

### FAIL: bats / phase7-tier3s-hostile-stream.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-hostile-stream.bats.log)
- source: [ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-hostile-stream.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-hostile-stream.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-hostile-stream.bats.scratch/s129.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T171430Z-298415/bats/phase7-tier3s-hostile-stream.bats.scratch/s129.log)
- notes: VM=qci-bats-phase7-tier3s-hostile-stream-261004-191713-308131-18354 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# PASS: single-attach: reconnect to A's consumed link.sock is refused (refused)
# [s129] 78 passes, 0 failures
#   `assert_output_contains "PASS: hose wrote onto bridge sockets (attributed ends only)"' failed
# PASS: single-attach: reconnect to A's consumed link.sock is refused (refused)
# [s129] 78 passes, 0 failures
```

## Skips
- lifecycle / qci-bats-phase7-tier3s-chrome-secctx-261004-191713-308075-10171: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-clipboard-gate-261004-191713-308095-3559: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-hostile-stream-261004-191713-308131-18354: failed VM powered off (disk preserved) for debugging; virsh start to inspect

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 155 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pass | bats | phase7-tier3s-app.bats | integration | bats | 0 | [log](bats/phase7-tier3s-app.bats.log) | VM=qci-bats-phase7-tier3s-app-261004-191713-308060-7119 |
| fail | bats | phase7-tier3s-chrome-secctx.bats | integration | bats | 35 | [log](bats/phase7-tier3s-chrome-secctx.bats.log) | VM=qci-bats-phase7-tier3s-chrome-secctx-261004-191713-308075-10171 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-chrome-secctx-261004-191713-308075-10171 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| fail | bats | phase7-tier3s-clipboard-gate.bats | integration | bats | 35 | [log](bats/phase7-tier3s-clipboard-gate.bats.log) | VM=qci-bats-phase7-tier3s-clipboard-gate-261004-191713-308095-3559 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-clipboard-gate-261004-191713-308095-3559 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-denied.bats | integration | bats | 0 | [log](bats/phase7-tier3s-denied.bats.log) | VM=qci-bats-phase7-tier3s-denied-261004-191713-308028-28921 |
| pass | bats | phase7-tier3s-headless.bats | integration | bats | 0 | [log](bats/phase7-tier3s-headless.bats.log) | VM=qci-bats-phase7-tier3s-headless-261004-191713-307999-6510 |
| fail | bats | phase7-tier3s-hostile-stream.bats | integration | bats | 35 | [log](bats/phase7-tier3s-hostile-stream.bats.log) | VM=qci-bats-phase7-tier3s-hostile-stream-261004-191713-308131-18354 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-hostile-stream-261004-191713-308131-18354 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-lifecycle.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lifecycle.bats.log) | VM=qci-bats-phase7-tier3s-lifecycle-261004-191713-308085-20167 |
| pass | bats | phase7-tier3s-lineage.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lineage.bats.log) | VM=qci-bats-phase7-tier3s-lineage-261004-191713-308109-16600 |
| pass | bats | phase7-tier3s-sigkill-cleanup.bats | integration | bats | 0 | [log](bats/phase7-tier3s-sigkill-cleanup.bats.log) | VM=qci-bats-phase7-tier3s-sigkill-cleanup-261004-191713-308033-26928 |
| pass | bats | phase7-tier3s-waypipe.bats | integration | bats | 0 | [log](bats/phase7-tier3s-waypipe.bats.log) | VM=qci-bats-phase7-tier3s-waypipe-261004-191713-308041-14244 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-b | bb0b25c6b | 0 | [status](repos/qdistro.status.txt) | `/var/tmp/t3s-qci-b-4729fce58` |

## Artifact Index
- [agent-notes/](agent-notes)
- [bats/](bats)
- [exit-code.txt](exit-code.txt)
- [flake.tsv](flake.tsv)
- [gui/](gui)
- [host/](host)
- [host-load.tsv](host-load.tsv)
- [journals/](journals)
- [manifest.txt](manifest.txt)
- [preflight/](preflight)
- [repo-state.tsv](repo-state.tsv)
- [reports/](reports)
- [repos/](repos)
- [results.d/](results.d)
- [results.tsv](results.tsv)
- [scenario-attempts.tsv](scenario-attempts.tsv)
- [screenshots/](screenshots)
- [selftest/](selftest)
- [timings.d/](timings.d)
- [timings.tsv](timings.tsv)
- [vm/](vm)
