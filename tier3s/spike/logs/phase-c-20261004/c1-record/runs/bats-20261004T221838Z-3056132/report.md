# qdistro CI report: bats-20261004T221838Z-3056132

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T22:18:38Z`
- **finished_utc**: `2026-10-04T22:19:38Z`
- **exit_code**: `0`
- **exit_class**: `pass`
- **workspace**: `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c`
- **command**: `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c/ci/bin/qci bats --vm qci-bats-phase7-tier3s-lineage-261004-224817-2678925-19735 --file tests/integration/vm/phase7-tier3s-lineage.bats`
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
| pass | bats | phase7-tier3s-lineage.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lineage.bats.log) | VM=qci-bats-phase7-tier3s-lineage-261004-224817-2678925-19735 |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-c | e80b9798b | 0 | [status](repos/qdistro.status.txt) | `/home/play2/qdistro/qdistro/.worktrees/qdistro-tier3s-c` |

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
