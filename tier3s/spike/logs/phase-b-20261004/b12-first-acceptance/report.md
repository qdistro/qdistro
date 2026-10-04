# qdistro CI report: bats-20261003T163624Z-2151098

## Summary
- **gate**: `bats`
- **started_utc**: `2026-10-03T16:36:24Z`
- **finished_utc**: `2026-10-03T16:45:15Z`
- **exit_code**: `35`
- **exit_class**: `bats`
- **workspace**: `/var/tmp/t3s-qci-b-a1e16e5bb`
- **command**: `/var/tmp/t3s-qci-b-a1e16e5bb/ci/bin/qci bats tests/integration/vm/phase7-tier3s-app.bats tests/integration/vm/phase7-tier3s-chrome-secctx.bats tests/integration/vm/phase7-tier3s-clipboard-gate.bats tests/integration/vm/phase7-tier3s-denied.bats tests/integration/vm/phase7-tier3s-headless.bats tests/integration/vm/phase7-tier3s-hostile-stream.bats tests/integration/vm/phase7-tier3s-lifecycle.bats tests/integration/vm/phase7-tier3s-lineage.bats tests/integration/vm/phase7-tier3s-sigkill-cleanup.bats tests/integration/vm/phase7-tier3s-waypipe.bats`
- **results**: fail=5, pass=5, skip=5
- **actionable failures**: 5 (Phase-1 clean-run metric; excludes 0 expected/non-actionable)

## Test categories
Per-category result tally. Categories are the shared confidence vocabulary documented in `ci/TAXONOMY.md`; this is reporting only — it gates nothing.

| category | total | pass | fail | blocked | skip |
| --- | --- | --- | --- | --- | --- |
| integration | 10 | 5 | 5 | 0 | 0 |
| vm | 5 | 0 | 0 | 0 | 5 |

## Failures and blocked work
### FAIL: bats / phase7-tier3s-app.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-app.bats.log)
- source: [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-app.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-app.bats.scratch/t3s-setup.log), [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-app.bats.scratch/s124.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-app.bats.scratch/s124.log)
- notes: VM=qci-bats-phase7-tier3s-app-261003-183943-2215538-4199 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 88 passes, 0 failures
# FAIL: weston: compositor reports focus on handle 1: got '2', want '1'
# FAIL: foot: compositor reports focus on handle 2: got '2', want '1'
# [s124] 61 passes, 2 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# FAIL: weston: compositor reports focus on handle 1: got '2', want '1'
# FAIL: foot: compositor reports focus on handle 2: got '2', want '1'
# [s124] 61 passes, 2 failures
```

### FAIL: bats / phase7-tier3s-clipboard-gate.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-clipboard-gate.bats.log)
- source: [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-clipboard-gate.bats.scratch/t3s-setup.log), [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-clipboard-gate.bats.scratch/s127.log)
- notes: VM=qci-bats-phase7-tier3s-clipboard-gate-261003-183943-2215542-25261 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# FAIL: qdshell denied a png-only tier3s offer before the broker: got '0', want '1'
# FAIL: qdshell logged the tier3s mime-strip: got '0', want '1'
# FAIL: live verdict flipped to allow under the rule: got '0', want '1'
# PASS: audit: denied transfer row(s) recorded (2)
# [s127] 67 passes, 3 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# FAIL: qdshell denied a png-only tier3s offer before the broker: got '0', want '1'
# FAIL: qdshell logged the tier3s mime-strip: got '0', want '1'
# FAIL: live verdict flipped to allow under the rule: got '0', want '1'
# PASS: audit: denied transfer row(s) recorded (2)
# [s127] 67 passes, 3 failures
```

### FAIL: bats / phase7-tier3s-hostile-stream.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-hostile-stream.bats.log)
- source: [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-hostile-stream.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-hostile-stream.bats.scratch/t3s-setup.log), [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-hostile-stream.bats.scratch/s129.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-hostile-stream.bats.scratch/s129.log)
- notes: VM=qci-bats-phase7-tier3s-hostile-stream-261003-183943-2215592-9458 raw_rc=1
- recommendation: Inspect qdwin/qdshell protocol logs first; avoid qdshell workarounds for compositor protocol bugs.

