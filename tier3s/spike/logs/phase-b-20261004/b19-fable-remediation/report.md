# qdistro CI report: bats-20261004T123605Z-3058184

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-04T12:36:05Z`
- **finished_utc**: `2026-10-04T12:41:49Z`
- **exit_code**: `35`
- **exit_class**: `bats`
- **workspace**: `/var/tmp/t3s-qci-b-4729fce58`
- **command**: `/var/tmp/t3s-qci-b-4729fce58/ci/bin/qci bats --file tests/integration/vm/phase7-tier3s-headless.bats --file tests/integration/vm/phase7-tier3s-denied.bats --file tests/integration/vm/phase7-tier3s-sigkill-cleanup.bats --file tests/integration/vm/phase7-tier3s-waypipe.bats --file tests/integration/vm/phase7-tier3s-app.bats --file tests/integration/vm/phase7-tier3s-lifecycle.bats --file tests/integration/vm/phase7-tier3s-chrome-secctx.bats --file tests/integration/vm/phase7-tier3s-clipboard-gate.bats --file tests/integration/vm/phase7-tier3s-lineage.bats --file tests/integration/vm/phase7-tier3s-hostile-stream.bats`
- **results**: fail=4, pass=6, skip=4
- **actionable failures**: 4 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 10 | 6 | 4 | 0 | 0 |
| vm | 4 | 0 | 0 | 0 | 4 |

## Failures and blocked work
### FAIL: bats / phase7-tier3s-app.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-app.bats.log)
- source: [ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-app.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-app.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-app.bats.scratch/s124.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-app.bats.scratch/s124.log)
- notes: VM=qci-bats-phase7-tier3s-app-261004-143857-3068958-3111 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 88 passes, 0 failures
# FAIL: s124w: spec carries the weston-terminal.json profile: got '', want 'weston-terminal.json'
# FAIL: s124w: NoNewPrivs + seccomp filter mode inside: got 'sh: line 1: awk: command not found
# sh: line 1: awk: command not found
# FAIL: s124f: spec carries the foot.json profile: got '', want 'foot.json'
# FAIL: s124f: NoNewPrivs + seccomp filter mode inside: got 'sh: line 1: awk: command not found
# sh: line 1: awk: command not found
# [s124] 71 passes, 4 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# FAIL: s124w: spec carries the weston-terminal.json profile: got '', want 'weston-terminal.json'
# FAIL: s124w: NoNewPrivs + seccomp filter mode inside: got 'sh: line 1: awk: command not found
# sh: line 1: awk: command not found
# FAIL: s124f: spec carries the foot.json profile: got '', want 'foot.json'
# FAIL: s124f: NoNewPrivs + seccomp filter mode inside: got 'sh: line 1: awk: command not found
# sh: line 1: awk: command not found
# [s124] 71 passes, 4 failures
```

### FAIL: bats / phase7-tier3s-clipboard-gate.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-clipboard-gate.bats.log)
- source: [ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log)
- notes: VM=qci-bats-phase7-tier3s-clipboard-gate-261004-143857-3068992-3980 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# PASS: qdshell denied a png-only tier3s offer before the broker (1)
# FAIL: enforce: forged silo claim over A's registered bridge still resolves attested: got 'allow', want 'deny'
# FAIL: live verdict flipped to allow under the rule: got '0', want '1'
# FAIL: receive probe allows text/plain under the mime rule (attested bridge pid): got 'deny', want 'allow'
# PASS: audit: denied transfer row(s) recorded (1)
# [s127] 76 passes, 3 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# PASS: qdshell denied a png-only tier3s offer before the broker (1)
# FAIL: enforce: forged silo claim over A's registered bridge still resolves attested: got 'allow', want 'deny'
# FAIL: live verdict flipped to allow under the rule: got '0', want '1'
# FAIL: receive probe allows text/plain under the mime rule (attested bridge pid): got 'deny', want 'allow'
# PASS: audit: denied transfer row(s) recorded (1)
# [s127] 76 passes, 3 failures
```

### FAIL: bats / phase7-tier3s-hostile-stream.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-hostile-stream.bats.log)
- source: [ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-hostile-stream.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-hostile-stream.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-hostile-stream.bats.scratch/s129.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-hostile-stream.bats.scratch/s129.log)
- notes: VM=qci-bats-phase7-tier3s-hostile-stream-261004-143857-3069028-21740 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# PASS: single-attach: reconnect to A's consumed link.sock is refused (refused)
# FAIL: waypipe frames written onto the SANDBOX end of the link (at the trusted client parser): got '0', want '1'
# [s129] 75 passes, 1 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# PASS: single-attach: reconnect to A's consumed link.sock is refused (refused)
# FAIL: waypipe frames written onto the SANDBOX end of the link (at the trusted client parser): got '0', want '1'
# [s129] 75 passes, 1 failures
```

