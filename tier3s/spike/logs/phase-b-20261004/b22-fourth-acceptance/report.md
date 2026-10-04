# qdistro CI report: bats-20261004T153647Z-3857896

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T15:36:47Z`
- **finished_utc**: `2026-10-04T15:43:10Z`
- **exit_code**: `35`
- **exit_class**: `bats`
- **workspace**: `/var/tmp/t3s-qci-b-4729fce58`
- **command**: `/var/tmp/t3s-qci-b-4729fce58/ci/bin/qci bats --file tests/integration/vm/phase7-tier3s-headless.bats --file tests/integration/vm/phase7-tier3s-denied.bats --file tests/integration/vm/phase7-tier3s-sigkill-cleanup.bats --file tests/integration/vm/phase7-tier3s-waypipe.bats --file tests/integration/vm/phase7-tier3s-app.bats --file tests/integration/vm/phase7-tier3s-lifecycle.bats --file tests/integration/vm/phase7-tier3s-chrome-secctx.bats --file tests/integration/vm/phase7-tier3s-clipboard-gate.bats --file tests/integration/vm/phase7-tier3s-lineage.bats --file tests/integration/vm/phase7-tier3s-hostile-stream.bats`
- **results**: fail=1, pass=9, skip=1
- **actionable failures**: 1 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 10 | 9 | 1 | 0 | 0 |
| vm | 1 | 0 | 0 | 0 | 1 |

## Failures and blocked work
### FAIL: bats / phase7-tier3s-clipboard-gate.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-clipboard-gate.bats.log)
- source: [ci/runs/bats-20261004T153647Z-3857896/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T153647Z-3857896/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T153647Z-3857896/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T153647Z-3857896/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log)
- notes: VM=qci-bats-phase7-tier3s-clipboard-gate-261004-174000-3898990-26758 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# FAIL: qdshell gate line names the real tagged source silo: got '0', want '1'
# FAIL: default-deny verdict at set-time: got '0', want '1'
# FAIL: text/plain offer reached the broker unfiltered: got '0', want '1'
# PASS: qdshell denied a png-only tier3s offer before the broker (1)
# PASS: cold-verify bound offers denied first (the allow was earned) (1)
# PASS: audit: denied transfer row(s) recorded (1)
# PASS: locker idle-timeout restored (ABSENT)
# [s127] 91 passes, 3 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# FAIL: qdshell gate line names the real tagged source silo: got '0', want '1'
# FAIL: default-deny verdict at set-time: got '0', want '1'
# FAIL: text/plain offer reached the broker unfiltered: got '0', want '1'
# PASS: qdshell denied a png-only tier3s offer before the broker (1)
# PASS: cold-verify bound offers denied first (the allow was earned) (1)
# PASS: audit: denied transfer row(s) recorded (1)
# PASS: locker idle-timeout restored (ABSENT)
# [s127] 91 passes, 3 failures
```

## Skips
- lifecycle / qci-bats-phase7-tier3s-clipboard-gate-261004-174000-3898990-26758: failed VM powered off (disk preserved) for debugging; virsh start to inspect

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 185 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pass | bats | phase7-tier3s-app.bats | integration | bats | 0 | [log](bats/phase7-tier3s-app.bats.log) | VM=qci-bats-phase7-tier3s-app-261004-174000-3898957-15258 |
| pass | bats | phase7-tier3s-chrome-secctx.bats | integration | bats | 0 | [log](bats/phase7-tier3s-chrome-secctx.bats.log) | VM=qci-bats-phase7-tier3s-chrome-secctx-261004-174000-3898991-6048 |
| fail | bats | phase7-tier3s-clipboard-gate.bats | integration | bats | 35 | [log](bats/phase7-tier3s-clipboard-gate.bats.log) | VM=qci-bats-phase7-tier3s-clipboard-gate-261004-174000-3898990-26758 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-clipboard-gate-261004-174000-3898990-26758 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-denied.bats | integration | bats | 0 | [log](bats/phase7-tier3s-denied.bats.log) | VM=qci-bats-phase7-tier3s-denied-261004-174000-3898916-12024 |
| pass | bats | phase7-tier3s-headless.bats | integration | bats | 0 | [log](bats/phase7-tier3s-headless.bats.log) | VM=qci-bats-phase7-tier3s-headless-261004-174000-3898901-32759 |
| pass | bats | phase7-tier3s-hostile-stream.bats | integration | bats | 0 | [log](bats/phase7-tier3s-hostile-stream.bats.log) | VM=qci-bats-phase7-tier3s-hostile-stream-261004-174148-3946161-6758 |
| pass | bats | phase7-tier3s-lifecycle.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lifecycle.bats.log) | VM=qci-bats-phase7-tier3s-lifecycle-261004-174000-3898966-17309 |
| pass | bats | phase7-tier3s-lineage.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lineage.bats.log) | VM=qci-bats-phase7-tier3s-lineage-261004-174000-3899012-27756 |
| pass | bats | phase7-tier3s-sigkill-cleanup.bats | integration | bats | 0 | [log](bats/phase7-tier3s-sigkill-cleanup.bats.log) | VM=qci-bats-phase7-tier3s-sigkill-cleanup-261004-174000-3898934-14848 |
| pass | bats | phase7-tier3s-waypipe.bats | integration | bats | 0 | [log](bats/phase7-tier3s-waypipe.bats.log) | VM=qci-bats-phase7-tier3s-waypipe-261004-174000-3898950-3597 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-b | c1f9ec5e6 | 0 | [status](repos/qdistro.status.txt) | `/var/tmp/t3s-qci-b-4729fce58` |

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
