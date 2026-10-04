# qdistro CI report: bats-20261004T202011Z-2424301

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T20:20:11Z`
- **finished_utc**: `2026-10-04T20:27:43Z`
- **exit_code**: `0`
- **exit_class**: `pass`
- **workspace**: `/var/tmp/t3s-qci-b-4729fce58`
- **command**: `/var/tmp/t3s-qci-b-4729fce58/ci/bin/qci bats --file tests/integration/vm/phase7-tier3s-headless.bats --file tests/integration/vm/phase7-tier3s-denied.bats --file tests/integration/vm/phase7-tier3s-sigkill-cleanup.bats --file tests/integration/vm/phase7-tier3s-waypipe.bats --file tests/integration/vm/phase7-tier3s-app.bats --file tests/integration/vm/phase7-tier3s-lifecycle.bats --file tests/integration/vm/phase7-tier3s-chrome-secctx.bats --file tests/integration/vm/phase7-tier3s-clipboard-gate.bats --file tests/integration/vm/phase7-tier3s-lineage.bats --file tests/integration/vm/phase7-tier3s-hostile-stream.bats`
- **results**: pass=10
- **actionable failures**: 0 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 10 | 10 | 0 | 0 | 0 |

## Failures and blocked work
No failing or blocked result rows were recorded.

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 141 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pass | bats | phase7-tier3s-app.bats | integration | bats | 0 | [log](bats/phase7-tier3s-app.bats.log) | VM=qci-bats-phase7-tier3s-app-261004-222244-2473750-16863 |
| pass | bats | phase7-tier3s-chrome-secctx.bats | integration | bats | 0 | [log](bats/phase7-tier3s-chrome-secctx.bats.log) | VM=qci-bats-phase7-tier3s-chrome-secctx-261004-222244-2473773-10494 |
| pass | bats | phase7-tier3s-clipboard-gate.bats | integration | bats | 0 | [log](bats/phase7-tier3s-clipboard-gate.bats.log) | VM=qci-bats-phase7-tier3s-clipboard-gate-261004-222244-2473789-4437 |
| pass | bats | phase7-tier3s-denied.bats | integration | bats | 0 | [log](bats/phase7-tier3s-denied.bats.log) | VM=qci-bats-phase7-tier3s-denied-261004-222244-2473717-30743 |
| pass | bats | phase7-tier3s-headless.bats | integration | bats | 0 | [log](bats/phase7-tier3s-headless.bats.log) | VM=qci-bats-phase7-tier3s-headless-261004-222244-2473697-21750 |
| pass | bats | phase7-tier3s-hostile-stream.bats | integration | bats | 0 | [log](bats/phase7-tier3s-hostile-stream.bats.log) | VM=qci-bats-phase7-tier3s-hostile-stream-261004-222244-2473826-6995 |
| pass | bats | phase7-tier3s-lifecycle.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lifecycle.bats.log) | VM=qci-bats-phase7-tier3s-lifecycle-261004-222244-2473785-9090 |
| pass | bats | phase7-tier3s-lineage.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lineage.bats.log) | VM=qci-bats-phase7-tier3s-lineage-261004-222244-2473811-26276 |
| pass | bats | phase7-tier3s-sigkill-cleanup.bats | integration | bats | 0 | [log](bats/phase7-tier3s-sigkill-cleanup.bats.log) | VM=qci-bats-phase7-tier3s-sigkill-cleanup-261004-222244-2473721-9428 |
| pass | bats | phase7-tier3s-waypipe.bats | integration | bats | 0 | [log](bats/phase7-tier3s-waypipe.bats.log) | VM=qci-bats-phase7-tier3s-waypipe-261004-222244-2473753-27005 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-b | 7ad3518c3 | 0 | [status](repos/qdistro.status.txt) | `/var/tmp/t3s-qci-b-4729fce58` |

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
