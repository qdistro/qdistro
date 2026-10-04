# qdistro CI report: bats-20261004T183215Z-962731

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T18:32:15Z`
- **finished_utc**: `2026-10-04T18:54:30Z`
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
### FAIL: bats / phase7-tier3s-chrome-secctx.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-chrome-secctx.bats.log)
- source: [ci/runs/bats-20261004T183215Z-962731/bats/phase7-tier3s-chrome-secctx.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T183215Z-962731/bats/phase7-tier3s-chrome-secctx.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T183215Z-962731/bats/phase7-tier3s-chrome-secctx.bats.scratch/s126.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T183215Z-962731/bats/phase7-tier3s-chrome-secctx.bats.scratch/s126.log)
- notes: VM=qci-bats-phase7-tier3s-chrome-secctx-261004-203428-969615-1323 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# PASS: Tier3FocusIPC rejects a non-tier handle (9999) (error: handle=9999 is not a tier-3 toplevel)
# PASS: bridge path: output-manager test refused (implementation error)
# PASS: bridge path: output-manager apply refused (implementation error)
# PASS: tagged peer: output-manager test refused (implementation error) (0)
# PASS: tagged peer: test denial carried the implementation-error line (1)
# PASS: tagged peer: output-manager apply refused (implementation error) (0)
# PASS: tagged peer: apply denial carried the implementation-error line (1)
# PASS: second connect on the consumed context is refused (live EOF) (recv_eof)
# PASS: compositor logged the refused extra connection (1)
# FAIL: a bridge-probe container survived its probe's exit
# [s126] 90 passes, 1 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# PASS: Tier3FocusIPC rejects a non-tier handle (9999) (error: handle=9999 is not a tier-3 toplevel)
# PASS: bridge path: output-manager test refused (implementation error)
# PASS: bridge path: output-manager apply refused (implementation error)
# PASS: tagged peer: output-manager test refused (implementation error) (0)
# PASS: tagged peer: test denial carried the implementation-error line (1)
# PASS: tagged peer: output-manager apply refused (implementation error) (0)
```

## Skips
- lifecycle / qci-bats-phase7-tier3s-chrome-secctx-261004-203428-969615-1323: failed VM powered off (disk preserved) for debugging; virsh start to inspect

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 124 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pass | bats | phase7-tier3s-app.bats | integration | bats | 0 | [log](bats/phase7-tier3s-app.bats.log) | VM=qci-bats-phase7-tier3s-app-261004-203428-969592-31213 |
| fail | bats | phase7-tier3s-chrome-secctx.bats | integration | bats | 35 | [log](bats/phase7-tier3s-chrome-secctx.bats.log) | VM=qci-bats-phase7-tier3s-chrome-secctx-261004-203428-969615-1323 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-chrome-secctx-261004-203428-969615-1323 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-clipboard-gate.bats | integration | bats | 0 | [log](bats/phase7-tier3s-clipboard-gate.bats.log) | VM=qci-bats-phase7-tier3s-clipboard-gate-261004-203428-969633-22526 |
| pass | bats | phase7-tier3s-denied.bats | integration | bats | 0 | [log](bats/phase7-tier3s-denied.bats.log) | VM=qci-bats-phase7-tier3s-denied-261004-203428-969551-5106 |
| pass | bats | phase7-tier3s-headless.bats | integration | bats | 0 | [log](bats/phase7-tier3s-headless.bats.log) | VM=qci-bats-phase7-tier3s-headless-261004-203428-969530-1287 |
| pass | bats | phase7-tier3s-hostile-stream.bats | integration | bats | 0 | [log](bats/phase7-tier3s-hostile-stream.bats.log) | VM=qci-bats-phase7-tier3s-hostile-stream-261004-203428-969665-6451 |
| pass | bats | phase7-tier3s-lifecycle.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lifecycle.bats.log) | VM=qci-bats-phase7-tier3s-lifecycle-261004-203428-969601-10753 |
| pass | bats | phase7-tier3s-lineage.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lineage.bats.log) | VM=qci-bats-phase7-tier3s-lineage-261004-203428-969649-16965 |
| pass | bats | phase7-tier3s-sigkill-cleanup.bats | integration | bats | 0 | [log](bats/phase7-tier3s-sigkill-cleanup.bats.log) | VM=qci-bats-phase7-tier3s-sigkill-cleanup-261004-203428-969564-13843 |
| pass | bats | phase7-tier3s-waypipe.bats | integration | bats | 0 | [log](bats/phase7-tier3s-waypipe.bats.log) | VM=qci-bats-phase7-tier3s-waypipe-261004-203428-969569-22331 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-b | 24540c6f0 | 0 | [status](repos/qdistro.status.txt) | `/var/tmp/t3s-qci-b-4729fce58` |

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