```text
# [t3s-setup] 82 passes, 0 failures
# PASS: single-attach: reconnect to A's consumed link.sock is refused (refused)
# FAIL: A's in-sandbox wayland socket resolved: got 'no', want 'yes'
# [s129] 70 passes, 1 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# PASS: single-attach: reconnect to A's consumed link.sock is refused (refused)
# FAIL: A's in-sandbox wayland socket resolved: got 'no', want 'yes'
# [s129] 70 passes, 1 failures
```

### FAIL: bats / phase7-tier3s-lifecycle.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-lifecycle.bats.log)
- source: [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-lifecycle.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-lifecycle.bats.scratch/t3s-setup.log), [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-lifecycle.bats.scratch/s125.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-lifecycle.bats.scratch/s125.log)
- notes: VM=qci-bats-phase7-tier3s-lifecycle-261003-184154-2284100-18162 raw_rc=1
- recommendation: Inspect qdwin/qdshell protocol logs first; avoid qdshell workarounds for compositor protocol bugs.

```text
# [t3s-setup] 82 passes, 0 failures
# INFO: no-compositor: start rc=1 Call failed: start of tier3s silo 's125a' failed: the launch was refused or failed before it ran: REFUSE: GUI workload weston-terminal but no admin compositor socket at /run/user/1000/wayland-1; the silo is Stopped (see journalctl -u qdistro-tier3s-silo@s125a.service) 
# PASS: no-compositor: StartSilo fails for the refused launch (rc=1)
# PASS: no-compositor: the silo reads Stopped right after the refused start (Stopped)
#     unit: qdistro-tier3s-silo@s125a.service: Failed with result 'exit-code'.
#     unit: Failed to start qdistro tier-3s (gVisor) silo s125a (Experimental, dev profile only).
# PASS: no-compositor: the spawn refused with the expected message (1)
# PASS: no-compositor: refusal fails the launch unit visibly (exit 2) (exit-code:2)
# ## 2. missing launch identity: bridge client never publishes a record
# INFO: no-launch-record: start rc=1 Call failed: start of tier3s silo 's125a' failed: the launch was refused or failed before it ran: REFUSE: the waypipe bridge client did not publish a live pid (launch record /run/user/1000/qdistro-tier3s-launchrec-5009a56a200a85684fe783a6f9366e8c.pid) (client log tail: ); the silo is Stopped (see jo
# PASS: no-launch-record: StartSilo fails for the refused launch (rc=1)
# PASS: no-launch-record: the silo reads Stopped right after the refused start (Stopped)
#     unit: qdistro-tier3s-silo@s125a.service: Failed with result 'exit-code'.
#     unit: Failed to start qdistro tier-3s (gVisor) silo s125a (Experimental, dev profile only).
# PASS: no-launch-record: the spawn refused with the expected message (1)
# PASS: no-launch-record: refusal fails the launch unit visibly (exit 2) (exit-code:2)
# ## 3. RegisterLaunch failure refuses before podman run
# PASS: control: direct RegisterLaunch is denied under the drop-in (rc=1)
# INFO: registerlaunch-denied: start rc=1 Call failed: start of tier3s silo 's125a' failed: the launch was refused or failed before it ran: REFUSE: RegisterLaunch failed for bridge client pid 7967; no unregistered GUI launch (client log tail: qdistro-secctx-exec: trusted launcher accepted via root-parent fallback (parent pid=7956 uid=0, exe
# PASS: registerlaunch-denied: StartSilo fails for the refused launch (rc=1)
```

### FAIL: bats / phase7-tier3s-lineage.bats
- exit: `35` class: `bats` kind: `bats`
- evidence: [log](bats/phase7-tier3s-lineage.bats.log)
- source: [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-lineage.bats.scratch/t3s-setup.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-lineage.bats.scratch/t3s-setup.log), [ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-lineage.bats.scratch/s128.log](/var/tmp/t3s-qci-b-a1e16e5bb/ci/runs/bats-20261003T163624Z-2151098/bats/phase7-tier3s-lineage.bats.scratch/s128.log)
- notes: VM=qci-bats-phase7-tier3s-lineage-261003-184201-2288949-22187 raw_rc=1
- recommendation: Rerun the single bats file against the preserved VM before broadening the search.

