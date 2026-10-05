# qdistro CI report: bats-20261004T230508Z-3434100

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T23:05:08Z`
- **finished_utc**: `2026-10-04T23:05:50Z`
- **exit_code**: `0`
- **exit_class**: `pass`
- **workspace**: `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c`
- **command**: `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c/ci/bin/qci bats --vm qci-c1-record-5 --file tests/integration/vm/phase7-tier3s-netnone.bats`
- **results**: pass=1
- **actionable failures**: 0 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 1 | 1 | 0 | 0 | 0 |

## Failures and blocked work
No failing or blocked result rows were recorded.

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pass | bats | phase7-tier3s-netnone.bats | integration | bats | 0 | [log](bats/phase7-tier3s-netnone.bats.log) | VM=qci-c1-record-5 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-c | de11e595e | 0 | [status](repos/qdistro.status.txt) | `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c` |

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
