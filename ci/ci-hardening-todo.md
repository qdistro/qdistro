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

## 2. Fixed-port driver-stager collision  (`:8768` / `:8765`)  — DONE 2026-06-13
`tiered-isolation.bats` (local `stage_vm_driver`), `tier2-hardening-lockin.bats`,
and `helpers.bash:stage_http_8765` served drivers over a **fixed, host-global,
cross-user** HTTP port and copied them into a **shared** dir; a worker silently
reused whatever squatted the port and `stage_http_8765` even `pkill`ed sibling
workers' servers. Consolidated to ONE shared `stage_vm_driver` + `reap_vm_drivers`
in `helpers.bash`: **free port** (`python3 bind(0)`) **+ content-validation**
(`curl -fsS | cmp` proves the port serves OUR file, else retries a new port — 5
attempts, closing the bind(0)→http.server TOCTOU codex flagged), private per-file
serving dir under `BATS_FILE_TMPDIR` (not the shared dir), `QDISTRO_BATS_HTTP_PORT`
exported for the VM fetch, PIDs reaped via `teardown_file`. Migrated all 9
consumers (tiered-isolation ×35 sites, tier2-hardening-lockin, broker-e2e,
removable-media, app-launcher, admin-app-polish, compositor-shell,
browser-9e-daemons, and silo-egress — whose local free-port helper was folded in).
Removed the `pkill`; switched fetches to `curl -fsS`. VM-verified (broker-e2e
gate green) and codex-reviewed.
NOTE (codex): this collision was only ~10% of the two named failures — the
bigger cause is item 3.

## 3. Too-short tier2/tier5 in-driver readiness windows under contention
The tier-2 drivers use 15s/30s readiness windows (container start, inner
toplevel) that **overran under 14-way CPU contention + cold first-run podman
image builds** (measured 5m43s wall for 17.7s CPU) — codex's ~60% root cause.

**DONE (2026-06-13): pre-bake the tier-2 podman images (the dominant cause).**
Removed the cold `podman build` (Tumbleweed pull + zypper install) from the
worker hot path: `fresh-vm-bootstrap.sh` §8 gains a `QDISTRO_BUILD_TIER2_IMAGES`
gate that builds all 3 workload images (weston-terminal, text-viewer,
url-preview) as admin (rootless), verifies the tags, FATAL on failure (no silent
fallback to the flaky path). `spin-test-vm.sh` forwards the flag; `qci
ensure_run_golden` sets it for the **bats golden only**, so every cloned worker
inherits the images as shared CoW backing. Drivers keep their on-demand build as
a fallback for non-prebaked VMs. VM-verified: golden pre-built + verified all 3
tags; a cloned worker had the images and s40 SKIPPED its cold build. Also fixed a
latent qci bug (`${#RUN_GOLDEN_DISKS[@]:-0}` → `${#RUN_GOLDEN_DISKS[@]}`; the bad
substitution meant `GOLDEN_PRESERVE` never set → a preserved failed VM's golden
was deleted, breaking triage).

**DEFERRED — readiness-window scaling (B1) / heavy-file concurrency cap (c).**
With the cold build gone, there's no fresh evidence the 15s/30s post-spawn
windows are still too tight: the validating run's s40 actually failed on a
**pre-existing broker-policy product bug** (`spawn-tier2: broker has no allow
rule for weston-terminal`, decision=unknown — a bake/product gap, out of scope),
not a timeout. Per codex: ship the prebake alone; revisit window scaling only
after the product blockers clear and the tier2/tier5 files can be re-measured
under normal `qci` contention. If they still flake without cold builds, do B1
(raise 15s→45s / 30s→60s in s32/s33/s34/s40/s59 ×2 copies) **with validation**,
or prefer (c) capping concurrency for the genuinely heavy files if the data shows
contention rather than service-startup variance.

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
