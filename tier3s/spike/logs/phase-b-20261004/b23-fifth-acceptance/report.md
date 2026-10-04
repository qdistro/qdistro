# qdistro CI report: bats-20261004T154612Z-3988839

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T15:46:12Z`
- **finished_utc**: `2026-10-04T15:52:33Z`
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
### FAIL: bats / phase7-tier3s-sigkill-cleanup.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-sigkill-cleanup.bats.log)
- source: [ci/runs/bats-20261004T154612Z-3988839/bats/phase7-tier3s-sigkill-cleanup.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T154612Z-3988839/bats/phase7-tier3s-sigkill-cleanup.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T154612Z-3988839/bats/phase7-tier3s-sigkill-cleanup.bats.scratch/s122.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T154612Z-3988839/bats/phase7-tier3s-sigkill-cleanup.bats.scratch/s122.log)
- notes: VM=qci-bats-phase7-tier3s-sigkill-cleanup-261004-174928-4010704-11836 raw_rc=1
- recommendation: Start with the collected user/system journals and the systemctl status artifact for the failed VM.

```text
# [t3s-setup] 64 passes, 0 failures
# ## 1. launcher SIGKILL (service failure), three times (ExecStopPost races the scope stop)
# PASS: sigkill#1: launch unit failed visibly (failed:signal)
#     unit: qdistro-tier3s-silo@s122a.service: Failed with result 'signal'.
# PASS: sigkill#2: launch unit failed visibly (failed:signal)
#     unit: qdistro-tier3s-silo@s122a.service: Failed with result 'signal'.
# PASS: sigkill#3: launch unit failed visibly (failed:signal)
#     unit: qdistro-tier3s-silo@s122a.service: Failed with result 'signal'.
# PASS: manager-stop: manager inactive (inactive)
# FAIL: manager-stop-after: no cleanup work dir left: got '1', want '0'
# PASS: ghost: labelled container live with no unit and no record (running:no:inactive)
# PASS: ghost: the manager reported no reconciliation failure (0)
# [s122] 151 passes, 1 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# ## 1. launcher SIGKILL (service failure), three times (ExecStopPost races the scope stop)
# PASS: sigkill#1: launch unit failed visibly (failed:signal)
#     unit: qdistro-tier3s-silo@s122a.service: Failed with result 'signal'.
# PASS: sigkill#2: launch unit failed visibly (failed:signal)
#     unit: qdistro-tier3s-silo@s122a.service: Failed with result 'signal'.
```

## Skips
- lifecycle / qci-bats-phase7-tier3s-sigkill-cleanup-261004-174928-4010704-11836: failed VM powered off (disk preserved) for debugging; virsh start to inspect

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 187 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pass | bats | phase7-tier3s-app.bats | integration | bats | 0 | [log](bats/phase7-tier3s-app.bats.log) | VM=qci-bats-phase7-tier3s-app-261004-174928-4010728-19 |
| pass | bats | phase7-tier3s-chrome-secctx.bats | integration | bats | 0 | [log](bats/phase7-tier3s-chrome-secctx.bats.log) | VM=qci-bats-phase7-tier3s-chrome-secctx-261004-174928-4010757-25861 |
| pass | bats | phase7-tier3s-clipboard-gate.bats | integration | bats | 0 | [log](bats/phase7-tier3s-clipboard-gate.bats.log) | VM=qci-bats-phase7-tier3s-clipboard-gate-261004-174928-4010769-670 |
| pass | bats | phase7-tier3s-denied.bats | integration | bats | 0 | [log](bats/phase7-tier3s-denied.bats.log) | VM=qci-bats-phase7-tier3s-denied-261004-174928-4010687-28948 |
| pass | bats | phase7-tier3s-headless.bats | integration | bats | 0 | [log](bats/phase7-tier3s-headless.bats.log) | VM=qci-bats-phase7-tier3s-headless-261004-174928-4010663-32537 |
| pass | bats | phase7-tier3s-hostile-stream.bats | integration | bats | 0 | [log](bats/phase7-tier3s-hostile-stream.bats.log) | VM=qci-bats-phase7-tier3s-hostile-stream-261004-175110-4068164-18997 |
| pass | bats | phase7-tier3s-lifecycle.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lifecycle.bats.log) | VM=qci-bats-phase7-tier3s-lifecycle-261004-174928-4010741-23850 |
| pass | bats | phase7-tier3s-lineage.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lineage.bats.log) | VM=qci-bats-phase7-tier3s-lineage-261004-174928-4010783-6161 |
| fail | bats | phase7-tier3s-sigkill-cleanup.bats | integration | bats | 35 | [log](bats/phase7-tier3s-sigkill-cleanup.bats.log) | VM=qci-bats-phase7-tier3s-sigkill-cleanup-261004-174928-4010704-11836 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-sigkill-cleanup-261004-174928-4010704-11836 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-waypipe.bats | integration | bats | 0 | [log](bats/phase7-tier3s-waypipe.bats.log) | VM=qci-bats-phase7-tier3s-waypipe-261004-174928-4010717-19842 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-b | 2e23d02a3 | 0 | [status](repos/qdistro.status.txt) | `/var/tmp/t3s-qci-b-4729fce58` |

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
