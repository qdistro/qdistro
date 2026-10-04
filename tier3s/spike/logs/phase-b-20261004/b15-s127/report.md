# qdistro CI report: bats-20261003T192421Z-3265587

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-03T19:24:21Z`
- **finished_utc**: `2026-10-03T19:29:19Z`
- **exit_code**: `35`
- **exit_class**: `bats`
- **workspace**: `/var/tmp/t3s-qci-b-4729fce58`
- **command**: `/var/tmp/t3s-qci-b-4729fce58/ci/bin/qci bats --file tests/integration/vm/phase7-tier3s-clipboard-gate.bats`
- **results**: fail=1, skip=1
- **actionable failures**: 1 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 1 | 0 | 1 | 0 | 0 |
| vm | 1 | 0 | 0 | 0 | 1 |

## Failures and blocked work
### FAIL: bats / phase7-tier3s-clipboard-gate.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-clipboard-gate.bats.log)
- source: [ci/runs/bats-20261003T192421Z-3265587/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261003T192421Z-3265587/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log), [ci/runs/bats-20261003T192421Z-3265587/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261003T192421Z-3265587/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log)
- notes: VM=qci-bats-phase7-tier3s-clipboard-gate-261003-212714-3309740-30969 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# FAIL: qdshell denied a png-only tier3s offer before the broker: got '0', want '1'
# FAIL: qdshell logged the tier3s mime-strip: got '0', want '1'
# FAIL: live verdict flipped to allow under the rule: got '0', want '1'
# PASS: audit: denied transfer row(s) recorded (2)
# FAIL: end: no cleanup work dir left: got '1', want '0'
# [s127] 66 passes, 4 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# FAIL: qdshell denied a png-only tier3s offer before the broker: got '0', want '1'
# FAIL: qdshell logged the tier3s mime-strip: got '0', want '1'
# FAIL: live verdict flipped to allow under the rule: got '0', want '1'
# PASS: audit: denied transfer row(s) recorded (2)
# FAIL: end: no cleanup work dir left: got '1', want '0'
# [s127] 66 passes, 4 failures
```

## Skips
- lifecycle / qci-bats-phase7-tier3s-clipboard-gate-261003-212714-3309740-30969: failed VM powered off (disk preserved) for debugging; virsh start to inspect

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 160 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fail | bats | phase7-tier3s-clipboard-gate.bats | integration | bats | 35 | [log](bats/phase7-tier3s-clipboard-gate.bats.log) | VM=qci-bats-phase7-tier3s-clipboard-gate-261003-212714-3309740-30969 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-clipboard-gate-261003-212714-3309740-30969 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-b | 0f09b88a3 | 0 | [status](repos/qdistro.status.txt) | `/var/tmp/t3s-qci-b-4729fce58` |

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
