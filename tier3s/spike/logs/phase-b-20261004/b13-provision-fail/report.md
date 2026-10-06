# qdistro CI report: bats-20261003T173009Z-2934791

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-03T17:30:09Z`
- **finished_utc**: `2026-10-03T17:30:19Z`
- **exit_code**: `40`
- **exit_class**: `vm_provision`
- **workspace**: `/var/tmp/t3s-qci-b-4729fce58`
- **command**: `/var/tmp/t3s-qci-b-4729fce58/ci/bin/qci bats --file tests/integration/vm/phase7-tier3s-app.bats --file tests/integration/vm/phase7-tier3s-clipboard-gate.bats --file tests/integration/vm/phase7-tier3s-hostile-stream.bats --file tests/integration/vm/phase7-tier3s-lifecycle.bats --file tests/integration/vm/phase7-tier3s-lineage.bats`
- **results**: fail=1
- **actionable failures**: 1 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| vm | 1 | 0 | 1 | 0 | 0 |

## Failures and blocked work
### FAIL: bats / golden-build
- exit: `40` class: `vm_provision` kind: `vm`
- evidence: [log](vm/golden-bats.log)
- notes: run-golden build failed (rc=3)
- recommendation: Build or repair the prebaked VM image with qdistro/scripts/vm/build-baked-baseweed.sh.

```text
[spin-test-vm] stage 1: baseweed-admin.qcow2 already present
[spin-test-vm] stage 2: baseweed-baked.qcow2 already present
[spin-test-vm] building/caching native components in rootless Podman...
fatal: not a git repository (or any parent up to mount point /)
Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).
```

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | FAIL | 3 | golden-build | 10 |

## Unmatched-classifier attempts (marker-drift watch)
1 failing attempt(s) matched NO infra/tooling marker and fell through to a generic classifier. Usually real product failures — but a rising count can mean a drifted provider/CLI marker string is demoting infra failures to product-fail. Inspect the `*.unmatched-tail.txt` sidecar beside each agent log. Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | FAIL | 3 | golden-build | 10 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fail | bats | golden-build | vm | vm | 40 | [log](vm/golden-bats.log) | run-golden build failed (rc=3) |

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
