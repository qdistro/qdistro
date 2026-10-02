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

## Milestone A-iii (VM acceptance drivers, qci VM lane), 2026-10-02

Drivers (guest, root): `tests/integration/vm/s120-tier3s-headless.sh`,
`s121-tier3s-denied.sh`, `s122-tier3s-sigkill-cleanup.sh` (shared
`tier3s-guest-lib.sh`), run by `phase7-tier3s-{headless,denied,sigkill-cleanup}.bats`
through `ci/bin/qci bats --file …` on **fresh qci workers** cloned from the
run's golden (built WITHOUT `QDISTRO_TIER3S=1`). Each worker's setup
(`tier3s-guest-setup.sh`, host side `tier3s.bash`) installs the tested commit
with `QDISTRO_TIER3S=1`, provisions runsc offline from the sha512-checked host
cache, requires the installed probe to PASS and loads the workload image from
the host-cached OCI archive (`tier3s/cache-image-archive.sh`), asserting its
image ID. Every check prints one `PASS:`/`FAIL:` line; a driver exits 1 on any
FAIL and the bats wrapper also requires `[sNNN] N passes, 0 failures`.

Each run directory below holds, per bats file: the qci per-file log
(`phase7-tier3s-*.bats.log`: the TAP stream with every guest transcript
`# `-prefixed) and the scratch transcripts (`phase7-tier3s-*.scratch/
{t3s-setup,s12N}.log`, the files the line numbers point into), plus qci's
`results.tsv`, `manifest.txt`, `timings.tsv`, `created-vms.txt`.

### Runs