### FAIL: bats / phase7-tier3s-waypipe.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-waypipe.bats.log)
- source: [ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-waypipe.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-waypipe.bats.scratch/t3s-setup.log), [ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-waypipe.bats.scratch/s123.log](/var/tmp/t3s-qci-b-4729fce58/ci/runs/bats-20261004T123605Z-3058184/bats/phase7-tier3s-waypipe.bats.scratch/s123.log)
- notes: VM=qci-bats-phase7-tier3s-waypipe-261004-143857-3068947-26469 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# PASS: sandbox write to the bridge dir is refused (rc=1)
# FAIL: host link.sock is admin-owned 0600 (umask 0177 wrap): got '', want '600:1000'
# FAIL: link.sock is a live socket inside the sandbox (host-uds=open passthrough): got 'no', want 'yes'
# PASS: single-attach: a second connect to link.sock is refused (refused)
# [s123] 71 passes, 2 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# PASS: sandbox write to the bridge dir is refused (rc=1)
# FAIL: host link.sock is admin-owned 0600 (umask 0177 wrap): got '', want '600:1000'
# FAIL: link.sock is a live socket inside the sandbox (host-uds=open passthrough): got 'no', want 'yes'
# PASS: single-attach: a second connect to link.sock is refused (refused)
# [s123] 71 passes, 2 failures
```

## Skips
- lifecycle / qci-bats-phase7-tier3s-app-261004-143857-3068958-3111: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-clipboard-gate-261004-143857-3068992-3980: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-hostile-stream-261004-143857-3069028-21740: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-waypipe-261004-143857-3068947-26469: failed VM powered off (disk preserved) for debugging; virsh start to inspect

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 162 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fail | bats | phase7-tier3s-app.bats | integration | bats | 35 | [log](bats/phase7-tier3s-app.bats.log) | VM=qci-bats-phase7-tier3s-app-261004-143857-3068958-3111 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-app-261004-143857-3068958-3111 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-chrome-secctx.bats | integration | bats | 0 | [log](bats/phase7-tier3s-chrome-secctx.bats.log) | VM=qci-bats-phase7-tier3s-chrome-secctx-261004-143857-3068984-10460 |
| fail | bats | phase7-tier3s-clipboard-gate.bats | integration | bats | 35 | [log](bats/phase7-tier3s-clipboard-gate.bats.log) | VM=qci-bats-phase7-tier3s-clipboard-gate-261004-143857-3068992-3980 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-clipboard-gate-261004-143857-3068992-3980 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-denied.bats | integration | bats | 0 | [log](bats/phase7-tier3s-denied.bats.log) | VM=qci-bats-phase7-tier3s-denied-261004-143857-3068930-6083 |
| pass | bats | phase7-tier3s-headless.bats | integration | bats | 0 | [log](bats/phase7-tier3s-headless.bats.log) | VM=qci-bats-phase7-tier3s-headless-261004-143857-3068897-9380 |
| fail | bats | phase7-tier3s-hostile-stream.bats | integration | bats | 35 | [log](bats/phase7-tier3s-hostile-stream.bats.log) | VM=qci-bats-phase7-tier3s-hostile-stream-261004-143857-3069028-21740 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-hostile-stream-261004-143857-3069028-21740 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-lifecycle.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lifecycle.bats.log) | VM=qci-bats-phase7-tier3s-lifecycle-261004-143857-3068976-26452 |
| pass | bats | phase7-tier3s-lineage.bats | integration | bats | 0 | [log](bats/phase7-tier3s-lineage.bats.log) | VM=qci-bats-phase7-tier3s-lineage-261004-143857-3069025-32179 |
| pass | bats | phase7-tier3s-sigkill-cleanup.bats | integration | bats | 0 | [log](bats/phase7-tier3s-sigkill-cleanup.bats.log) | VM=qci-bats-phase7-tier3s-sigkill-cleanup-261004-143857-3068917-28481 |
| fail | bats | phase7-tier3s-waypipe.bats | integration | bats | 35 | [log](bats/phase7-tier3s-waypipe.bats.log) | VM=qci-bats-phase7-tier3s-waypipe-261004-143857-3068947-26469 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-waypipe-261004-143857-3068947-26469 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |

## Repo State
| repo | branch | head | dirty | status | root |
| --- | --- | --- | --- | --- | --- |
| qdistro | claude/tier3s-b | 920351f70 | 0 | [status](repos/qdistro.status.txt) | `/var/tmp/t3s-qci-b-4729fce58` |

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
