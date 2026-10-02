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
| `a-r3-qci/` | `1bec207b2` (astra+fable r3 fixes; run from a pinned, detached worktree of that commit) | `/var/tmp/t3s-qci-1bec207b2/ci/runs/bats-20261002T193045Z-66479` | **3/3 PASS, the acceptance run** (`results.tsv`): s120 196 passes / 0 failures, s121 155/0, s122 152/0; each worker's setup 59/0. Workers `qci-bats-phase7-tier3s-{headless,denied,sigkill-cleanup}-261002-213307-*` were reaped by qci; the run's golden disk `qci-golden-bats-261002-213103-73971-23307.qcow2` was removed by hand (nothing referenced it) |
| `a-r3-dev/` | dev VM `t3sr3-261002-210307-2819389-18202` (staged `1bec207b2`) | n/a (vm-exec) | development run of the r3 fix: `dev-prov2.log` (dev-only `dev-prov.sh`, **65/0**): the `PMRC=<rc>` provenance protocol end-to-end on real systemd 261 / podman 6.0.2 — a refusing `systemd-run` and a failing `runuser` each read as a failed query (cleanup rc 4, launch/record/container/scope preserved), a verdict sharing its output is a failed query, the real chain tears a live launch down completely, a genuine absent tears its record down, and the manager's verdict on the real `runuser→env→sh→podman` chain gives present/absent/None as designed. `dev-prov.log` is the first attempt (three dev-script bugs, none product); `dev-setup.log` 54/0 staged the tested commit. Destroyed after the runs |
| `a-r2-qci/` | `92f7d07c0` (astra+fable r2 fixes; run from a pinned, detached worktree of that commit) | `/var/tmp/t3s-r2/qci-runs/bats-20261002T164956Z-3634433` | 3/3 PASS, the acceptance run before the astra+fable r3 fixes; superseded by `a-r3-qci/` (`results.tsv`): s120 196 passes / 0 failures, s121 155/0, s122 152/0; each worker's setup 59/0. Workers `qci-bats-phase7-tier3s-{headless,denied,sigkill-cleanup}-261002-185253-*` were reaped by qci; the run's golden disk `qci-golden-bats-261002-185055-3640111-31651.qcow2` was removed by hand (nothing referenced it) |
| `a-r2-dev/` | dev VM `t3sr2-261002-180333-3108422-16926` (staged `aa16c9aa8`, then the s121 forge fix that became `92f7d07c0`) | n/a (vm-exec) | development runs of the r2 fixes: `dev-kill.log` (dev-only `dev-kill.sh`, 30/0): the installed cleanup against a podman that starts a TERM-ignoring helper in a new session and hangs, bind-mounted in a private mount namespace: a timed-out call, a SIGTERM to the cleanup and a SIGKILL of the cleanup's own scope each leave nothing behind (the last by systemd's `RuntimeMaxSec` stop of the call scope, journal lines in the log); `dev-s121a-excerpt.log`: the first forged-READY step moved nothing (wrong cgroup path for a template instance) and its own positive control failed it (152/3); `dev-s121b.log` 155/0 after the fix; `dev-s120.log` 196/0, `dev-s122.log` 152/0. Destroyed after the runs |
| `a-r1-qci/` | `bd565f612` (astra+fable r1 fixes; run from a pinned, detached worktree of that commit) | `/var/tmp/t3s-r1/qci-runs/bats-20261002T151149Z-2090267` | 3/3 PASS, the acceptance run before the astra+fable r2 fixes; superseded by `a-r2-qci/` (`results.tsv`): s120 192 passes / 0 failures, s121 126/0, s122 142/0; each worker's setup 57/0. Workers `qci-bats-phase7-tier3s-{headless,denied,sigkill-cleanup}-261002-171447-*` were reaped by qci; the run's golden disk `qci-golden-bats-261002-171250-2112384-4652.qcow2` was removed by hand |
| `a-r1-dev/` | dev VM `t3sr1-261002-142725-106982-11270` (staged `793b7de1f`, then the `bd565f612` guest lib) | n/a (vm-exec) | development runs of the r1 fixes: `dev-s121.log` 121/5 = a **driver** bug (`silo_state` decoded busctl's text form; the new `observed_reason` carries a `'` that busctl escapes; the manager's state was Stopped), fixed in `bd565f612`; then s121 126/0, s120 192/0, s122 142/0; `dev-r4-repro.log` (dev-only `r4-repro.sh`): the sol r4 stale-record case on the real system, refused with the launch untouched (17/0). Destroyed after the run |
| `a-iii-qci-r5/` | `465dbc408` (run from a pinned, detached worktree of that commit) | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T112452Z-3145988` | 3/3 PASS, the A-iii acceptance run before the astra+fable r1 fixes; superseded by `a-r1-qci/` (`results.tsv`): s120 189 passes / 0 failures, s121 108/0, s122 126/0; each worker's setup 57/0. Workers `qci-bats-phase7-tier3s-{headless,denied,sigkill-cleanup}-261002-132735-*` were reaped by qci; the run's golden disk was removed by hand |
| `a-iii-qci-r4/` | `30ac7b819` | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T111348Z-3055033` | 3/3 PASS (s120 189/0, s121 108/0, s122 126/0) after the sol r2 fixes; superseded by r5 (sol r3 fix `465dbc408`: the label reap requires a valid, dead launch-unit label) |
| `a-iii-qci-r3/` | `0f10449f7` | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T110045Z-2959259` | 3/3 PASS (s120 189/0, s121 106/0, s122 126/0) after the sol r1 fixes; superseded by r4, r5 (sol r2 fixes `30ac7b819`: tri-state unit state in the reaper, record/dir listings through `qry`) |
| `a-iii-qci-r2/` | `3d924d726` | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T103923Z-2830933` | 3/3 PASS (s120 189/0, s121 103/0, s122 126/0) before the sol r1 fixes (`0f10449f7`: the reaper's BindsTo check, the oracles' query-failure handling); superseded by r3–r5 |
| `a-iii-qci-r1/` | `0d9ade612` | `/var/tmp/t3s-aiii/qci-runs/bats-20261002T102945Z-2723488` | 2/3 PASS; s122 FAIL 106/2 = **finding**: on the launcher SIGKILL, `ExecStopPost`'s cleanup lost a race with `podman run --rm` (the container vanished between `exists` and `inspect`), so it preserved the record and the per-launch dir although sandbox, scope and container were gone (`phase7-tier3s-sigkill-cleanup.scratch/s122.log:18`–`35`). Fixed in `4fbeb43c7` (a failed inspect/stop is re-queried; only a definitive "absent" continues); the SIGKILL case now runs 3× (`3d924d726`). The failed worker (kept powered off by qci) was destroyed and undefined |
| `a-iii-dev/` | dev VM `tier3s-261002-115945-2329209-31246` | n/a (vm-exec) | development runs kept for two findings: `s122-dev1-reaper-findings.log` (podman 6 rejects `index .Labels` in a `ps` template, so `--reap-stale` failed whenever a labelled container existed; an orphan per-launch dir was left when the reap raced the scope's BindsTo stop; fixed in `007fab313`) and `s120-dev2-smoke-sigterm-race.log` (a driver bug: `podman stop` before the smoke installed its TERM trap; the drivers now wait for `SMOKE holding`) |

### Δ DONE bar → evidence (all in `a-r3-qci/`, tested commit `1bec207b2`; `file:line`)

The r3 fixes (astra + fable round 3) changed the cleanup's and the
manager's existence-verdict protocol, NSS lookup bounds, call-output caps
and the `.call-*` sweep — none of the drivers — so every line below points
at the r3 run. All 45 r2 citations were re-checked against the r3
transcripts (masked for tokens, pids and timestamps): 40 carry over
unchanged; five s121 citations shifted +1 (one extra event-log line) and
were re-pointed. The r1 lines were originally re-mapped mechanically
(difflib over the transcripts with tokens, pids and hashes masked) and
spot-checked. The A-iii run r5 table (`a-iii-qci-r5/`) is kept in git
history (`8a18b7c8d`).

Abbreviations: `s120` = `phase7-tier3s-headless.scratch/s120.log`, `s121` =
`phase7-tier3s-denied.scratch/s121.log`, `s122` =
`phase7-tier3s-sigkill-cleanup.scratch/s122.log`, `setup` =
`phase7-tier3s-headless.scratch/t3s-setup.log` (the other two workers'
`t3s-setup.log` have the same lines).

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | every runtime process class in the recorded owning scope (recursive `cgroup.procs`) | PASS | `s120:43`–`51` (runuser, podman CLI, conmon, gofer, sandbox = `State.Pid`, fd-parking, 17 systrap stubs; 0 of the runsc-bundle processes outside; recorded sentry and conmon inside); every conmon and podman CLI of the launch host-wide inside (fable P3-5) `s120:52`–`53`; launch B `s120:175`–`182` |
| 2 | normal exit | PASS | `s120:24`–`38` |
| 2 | plain `podman stop`; plain `podman rm -f` | PASS | `s120:248`–`259`; `s120:261`–`271` |
| 2 | session-manager stop (`StopSilo`) | PASS | `s120:183`–`192` |
| 2 | launcher SIGKILL / service failure, 3× | PASS | `s122:20`–`62` |
| 2 | session-manager crash (SIGKILL) and restart | PASS | `s122:108`–`127`, `s122:128`–`146`: the old launch got a completed **stop job** (PID 1 journal fields, fable P3-4) `s122:125`, `:144` and the verified cleanup `:126`, `:145`; everything gone; relaunched with a fresh token whose record is `phase=running`, container running under it and scope live (astra 6) `s122:117`–`120`, `:136`–`139` |
| 2 | restart reconciliation after state loss: a live launch unit the manager never started; the same with its record removed; a live labelled container with no unit | PASS | `s122:155`–`165`; `s122:166`–`176`; `s122:178`–`182` |
| 2 | forced runtime failure while live (root replaced, then missing): an error, record + scope preserved, no false "no container"; restored root → complete teardown | PASS | `s120:215`–`234`; `s120:235`–`246` |
| O11 | a session-manager **stop** leaves no launch-owned process, scope, token dir or control dir | PASS | `s122:64`–`88` (two live launches, each stopped through the verified cleanup before the manager, then every absence check); the relaunch after it is a running launch `s122:92`–`99` |
| 3 | two concurrent launches; tearing one down preserves the other | PASS | `s120:171`–`195` |
| 4 | runtime identity (ΔA9); corroboration as INFO only | PASS | `s120:54`–`61` |
| 4 | state-root policy (ΔA1): plain `ps`/`ps --sync`/`stop`/`rm -f` reach the sandbox; missing or replaced root: a plain stop fails visibly, nothing minted, record and scope kept | PASS | `s120:62`–`65`, `s120:250`, `s120:263`; `s120:197`–`213` |
| ΔA8 | the control record's fields and modes | PASS | `s120:66`–`75` |
| 5 | broker denial (no rule = unknown, explicit deny; untemplated and templated) ⇒ no `podman run`, no activation record; the refused StartSilo **fails with the refusal and the silo reads Stopped without a StopSilo** (astra 4 / fable P2-1); the allow-rule start of the refused silo is a real retry; positive control sees both | PASS | fixture `s121:16`–`17`; oracle self-tests `s121:18`–`22`; denials `s121:28`–`103` (API outcome `:31`–`33`, `:49`–`51`, `:69`–`71`, `:87`–`89`); retry + control `s121:104`–`139` (`:106`–`107`) |
| 6 | hardened profiles (release, daily) refuse with a clear message at create, start, in the spawn (the direct `systemctl start` now fails too) and the probe; a probe failure refuses (StartSilo fails, Stopped); no fallback | PASS | `s121:144`–`189`; `s121:191`–`209` |
| 7 | tier-2 unit and static suites unchanged and passing; no `tier2/` file in the diff | PASS | `../tier2-suites-a-r2-host.log` (code = `92f7d07c0`): 0 `tier2/` files changed; unit 130 passed; static `bash -n` rc 0, `shellcheck -S warning -e SC1090` rc 0 under the documented baseline waiver |
| 8 | posture from the OCI spec and the running sandbox; ΔA5 image; `fchmodat2` path; each ΔA4 decision | PASS | spec `s120:97`–`118`; sandbox and image `s120:151`–`169` |
| O10 | the installer installs tier 3s only with `QDISTRO_TIER3S=1` (fresh worker: none before; none after a flag-less run; all after the flagged run) | PASS | `setup:6`–`17`, `setup:19`–`53` |
| lane | qci-lane provisioning: tested commit, runsc sha512, offline provision, probe PASS, image ID = manifest | PASS | `setup:8`–`103`; tested commit on `phase7-tier3s-*.bats.log:2` |
| r1 | `Type=notify`: StartSilo returns only once the launch is recorded running | PASS | `s120:42` |
| r2 | `NotifyAccess=main`: an admin process inside the launch unit's cgroup that sends `READY=1` to systemd's socket leaves the start job running; the launch then runs on the spawn's own READY; positive control: with a runtime `NotifyAccess=all` drop-in the same forgery completes the start (astra r2 #4) | PASS | `s121:212`, `:214`–`220`; control `s121:221`–`228`; a workload that ends at once also starts under `main` `s121:106`, path `:133` (INFO) |
| r2 | no cleanup call scope (`qdistro-t3s-call-*.scope`) and no cleanup work dir survive any teardown path (astra r2 #2) | PASS | `s120:14`–`15`, `:282`–`283`; `s121:141`–`142`, `:235`–`236`, `:247`–`248`; `s122:13`–`14`, `:89`–`90`, `:105`–`106`, `:152`–`153`, `:192`–`193`; `setup:117`–`118` |
| r3 | existence verdicts have in-call provenance (the `PMRC=<rc>` line): a refused `StartTransientUnit` or a `runuser` failure is a failed query — nonzero rc, record/scope/container preserved — never "absent"; a genuine `PMRC=1` still tears down (astra+fable r3 P1) | PASS (dev VM, not a DONE-bar driver) | `../a-r3-dev/dev-prov2.log` (65/0) |
| r2 | call supervision on real systemd/cgroups (timeout, SIGTERM, SIGKILL of the supervisor) | PASS (dev VM, not a DONE-bar driver) | `../a-r2-dev/dev-kill.log` |

### Host logs (astra+fable r3)

| Log | Tree | Result |
|---|---|---|
| `affected-suites-a-r3-host.log` | code = `1bec207b2` | tier3s spawn/probe/provision + session-manager tier3s/base/bounds + silo observation + broker suites: **747 passed, 1 skipped** (the pre-existing real-root skip) |
| `unit-all-a-r3-host.log` | code = `1bec207b2` | full `tests/unit`: **6801 passed, 8 skipped** |
| `tier2-suites-a-r3-host.log` | code = `1bec207b2` | DONE bar 7: 0 `tier2/` files changed; tier-2 unit 130 passed; `shellcheck -S warning` of the cleanup, the spawn and the scope helper rc 0 |
| `mutate-guards-a-r3-host.log` | code = `1bec207b2` | **155 mutations, 0 problems**: the 149 earlier ones plus 6 r3 ones (R49–R54: verdict provenance, whole-output requirement, NSS bound, output cap, `$PROC` sweep, the same in the manager); files restored byte-identical |

### Host logs (astra+fable r2)

| Log | Tree | Result |
|---|---|---|
| `tier2-suites-a-r2-host.log` | code = `92f7d07c0` | DONE bar 7 `### RESULT: PASS`: 0 `tier2/` files changed; tier-2 unit 130 passed; static `bash -n` rc 0, `shellcheck -S warning -e SC1090` rc 0 under the documented waiver (single SC1090, identical to the baseline); `shellcheck -S warning` of the cleanup and the spawn rc 0; tier3s + session-manager + broker suites (context) 1392 passed, 1 skipped (the pre-existing real-root skip) |
| `mutate-guards-a-r2-host.log` | code = `92f7d07c0` | **149 mutations, 0 problems**: the 132 earlier ones (R1, R2, R6–R8, R11, R18, R20, R23, R24, R26 re-targeted at the r2 code) plus 17 r2 ones (R36–R48, A27–A29, U5); files restored byte-identical |

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