| Dir | Tested commit | qci run dir (host) | Result |
|---|---|---|---|
| `a-r1-qci/` | `bd565f612` (astra+fable r1 fixes; run from a pinned, detached worktree of that commit) | `/var/tmp/t3s-r1/qci-runs/bats-20261002T151149Z-2090267` | **3/3 PASS, the acceptance run** (`results.tsv`): s120 192 passes / 0 failures, s121 126/0, s122 142/0; each worker's setup 57/0. Workers `qci-bats-phase7-tier3s-{headless,denied,sigkill-cleanup}-261002-171447-*` were reaped by qci; the run's golden disk `qci-golden-bats-261002-171250-2112384-4652.qcow2` was removed by hand |
| `a-r1-dev/` | dev VM `t3sr1-261002-142725-106982-11270` (staged `793b7de1f`, then the `bd565f612` guest lib) | n/a (vm-exec) | development runs of the r1 fixes: `dev-s121.log` 121/5 = a **driver** bug (`silo_state` decoded busctl's text form; the new `observed_reason` carries a `'` that busctl escapes; the manager's state was Stopped), fixed in `bd565f612`; then s121 126/0, s120 192/0, s122 142/0; `dev-r4-repro.log` (dev-only `r4-repro.sh`): the sol r4 stale-record case on the real system, refused with the launch untouched (17/0). Destroyed after the run |
| `a-iii-qci-r5/` | `465dbc408` (run from a pinned, detached worktree of that commit) | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T112452Z-3145988` | 3/3 PASS, the A-iii acceptance run before the astra+fable r1 fixes; superseded by `a-r1-qci/` (`results.tsv`): s120 189 passes / 0 failures, s121 108/0, s122 126/0; each worker's setup 57/0. Workers `qci-bats-phase7-tier3s-{headless,denied,sigkill-cleanup}-261002-132735-*` were reaped by qci; the run's golden disk was removed by hand |
| `a-iii-qci-r4/` | `30ac7b819` | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T111348Z-3055033` | 3/3 PASS (s120 189/0, s121 108/0, s122 126/0) after the sol r2 fixes; superseded by r5 (sol r3 fix `465dbc408`: the label reap requires a valid, dead launch-unit label) |
| `a-iii-qci-r3/` | `0f10449f7` | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T110045Z-2959259` | 3/3 PASS (s120 189/0, s121 106/0, s122 126/0) after the sol r1 fixes; superseded by r4, r5 (sol r2 fixes `30ac7b819`: tri-state unit state in the reaper, record/dir listings through `qry`) |
| `a-iii-qci-r2/` | `3d924d726` | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T103923Z-2830933` | 3/3 PASS (s120 189/0, s121 103/0, s122 126/0) before the sol r1 fixes (`0f10449f7`: the reaper's BindsTo check, the oracles' query-failure handling); superseded by r3–r5 |
| `a-iii-qci-r1/` | `0d9ade612` | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T102945Z-2723488` | 2/3 PASS; s122 FAIL 106/2 = **finding**: on the launcher SIGKILL, `ExecStopPost`'s cleanup lost a race with `podman run --rm` (the container vanished between `exists` and `inspect`), so it preserved the record and the per-launch dir although sandbox, scope and container were gone (`phase7-tier3s-sigkill-cleanup.scratch/s122.log:18`–`35`). Fixed in `4fbeb43c7` (a failed inspect/stop is re-queried; only a definitive "absent" continues); the SIGKILL case now runs 3× (`3d924d726`). The failed worker (kept powered off by qci) was destroyed and undefined |
| `a-iii-dev/` | dev VM `tier3s-261002-115945-2329209-31246` | n/a (vm-exec) | development runs kept for two findings: `s122-dev1-reaper-findings.log` (podman 6 rejects `index .Labels` in a `ps` template, so `--reap-stale` failed whenever a labelled container existed; an orphan per-launch dir was left when the reap raced the scope's BindsTo stop; fixed in `007fab313`) and `s120-dev2-smoke-sigterm-race.log` (a driver bug: `podman stop` before the smoke installed its TERM trap; the drivers now wait for `SMOKE holding`) |

### Δ DONE bar → evidence (all in `a-r1-qci/`, tested commit `bd565f612`; `file:line`)

The r1 fixes (astra + fable round 1, the sol r4 P1 included) changed the
launch unit to `Type=notify`, the cleanup's ownership, locking and bounds,
and the drivers' assertions, so every line below points at the r1 run. The
A-iii run r5 table (`a-iii-qci-r5/`) is kept in git history (`8a18b7c8d`).

Abbreviations: `s120` = `phase7-tier3s-headless.scratch/s120.log`, `s121` =
`phase7-tier3s-denied.scratch/s121.log`, `s122` =
`phase7-tier3s-sigkill-cleanup.scratch/s122.log`, `setup` =
`phase7-tier3s-headless.scratch/t3s-setup.log` (the other two workers'
`t3s-setup.log` have the same lines).

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | every runtime process class in the recorded owning scope (recursive `cgroup.procs`) | PASS | `s120:41`–`49` (runuser, podman CLI, conmon, gofer, sandbox = `State.Pid`, fd-parking, 17 systrap stubs; 0 of the runsc-bundle processes outside; recorded sentry and conmon inside); every conmon and podman CLI of the launch host-wide inside (fable P3-5) `s120:50`–`51`; launch B `s120:173`–`180` |
| 2 | normal exit | PASS | `s120:22`–`36` |
| 2 | plain `podman stop`; plain `podman rm -f` | PASS | `s120:246`–`257`; `s120:259`–`269` |
| 2 | session-manager stop (`StopSilo`) | PASS | `s120:181`–`190` |
| 2 | launcher SIGKILL / service failure, 3× | PASS | `s122:18`–`61` |
| 2 | session-manager crash (SIGKILL) and restart | PASS | `s122:103`–`122`, `s122:123`–`141`: the old launch got a completed **stop job** (PID 1 journal fields, fable P3-4) `s122:120`, `:139` and the verified cleanup `:121`, `:140`; everything gone; relaunched with a fresh token whose record is `phase=running`, container running under it and scope live (astra 6) `s122:112`–`115`, `:131`–`134` |
| 2 | restart reconciliation after state loss: a live launch unit the manager never started; the same with its record removed; a live labelled container with no unit | PASS | `s122:148`–`158`; `s122:159`–`169`; `s122:171`–`175` |
| 2 | forced runtime failure while live (root replaced, then missing): an error, record + scope preserved, no false "no container"; restored root → complete teardown | PASS | `s120:213`–`232`; `s120:233`–`244` |
| O11 | a session-manager **stop** leaves no launch-owned process, scope, token dir or control dir | PASS | `s122:63`–`87` (two live launches, each stopped through the verified cleanup before the manager, then every absence check); the relaunch after it is a running launch `s122:89`–`96` |
| 3 | two concurrent launches; tearing one down preserves the other | PASS | `s120:169`–`193` |
| 4 | runtime identity (ΔA9); corroboration as INFO only | PASS | `s120:52`–`59` |
| 4 | state-root policy (ΔA1): plain `ps`/`ps --sync`/`stop`/`rm -f` reach the sandbox; missing or replaced root: a plain stop fails visibly, nothing minted, record and scope kept | PASS | `s120:60`–`63`, `s120:248`, `s120:261`; `s120:195`–`211` |
| ΔA8 | the control record's fields and modes | PASS | `s120:64`–`73` |
| 5 | broker denial (no rule = unknown, explicit deny; untemplated and templated) ⇒ no `podman run`, no activation record; the refused StartSilo **fails with the refusal and the silo reads Stopped without a StopSilo** (astra 4 / fable P2-1); the allow-rule start of the refused silo is a real retry; positive control sees both | PASS | fixture `s121:14`–`15`; oracle self-tests `s121:16`–`20`; denials `s121:26`–`101` (API outcome `:29`–`31`, `:47`–`49`, `:67`–`69`, `:85`–`87`); retry + control `s121:102`–`137` (`:104`–`105`) |
| 6 | hardened profiles (release, daily) refuse with a clear message at create, start, in the spawn (the direct `systemctl start` now fails too) and the probe; a probe failure refuses (StartSilo fails, Stopped); no fallback | PASS | `s121:139`–`184`; `s121:186`–`204` |
| 7 | tier-2 unit and static suites unchanged and passing; no `tier2/` file in the diff | PASS | `../tier2-suites-a-r1-host.log` (code = `bd565f612`): 0 `tier2/` files changed; unit 130 passed; static `bash -n` rc 0, `shellcheck -S warning -e SC1090` rc 0 under the documented baseline waiver |
| 8 | posture from the OCI spec and the running sandbox; ΔA5 image; `fchmodat2` path; each ΔA4 decision | PASS | spec `s120:95`–`116`; sandbox and image `s120:149`–`167` |
| O10 | the installer installs tier 3s only with `QDISTRO_TIER3S=1` (fresh worker: none before; none after a flag-less run; all after the flagged run) | PASS | `setup:6`–`17`, `setup:19`–`53` |
| lane | qci-lane provisioning: tested commit, runsc sha512, offline provision, probe PASS, image ID = manifest | PASS | `setup:8`–`103`; tested commit on `phase7-tier3s-*.bats.log:2` |
| r1 | `Type=notify`: StartSilo returns only once the launch is recorded running | PASS | `s120:40` |

### Host logs (astra+fable r1)

| Log | Tree | Result |
|---|---|---|
| `tier2-suites-a-r1-host.log` | code = `bd565f612` | DONE bar 7 `### RESULT: PASS` (as above); tier3s + session-manager + broker suites (context) 1371 passed, 1 skipped (the pre-existing real-root skip) |
| `mutate-guards-a-r1-host.log` | code = `bd565f612` | **132 mutations, 0 problems**: the 92 earlier ones (A3, A15, A20, R1, R2, R5–R7, R9 re-targeted at the r1 code) plus 40 r1 ones (R10–R35, S13–S18, A23–A26, L6–L8, U4); files restored byte-identical |

### Host logs (A-iii)

| Log | Tree | Result |
|---|---|---|
| `tier2-suites-a-iii-host.log` | code = `465dbc408` | DONE bar 7 (`### RESULT: PASS`): `tier2/` files changed vs `claude/tier3s`: 0; tier-2 unit suites (`test_tier2_spawn`, `test_tier2_snapshot_repos`, `test_podapp_launch`, `test_podapps_scan`, `test_silo_launch`, `test_spawn_common`) 130 passed; static `bash -n` rc 0, `shellcheck -S warning -e SC1090` rc 0 with the SC1090 baseline waiver, the unwaived output a single SC1090 identical to the `claude/tier3s` baseline; tier3s + session-manager + broker suites (context) 1323 passed, 1 skipped (the pre-existing real-root skip) |
| `mutate-guards-a-iii-host.log` | code = `465dbc408` | 92 mutations (29 Phase 0 + 26 A-i + 23 A-ii + 14 A-iii: I3–I5 installer opt-in O10, U2/U3 stop propagation O11, R1–R9 reaper/teardown findings from the VM, qci and sol r1–r3) all caught; files restored byte-identical |
