# CI hardening — remaining work

Follow-ups from the 2026-06-13 parallelization work (see `ci/README.md` →
Parallelism & per-run golden). Focus is **CI robustness/speed**, NOT fixing
individual failing tests yet. Each item below was found by measuring a full
parallel `qci full` run (90 min; per-task timings in the run's `timings.tsv`)
and reviewing the harness with subagents + codex.

Ordered by priority.

## 1. `vm-exec` overall deadline  (MUST-FIX)  — DONE 2026-06-13
`scripts/vm/vm-exec` polled `guest-exec-status` in a `while true` with **no
overall timeout** — it only bailed after 5 consecutive *agent unreachable*
errors. A wedged/CPU-starved in-guest command made it poll forever, holding a
parallel worker slot (`tier2-silo-secctx-wiretag.bats` hung a run for 36 min).
Fixed: `QDISTRO_VM_EXEC_TIMEOUT` (default **1800s**; the slowest legit work item
observed was 1031s) bounds the poll loop via bash `SECONDS`; on expiry it does an
in-guest **descendant-tree** kill (BFS over `ps --ppid`, collect-then-signal so
reparenting can't hide children — `pkill -P` alone leaked them in testing) and
exits **124** with the command + elapsed seconds. Also added
`QDISTRO_VM_AGENT_RPC_TIMEOUT` (default 30s) wrapping every
`virsh qemu-agent-command` in host-side `timeout` — without it the `SECONDS`
check can't fire while blocked in a command substitution (codex's main finding).
`vm-start-and-wait` `MAX_WAIT` raised 120→300 (`QD_VM_START_MAX_WAIT`).
VM-verified (normal path + exec'd / backgrounded / grandchild kills) and
codex-reviewed.

## 2. Fixed-port driver-stager collision  (`:8768` / `:8765`)
`tiered-isolation.bats` (local `stage_vm_driver`, ~22–41), `tier2-hardening-lockin.bats`
(~28–47), and `helpers.bash:stage_http_8765` (~165) serve a driver over a
**fixed, host-global, cross-user** HTTP port and copy it into a **shared** dir.
A worker silently reuses whatever (foreign/stale) server squats the port; stale
listeners from other users/runs were observed live. `stage_http_8765` even
`pkill`s sibling workers' servers. Fix: consolidate to ONE shared helper using
**free-port + content-validation** (the `silo-egress.bats` pattern: bind port 0,
`curl … | cmp` to prove it serves OUR file) — or base64 delivery (the
`qdlocker-faillock.bats` pattern). Remove the `pkill`; replace the per-file
local copies; add `curl -fsS`; stage into `BATS_TEST_TMPDIR` not the shared dir;
add a `teardown_file` to reap leaked servers. Other `:8765` consumers to migrate:
`admin-app-polish`, `app-launcher`, `broker-e2e`, `browser-9e-daemons`,
`compositor-shell`.
NOTE (codex): this collision was only ~10% of the two named failures — the
bigger cause is item 3.

## 3. Too-short tier2/tier5 in-driver readiness windows under contention
The tier-2 drivers use 15s/30s readiness windows (container start, inner
toplevel) that **overrun under 14-way CPU contention + cold first-run podman
image builds** (measured 5m43s wall for 17.7s CPU). This — not the port bug — was
codex's ~60% root cause of `tiered-isolation`/`tier2-hardening-lockin` failures.
Fix options (combine): pre-bake the tier-2 podman images into `baseweed-baked`
so first-run build isn't in the hot path; scale the in-driver waits
(build-aware); and/or cap concurrency for the heavy tier2/tier5 tests.

## 4. GUI golden image (phase 2)
Extend the per-run golden (item done for bats) to the `gui` gate. Build one gui
golden via the full `spin-test-vm-gui.sh` (incl. POSTBOOT labwc/permissions-gui
layering), then clones skip POSTBOOT. Needs: a `QCI_RUN_GOLDEN_BACKING` mode in
`spin-test-vm-gui.sh` (pass through to `spin-test-vm.sh`, skip POSTBOOT, verify
**labwc/wayland-0** not qdwin/wayland-1); a `gate_gui` hook
(`ensure_run_golden gui` before the session VM). `RUN_GOLDEN_GUI` is already
wired in `acquire_vm`. gui provisioning (~150–250 s/scenario) dominates the gui
gate, so this is a big win.

## 5. `clone-baseweed.sh` interrupt-leak hardening
`spin-test-vm.sh` now traps + reclaims a half-provisioned VM on failure/interrupt,
but `clone-baseweed.sh` itself has no self-cleanup trap: an INT/TERM during
`qemu-img create`/`virsh define`/start can leak a defined domain + overlay before
the caller's trap is installed. Add a `CLONE_OK`-gated EXIT/INT/TERM trap that
destroys+undefines the VM and removes the disk unless it reached the final name
echo. (Narrow window; `qci cleanup` reclaims leaks today.)

## Investigate (not a fix yet)
- **GUI agent 79% fail rate.** In the measured run, 45/57 gui agent scenarios
  failed (`rc=70`), most via the `claude --model haiku` runner. Likely the model
  can't drive these GUI tasks rather than real regressions. Triage a few
  `<run-dir>/gui/*.agent.log` and decide: bump the gui agent model, or adjust the
  scenario prompts/harness. Do this BEFORE treating gui failures as product bugs.

## Done (2026-06-13, this work)
- Parallelized `bats` (tiered ≤16, `QCI_JOBS`) and `gui` (≤8, `QCI_GUI_JOBS`) gates.
- Per-run golden image for `bats` (provision 247s→11s, VM-verified).
- Per-task timing (`timings.tsv`), unique VM names, HTTP-port race fix, 4 vCPUs,
  baked-image password reset + pin, removed dead `run-parallel.sh`.
