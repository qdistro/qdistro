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
