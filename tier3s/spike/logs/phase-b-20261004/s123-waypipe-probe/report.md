# qdistro CI report: bats-20261003T162318Z-1944312

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-03T16:23:18Z`
- **finished_utc**: `2026-10-03T16:28:32Z`
- **exit_code**: `0`
- **exit_class**: `pass`
- **workspace**: `/var/tmp/t3s-qci-b-a1e16e5bb`
- **command**: `/var/tmp/t3s-qci-b-a1e16e5bb/ci/bin/qci bats --file tests/integration/vm/phase7-tier3s-waypipe.bats`
- **results**: pass=1
- **actionable failures**: 0 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 1 | 1 | 0 | 0 | 0 |

## Failures and blocked work
No failing or blocked result rows were recorded.

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 234 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pass | bats | phase7-tier3s-waypipe.bats | integration | bats | 0 | [log](bats/phase7-tier3s-waypipe.bats.log) | VM=qci-bats-phase7-tier3s-waypipe-261003-182726-2000967-12858 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | HEAD | a1e16e5bb | 0 | [status](repos/qdistro.status.txt) | `/var/tmp/t3s-qci-b-a1e16e5bb` |

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
