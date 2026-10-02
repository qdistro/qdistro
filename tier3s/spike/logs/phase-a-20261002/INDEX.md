# Phase A (milestone A-i) feasibility evidence, 2026-10-02

Dev test VM `tier3s-261002-105112-1722066-19303` (spun from this worktree,
dev profile, Tumbleweed 20260929, podman 6.0.2, systemd 261), runsc
20260928.0 provisioned offline from the sha512-checked host cache. Host
driver `tier3s/spike/run-phase-a-feasibility.sh`, guest script
`tier3s/spike/phase-a-feasibility.sh` (asserting: a step's exit status is its
BAD count), workload `registry.opensuse.org/opensuse/tumbleweed:20260929`
running `sleep`. These checks back the decisions in `tier3s/CONTRACT.md`
(D-A1, D-A3b, D-A4); they are **not** the A-iii acceptance drivers (no spawn
script, no session manager).

| Dir / log | Staged commit | Result |
|---|---|---|
| `feasibility-r1/` | `69e07eb32` | 10 OK. 11: the stop fails and the sandbox stays, but the wrapper's message never reaches `podman stop`'s stderr (fix: journal via logger). 20/21: the scope helper refused every launch (its own `$(...)` forks were in `cgroup.procs`; fix: mapfile); the BAD lines after that are a script that did not stop on an inactive scope. 21 was moved mid-write |
| `feasibility-r2/00`–`11` | `67f480bc1` | 0 BAD. D-A1: plain `ps`/`stop`/`rm` reach the fixed root; a moved or replaced root makes a plain stop fail (rc 125), the Sentry stays alive, no root is minted, the journal has `tier3s-runsc: state root … is missing` |
| `feasibility-r2/20`, `21` | `67f480bc1` | D-A3b placement 9/9 and root-owned limits; the BAD lines are a cmdline classifier for fd-parking and a wait that returned while the scope was `deactivating` |
| `feasibility/20` | `ad4e575ba` | 0 BAD. `systemctl stop` of the launch service: scope active during ExecStopPost (Before=), inactive ~2 s later; every process class in the scope |
| `feasibility/21` | `ad4e575ba` | 1 BAD = **finding**: SIGKILL of the launch main pid → BindsTo stops the scope concurrently with ExecStopPost (no stop job, so Before= orders nothing); everything gone ~2 s later |
| `feasibility/30`, `31` | (ad hoc, profile from `41d98aa51` rendering) | D-A4: `OCI seccomp: ignoring syscall "fchmodat2"` — the ALLOW was inert, `chmod -h` EPERM, `Syscall 452: denied by seccomp`; plain `chmod` OK; `llistxattr` and `syslog` ALLOWs effective (`ls -l` clean, dmesg banner); `utimensat` (280), `linkat`/`symlinkat` (265/266) and `fadvise64` (221) denied by the tier-2 base |

| `feasibility/40` | `ef56049ea` | D-A5: `make-tier3s-image.sh --oci-archive` builds `qdistro/tier3s-headless-smoke` in the VM on pin 20260929 (label checked, IMAGE_ID/DIGEST and archive sha256 printed); its smoke output under runsc with the shipped flags: snapshot 20260929, gVisor kernel/dmesg, uid 1000 passwd entry and HOME, `C.UTF-8` charmap UTF-8, `/run/user/1000` and `/home/admin/.cache` 1000:1000 0700, plain chmod rc 0, `chmod -h` rc 1, `ls -l` clean, loopback only |
| `mutate-guards-host.log` | `fbf2a57c8` tree | host: 55 mutations (29 Phase 0 + 26 Phase A: gate order A1–A3, control-dir location A4/A5, …) all caught; files restored byte-identical |

Note on 30: that profile still listed `fchmodat2` as ALLOW (the render
before the decision changed); 31 is the same profile. The checked-in
`headless-smoke.json` decides DENY.

## Milestone A-ii (session manager, broker, units, installer), 2026-10-02

Host driver `tier3s/spike/run-phase-a-ii-smoke.sh`, guest script
`tier3s/spike/phase-a-ii-smoke.sh` (asserting: a step's exit status is its BAD
count). A smoke, not the A-iii DONE-bar drivers.

| Dir / log | Tree | Result |
|---|---|---|
| `a-ii-smoke-r2/` | VM `tier3s-261002-114938-2289239-8644`, spun **fresh** from `04593b87a`, which is also the staged HEAD (`00-host.log`); destroyed after the run | **0 BAD in all 9 steps.** `01`: the spin's bootstrap installer left every CONTRACT §1 artifact (+ the session manager, its bus policy, the broker) byte-identical to HEAD, root-owned with the contract modes, tmpfiles dirs in place, runsc **not** installed, `qdistro-tier3s-silo@smoke.service` loaded from `/etc/systemd/system/` with `ExecMainStartTimestampMonotonic=0` (never started), the running daemon started after its file was written and serves `CreateTier3sSilo ssss`. `02`: the same after re-running the installer from the staged HEAD. `03`: offline provision from the pin-checked tarball, the INSTALLED probe PASS (state_root PASS). `04`: image on the snapshot pin. `05`: no rule → `unknown`; rule → `allow`. `10`: `CreateTier3sSilo` → `StartSilo` → the smoke ran under gVisor, exited 0, `torn down`, `StopSilo` → Stopped; FreezeSilo refused; no record/scope/container/runsc process. `11`: argv `--hold` via silos.yaml + manager restart; live launch: record unit/container/phase=running, token = container label, runtime = the wrapper, scope active with all 20 runsc-bundle processes inside and 0 outside, Sentry exe = pin; FreezeSilo refused and the silo stays Active; `StopSilo` → `SMOKE term`, torn down, unit Result=success, scope inactive, nothing left. `12`: DeleteSilo, nothing left |
| `a-ii-smoke-r1/` | VM `tier3s-261002-113918-2224374-3622` (fresh spin, staged `2748f778e`; destroyed) | 00–05 0 BAD; 10–12 launch up and torn down, BADs = driver bugs: the workload output is journalled under the owning **scope** (podman/conmon), not the launch unit, and runsc's shared empty `null-netns` file in the state root was counted as container state (fixed `25664540f`, which also silences the cleanup's `/proc/<pid>/stat` redirect noise) |
| `mutate-guards-a-ii-host.log` | `04593b87a` | 78 mutations (29 Phase 0 + 26 A-i + 23 A-ii) all caught; files restored byte-identical |
| `tier2-suites-a-ii-host.log` | `04593b87a` | tier-2 unit suites 212 passed; `git diff claude/tier3s..HEAD -- tier2 tier3 qdshell qdwin` empty; `shellcheck -S warning tier2/*.sh` reports one SC1090 in an unchanged file (pre-existing) |
