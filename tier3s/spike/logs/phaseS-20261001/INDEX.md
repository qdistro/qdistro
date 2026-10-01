# Phase S evidence — 2026-10-01, VM tier3s-261001-221428-2070491-12685 (dev profile)

All transcripts are `tier3s/spike/vmlog.sh` captures of `scripts/vm/vm-exec`.
Each one starts with the exact command, ends with `### exit=<rc>`, and every
command in it ran inside the VM. Logs 00–40 come from one run of
`tier3s/spike/run-phaseS.sh` at commit `8270755f2`. `00-stage-src.log` shows
the guest sha256 of every spike script. Log 34 was added afterwards, at
commit `daa9c5e71`, and logs 35–37 at `ce77d3f6e` (sol r1). Each one's own sha256 line shows `s3-cgroups.sh`.
Screenshots are `virsh` captures (`vm-gui screenshot-fresh`), graded by
opening the PNG, not by OCR.

| Log | What | Verdict |
|---|---|---|
| 00-stage-src.log | source staged from the host (`git archive`); script hashes; `runsc --version`; Phase 0 probe re-run = PASS; `smoke.json` equals a fresh render of tier-2 `weston-terminal.json` + `syslog` | info / PASS |
| 01-stage-image.log | offline image `localhost/tier3s-spike/tw-terminals:20260929` (id `2fc93acf…`) from the closure of 284 installed VM packages, listed NVRA at the end; foot set re-verified (sha256 + `rpm -K`) | info |
| 10-s1-headless-hello.log | step 1: **A0** the exact 03 command fails (`mkdir /var/run/runsc: permission denied`, rc 126); **A** same + `--runtime-flag=root=/run/user/1000/runsc` → gVisor kernel banner, `dmesg --syslog`, uid 1000, tmpfs `/run/user/1000` 1000:1000 0700, loopback-only routes, rc 0; **B** inspect/config.json/process tree/cgroups/uid maps/pin-by-sha512; **B2** plain `podman stop` (no tier3s flags) rc 125 and the sandbox keeps running; **C** errno table runsc vs runc vs crun; **C2** `ls` EPERM = `llistxattr` (195) denied by seccomp | PASS (with the root-flag change) |
| s1-artifacts/ | `config.json` (podman-emitted OCI spec), `proctree.txt`, `runsc-debug/` (create/boot/gofer/start/kill/delete debug logs), `strace-ls/` | evidence |
| 20-s2-wayland-info.log | step 2a: `waypipe server -- wayland-info` in the sandbox through the bridge, rc 0, full global list (from qdwin's `wayland-1`) appended | PASS |
| 21-s2-negative-no-host-uds.log | the same command without `--runtime-flag=host-uds=open` gives `ECONNREFUSED` on `link.sock`: a configuration cause, not a gVisor incompatibility | PASS (negative) |
| 22-s2-weston-terminal.log | step 2b: start, status (podman inspect = tier3s runtime, conmon tree, `runsc ps` inside = waypipe / weston-terminal / bash, host waypipe client unit, seccomp denials 122/123/221), stop (no leftovers) | PASS |
| 23-s2-foot.log | step 2c: the same for foot (`runsc ps`: waypipe / foot / bash) | PASS |
| screens/19-desktop-before.png | unlocked desktop, no tier3s window | baseline |
| screens/22-weston-terminal.png, 22-weston-terminal-input.png | weston-terminal window, taskbar entry `[tier3s] Wayland…`; banner shows `Linux version 4.19.0-gvisor` and gVisor dmesg; typed command echoes `typed-into-weston-terminal`, `uname -r` = `4.19.0-gvisor`, no `/dev/dri` | PASS (viewed) |
| screens/22-weston-terminal-after-stop.png | window gone after stop | PASS (viewed) |
| screens/23-foot.png, 23-foot-input.png, 23-foot-after-stop.png | the same for foot (taskbar `[tier3s] foot`) | PASS (viewed) |
| s2-{info,weston-terminal,foot}-artifacts/ | runsc debug logs, waypipe client log, sandbox stdout/stderr, wayland-info output | evidence |
| 30-s3-root-unit.log | 03 step 3 as written: root unit (MemoryMax=1G, TasksMax=512) → runuser → podman: **0/7** sandbox processes in the unit; podman CLI, conmon and runsc land in user@1000 scopes | finding |
| 31-s3-parent-root.log | (a) root scope with limits, delegated (chowned) to admin, podman `--cgroup-manager=cgroupfs --cgroup-parent=<scope>/sandbox`: **7/7** inside, scope keeps memory.max=1G and pids.max=512. The `sandbox` child stays empty: `--cgroup-parent` only sets the OCI `cgroupsPath`, which runsc ignores under `--ignore-cgroups` | contains all |
| 34-s3-scope-plain.log | control for (a): the same root scope without delegation and without `--cgroup-parent` gives **0/7**, because podman moves itself to `user@1000…/podman-<pid>.scope`. So (a) works because admin can write the scope, not because of the parent flag | finding |
| 32-s3-parent-user.log | (a′) systemd manager `--cgroup-parent=t3sspike.slice` (user manager): **7/7**, but only `pids` is delegated to user@1000 and no limit was set. The printed argv omits the `-d` the script appended before running it (print order fixed after the run) | contains all |
| 33-s3-split.log | (b) `systemd-run --user --scope -p Delegate=yes` → `podman run --cgroups=split`: **7/7** in `<scope>/runtime` (podman CLI included); controllers: pids only | contains all |
| 35-s3-deleg-noparent.log | one-variable control (sol r1): (a) minus `--cgroup-parent` only gives **7/7** | finding |
| 36-s3-nodeleg-parent.log | one-variable control (sol r1): (a) minus the chown only (scope root-owned, flag kept) gives **0/7**; podman CLI and conmon go to `user@1000…/podman-<pid>.scope` | finding |
| 37-s3-parent-root-rerun.log | (a) re-run with the toggled script that produced 35/36: **7/7**, the same as 31 | contains all |
| 40-final-state.log | after the run: no containers, no runsc/conmon/waypipe processes, no `t3s-*` units | PASS |

## attempts/ (kept because they record a finding)

| File | What |
|---|---|
| 10a-s1-attempt1-runsc-root-denied.log | first step-1 run: `--rm` + exact 03 command fails on `/var/run/runsc`; diagnosis in `runsc/config/flags.go` `DefaultRootDir` (pinned tag) |
| 23a-s2-foot-attempt1-image-locale.log, 24a-foot-attempt1-image-locale.png | first foot run: the window rendered through the bridge but foot printed `invalid locale` (the image lacked `glibc-locale-base`); the same image under runc lacks a UTF-8 locale too, so this is an image defect, not gVisor; fixed in `stage-image.sh` seeds |
| 32a-s3-parent-user-dashed-slice-trial.log | trial run (scratch, before the final run) with `--cgroup-parent=t3s-s3.slice`: conmon landed in `…/t3s.slice/t3s-s3.slice/…` (systemd dash nesting). Its VERDICT line reads 0/7 only because the script's target guessed the flat path; the per-process cgroups show all 7 in the nested slice. The final 32 uses `t3sspike.slice` |
| 01-stage-image-run0-foot-rpm-install.log | the first image stage, the only run that actually installed the six foot RPMs into the VM (later runs say "already installed") |

Superseded intermediate runs (identical in substance) were dropped from the
tree; they are in the branch history (`git log -- tier3s/spike/logs`). One
earlier full driver run was discarded because qdlocker
idle-locked the session mid-run and its screenshots show the lock screen.
`run-phaseS.sh` now checks qdwin's lock state before every GUI step.