```text
# [t3s-setup] 82 passes, 0 failures
# PASS: RegisterLaunch of a dead pid is refused (1)
# PASS: RegisterLaunch with a wrong starttime is refused (1)
# ## 3. enforce mode: unattested/forged source can only be denied
# FAIL: broker journal shows the override for the forged claim: got '0', want '1'
# FAIL: attested source + allow rule -> allow: got 'deny', want 'allow'
# [s128] 51 passes, 2 failures
#   `assert_success' failed
# --- command failed (exit=1) ---
# PASS: RegisterLaunch of a dead pid is refused (1)
# PASS: RegisterLaunch with a wrong starttime is refused (1)
# ## 3. enforce mode: unattested/forged source can only be denied
# FAIL: broker journal shows the override for the forged claim: got '0', want '1'
# FAIL: attested source + allow rule -> allow: got 'deny', want 'allow'
# [s128] 51 passes, 2 failures
```

## Skips
- lifecycle / qci-bats-phase7-tier3s-app-261003-183943-2215538-4199: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-clipboard-gate-261003-183943-2215542-25261: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-hostile-stream-261003-183943-2215592-9458: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-lifecycle-261003-184154-2284100-18162: failed VM powered off (disk preserved) for debugging; virsh start to inspect
- lifecycle / qci-bats-phase7-tier3s-lineage-261003-184201-2288949-22187: failed VM powered off (disk preserved) for debugging; virsh start to inspect

## Agent attempts (non-clean)
Agent scenario attempts that did not cleanly PASS with rc=0 — the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.

| scenario | attempt | status | agent_rc | classifier | wall_s |
| --- | --- | --- | --- | --- | --- |
| golden-build | 1 | DONE | 0 | — | 188 |

## All Results
| status | gate | subject | category | kind | exit | log | notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fail | bats | phase7-tier3s-app.bats | integration | bats | 35 | [log](bats/phase7-tier3s-app.bats.log) | VM=qci-bats-phase7-tier3s-app-261003-183943-2215538-4199 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-app-261003-183943-2215538-4199 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-chrome-secctx.bats | integration | bats | 0 | [log](bats/phase7-tier3s-chrome-secctx.bats.log) | VM=qci-bats-phase7-tier3s-chrome-secctx-261003-183943-2215549-9939 |
| fail | bats | phase7-tier3s-clipboard-gate.bats | integration | bats | 35 | [log](bats/phase7-tier3s-clipboard-gate.bats.log) | VM=qci-bats-phase7-tier3s-clipboard-gate-261003-183943-2215542-25261 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-clipboard-gate-261003-183943-2215542-25261 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-denied.bats | integration | bats | 0 | [log](bats/phase7-tier3s-denied.bats.log) | VM=qci-bats-phase7-tier3s-denied-261003-183943-2215565-23150 |
| pass | bats | phase7-tier3s-headless.bats | integration | bats | 0 | [log](bats/phase7-tier3s-headless.bats.log) | VM=qci-bats-phase7-tier3s-headless-261003-183943-2215581-9330 |
| fail | bats | phase7-tier3s-hostile-stream.bats | integration | bats | 35 | [log](bats/phase7-tier3s-hostile-stream.bats.log) | VM=qci-bats-phase7-tier3s-hostile-stream-261003-183943-2215592-9458 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-hostile-stream-261003-183943-2215592-9458 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| fail | bats | phase7-tier3s-lifecycle.bats | integration | bats | 35 | [log](bats/phase7-tier3s-lifecycle.bats.log) | VM=qci-bats-phase7-tier3s-lifecycle-261003-184154-2284100-18162 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-lifecycle-261003-184154-2284100-18162 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| fail | bats | phase7-tier3s-lineage.bats | integration | bats | 35 | [log](bats/phase7-tier3s-lineage.bats.log) | VM=qci-bats-phase7-tier3s-lineage-261003-184201-2288949-22187 raw_rc=1 |
| skip | lifecycle | qci-bats-phase7-tier3s-lineage-261003-184201-2288949-22187 | vm | vm | 0 |  | failed VM powered off (disk preserved) for debugging; virsh start to inspect |
| pass | bats | phase7-tier3s-sigkill-cleanup.bats | integration | bats | 0 | [log](bats/phase7-tier3s-sigkill-cleanup.bats.log) | VM=qci-bats-phase7-tier3s-sigkill-cleanup-261003-184204-2291452-1917 |
| pass | bats | phase7-tier3s-waypipe.bats | integration | bats | 0 | [log](bats/phase7-tier3s-waypipe.bats.log) | VM=qci-bats-phase7-tier3s-waypipe-261003-184220-2300038-22186 |

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
