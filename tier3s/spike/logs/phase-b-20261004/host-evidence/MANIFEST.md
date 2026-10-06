# Phase B host-side evidence — commit `aa2e3afd4` (+`5bbcec7b7` harness fix); `pytest-tier3s-spawn-final.log` re-verified at `748a1baa1`

Recorded: 2026-10-04, worktree `/home/play2/qdistro/.worktrees/qdistro-tier3s`,
branch `claude/tier3s-b`. Commands run from the worktree root.

`COMMIT` / `DIRTY_COUNT` pin the tree state at capture time (mutate-guards'
own edit was committed as `5bbcec7b7` before its rerun).

## Results

| Command | Log | Result |
|---|---|---|
| `python3 -m pytest tests/unit -q` | `pytest-unit.log` | 6886 passed, 8 skipped; 30 failed — all `test_mm_*` (multi-monitor), `OSError: Address already in use` on port 5556: a concurrent `qci host` run in another session's worktree held the control-server port through that batch. Unrelated files to this branch. |
| `python3 -m pytest -q -p no:cacheprovider <28 tier3s-relevant files>` (list below) | `pytest-unit-tier3s-relevant.log` | 1074 passed, 1 skipped; 2 failed — `test_a_query_is_cut_at_the_batch_deadline`, `test_the_bindsto_wait_is_by_the_clock` (timing-budget assertions while play1's `qci full` held the host). Both **passed** in `pytest-unit.log` earlier and in the isolated rerun. |
| `pytest` the 2 timing tests alone | `pytest-tier3s-timing-rerun.log` | 2 passed in 5.04s — confirmed load flake, not a regression. |
| `node qdshell/tests/test_tier3s_gate_behaviour.js` | `jstest-test_tier3s_gate_behaviour.js.log` | all checks passed, EXIT=0 |
| `node qdshell/tests/test_drift_guard.js` | `jstest-test_drift_guard.js.log` | all assertions passed, EXIT=0 |
| `node` over all `qdshell/tests/test_*.js` | `jstest-all.log` | 61 files, 0 failures |
| `python3 tier3s/spike/mutate-guards.py` | `mutate-guards.log` | 183/184 CAUGHT; 1 problem = E3 HARNESS ERROR (stale snippet after the refuse→bridge_refuse B-i change) |
| E3 explanation + `--only E3` rerun | `mutate-guards-e3-rerun.log` | CAUGHT; harness fixed in `5bbcec7b7` |
| `python3 tier3s/spike/mutate-guards.py` (full rerun at `5bbcec7b7`) | `mutate-guards-full.log` | 184/184 CAUGHT, 0 problems, EXIT=0 |
| `python3 -m pytest tests/unit/test_tier3s_spawn.py -q` at `748a1baa1` | `pytest-tier3s-spawn-final.log` | 204 passed — re-verified after the fable remediation (ro mount, bcst anchor) |

## r2 re-verification — `493312a25` (post-GUI-remediation)

| Command | Log | Result |
|---|---|---|
| `pytest` the 12 `test_broker_*` files | `pytest-broker-r2.log` | 209 passed — covers `36d779097` (VerifyClientIdentity gate admission) |
| `node` over all `qdshell/tests/test_*.js` | `jstest-all-r2.log` | 61 files, 0 failures — covers `b6523e801` (sidecar↔handle binding) |
| `python3 -m pytest tests/unit/test_tier3s_spawn.py -q` | `pytest-tier3s-spawn-r2.log` | 199 passed, 5 failed — all timing-budget timeouts while the b20c image build + mutation harness + 4 preserved VMs loaded the host; the de-flaked test itself passed |
| `pytest` the same 5 alone | `pytest-tier3s-spawn-r2-isolated.log` | 5 passed in 3.69s — load flake, not a regression |
| `python3 tier3s/spike/mutate-guards.py` | `mutate-guards-r2.log` | 184/184 CAUGHT, 0 problems — after the E2 `:rw`→`:ro` snippet refresh in `c1f9ec5e6`; baseline 444 passed |

## Relevant-set file list (pytest-unit-tier3s-relevant.log)

test_tier3s_probe, test_tier3s_provision, test_tier3s_spawn,
test_session_manager{,_audit,_dbus_async,_install,_subprocess_bounds,_tier3s},
test_tier1_spawn, test_tier2_snapshot_repos, test_tier2_spawn,
test_spawn_common, test_session_spawn_game, test_secctx_exec_hardening,
test_broker_{register_launch,cross_silo_lineage,lineage_gate,
layered_identity,secctx_provenance,clipboard_transfer,clipboard_receive,
identity_gate_integration,session_manager_handoff,delegated_argv_scopes,
check_permission,dbus_policy_coverage}.

Tier-2 surface: `test_tier1_spawn` + `test_tier2_*` + `test_spawn_common`
run unchanged and green — tier-2 launch semantics untouched by this
branch's diff.
