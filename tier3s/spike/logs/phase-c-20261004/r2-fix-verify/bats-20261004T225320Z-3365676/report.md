# qdistro CI report: bats-20261004T225320Z-3365676

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T22:53:20Z`
- **finished_utc**: `2026-10-04T22:58:35Z`
- **exit_code**: `35`
- **exit_class**: `bats`
- **workspace**: `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c`
- **command**: `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c/ci/bin/qci bats --vm qci-c1-record-4 --file tests/integration/vm/phase7-tier3s-limits.bats`
- **results**: fail=1
- **actionable failures**: 1 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 1 | 0 | 1 | 0 | 0 |

## Failures and blocked work
### FAIL: bats / phase7-tier3s-limits.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-limits.bats.log)
- source: [ci/runs/bats-20261004T225320Z-3365676/bats/phase7-tier3s-limits.bats.scratch/t3s-setup.log](/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c/ci/runs/bats-20261004T225320Z-3365676/bats/phase7-tier3s-limits.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T225320Z-3365676/bats/phase7-tier3s-limits.bats.scratch/s130.log](/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c/ci/runs/bats-20261004T225320Z-3365676/bats/phase7-tier3s-limits.bats.scratch/s130.log)
- notes: VM=qci-c1-record-4 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 64 passes, 0 failures
# PASS: limits: admin write to memory.max fails with EACCES/EPERM (rc=1)
# PASS: limits: admin write to memory.swap.max fails with EACCES/EPERM (rc=1)
# PASS: limits: admin write to pids.max fails with EACCES/EPERM (rc=1)
# PASS: limits: admin write to cpu.max fails with EACCES/EPERM (rc=1)
#     guest bomb: waiting on pid 54: waiting on PID 54 in sandbox "2d4f97cfab39ccee1e8617bbfc6547c705a77e24276785d562c2469607169dca": urpc method "containerManager.WaitPID" failed: EOF
# INFO: guest bomb: host peak pids.current=1021 (limit 1024), pids.events.local max 0 -> 4, guest jobs=died, fork-error lines: 0
# PASS: tasks: pids.events.local max grew — this scope's pids.max denied a fork (0 -> 4)
#     hog: waiting on pid 54: waiting on PID 54 in sandbox "be1ac80611e7da050b323282e3153157bde1d13242203983bef7515177f77a6f": urpc method "containerManager.WaitPID" failed: EOF|
# [s130] 83 passes, 0 failures
#   `assert_output_contains "PASS: tasks: pids.events max grew"' failed
# PASS: limits: admin write to memory.max fails with EACCES/EPERM (rc=1)
# PASS: limits: admin write to memory.swap.max fails with EACCES/EPERM (rc=1)
# PASS: limits: admin write to pids.max fails with EACCES/EPERM (rc=1)
# PASS: limits: admin write to cpu.max fails with EACCES/EPERM (rc=1)
#     guest bomb: waiting on pid 54: waiting on PID 54 in sandbox "2d4f97cfab39ccee1e8617bbfc6547c705a77e24276785d562c2469607169dca": urpc method "containerManager.WaitPID" failed: EOF
# INFO: guest bomb: host peak pids.current=1021 (limit 1024), pids.events.local max 0 -> 4, guest jobs=died, fork-error lines: 0
# PASS: tasks: pids.events.local max grew — this scope's pids.max denied a fork (0 -> 4)
#     hog: waiting on pid 54: waiting on PID 54 in sandbox "be1ac80611e7da050b323282e3153157bde1d13242203983bef7515177f77a6f": urpc method "containerManager.WaitPID" failed: EOF|
# [s130] 83 passes, 0 failures
```

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fail | bats | phase7-tier3s-limits.bats | integration | bats | 35 | [log](bats/phase7-tier3s-limits.bats.log) | VM=qci-c1-record-4 raw_rc=1 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-c | 39ad0d68c | 0 | [status](repos/qdistro.status.txt) | `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c` |

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
- [results.tsv](results.tsv)
- [scenario-attempts.tsv](scenario-attempts.tsv)
- [screenshots/](screenshots)
- [selftest/](selftest)
- [timings.tsv](timings.tsv)
- [vm/](vm)
