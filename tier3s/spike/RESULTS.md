# tier3s — Phase 0 + Phase S results and Phase A/B handoff

This is the in-tree, self-contained record of the tier-3s (gVisor `runsc`)
feasibility work: what was run, what it showed, where the evidence is, what
the evidence does **not** show, the deviations from the rules the work ran
under, and what Phase A/B must do. Everything it cites is in this tree
(`tier3s/spike/logs/…`). The private planning tracker (`todo/paravirt/` in
the qdistro workspace: plan `03`, kickoff `04`, write-up `05`, reviews) is
optional provenance; nothing here needs it.

Status: **Experimental, dev profile only, spike only.** Nothing is wired into
the image, kiwi config or any installer; nothing selects tier 3s
automatically and nothing falls back from it (explicit launch, no fallback).
No KVM claims: `--platform=systrap` throughout. Numbers are observations,
not acceptance criteria.

## Ground rules the work ran under

- Upstream static `runsc`, pinned (`tier3s/RUNSC_RELEASE`, release
  **20260928.0**), optional and on demand, never in the image.
- Dev profile only; the hardened profile is out of scope and `probe.sh`
  refuses it.
- Every build/install/podman/runsc step inside a libvirt test VM; the host
  edits, runs git, `virsh`, `vm-exec` and codex reviews only.
- The sandbox gets no network (`--network=none` + `--runtime-flag=network=none`).

### Deviations (disclosed, not retro-fixed)

1. **One host `runsc --version`.** While computing the per-file hashes for
   the pin on 2026-10-01, the extracted `runsc` was executed once on the host
   to read its version. It started no sandbox, but it is a literal breach of
   "nothing runs under runsc on the host" and is recorded in
   `logs/phase0-20261001/INDEX.md`. It cannot be undone by re-running in the
   VM; every later `runsc` execution ran in the VM.
2. **Review model.** The kickoff asked for codex `gpt-6.1-sol` reviews; that
   model was rejected for the account, so the Phase 0 and Phase S "sol"
   gates ran on **`gpt-6-sol`**. The milestone and full-branch reviews ran
   on `gpt-6-astra` as specified.
3. **Who ran what in the VM.** Every *sandbox* step (podman with the tier3s
   runtime) ran as admin (uid 1000, the dev direct-admin lane). Provisioning,
   the probe and the installed `runsc --version` (Phase S log `00`) ran as
   guest root.

## Phase 0 — provisioning and the prerequisite screen

Files: `RUNSC_RELEASE` (pin), `provision-runsc.sh`, `tier3s-runsc`
(wrapper), `probe.sh`; see `tier3s/README.md` for their roles.

Evidence:

- `logs/phase0-20261001/` (`INDEX.md`): first install, idempotence, probe
  PASS, the negatives (runsc removed, tampered cache, sidecar mode, extra
  symlink + appended byte, hardened profile, root refusal of the test hooks),
  14 unit tests.
- `logs/phase0-fix-20261002/` (`INDEX.md`): the re-run after the full-branch
  review, below.

### Full-branch review fixes (2026-10-02)

The full astra review found two P2 defects in what Phase 0 delivered, and
test/documentation gaps (P3). Fixed and re-evidenced:

- **The probe executed an unverified install.** It used to run
  `runsc --version` before checking hashes, file set, owners and modes, so a
  replaced runsc (stamp intact), a foreign-owned one, or a symlinked
  `RUNSC_DIR` was executed (as root, under the documented invocation) before
  being rejected. Now nothing from the install executes until the stamp, the
  trusted ancestors (real dirs, root-owned, not group/other-writable, up to
  `/`), the exact file set and every per-file sha512 pass; on any failure the
  probe prints `FAIL runsc_version: not executed`. Then runsc is opened once,
  the open inode is re-verified (same dev:ino as validated, regular file,
  0755, root, sha512 read through the fd) and executed via
  `/proc/self/fd/N`, so swapping the path after validation cannot change what
  runs, and the inode can only be rewritten by root (execve also refuses a
  file open for writing).
- **Provisioning was not serialized.** Now one exclusive `flock` on
  `/run/qdistro-runsc/provision.lock` (root 0700) is taken before the live
  state is inspected and held through swap, verification, rollback and
  cleanup; a second run waits and then re-inspects. The cached tarball is
  copied into a private stage dir and verified and extracted from that copy;
  a download is verified in the stage dir before an atomic publish into the
  cache (no shared `.part`).
- **Tests that never reached their guards** now do: a clean synthetic probe
  must be TEST-PASS / exit 3; the `--pin`, prefix and test-hook refusals run
  with euid 0 (real root in the VM, else a user namespace) using a readable
  alternate pin and exact messages; byte-only tampering hits the per-file hash
  loop; plus version text with a nonzero exit, wrapper env scrubbing, failed
  rollback preservation, a two-process lock test and swap-during-window
  tests. `tier3s/spike/mutate-guards.py` breaks each guard in the real
  scripts and shows its test failing (20 mutations).

## Phase S — the feasibility spike

Run 2026-10-01 in a dev test VM (Tumbleweed 20260929, kernel 7.2.8,
podman 6.0.2, waypipe 0.11.0, SELinux Permissive) with runsc 20260928.0.
Evidence: `logs/phaseS-20261001/` (`INDEX.md`), scripts in this directory
(`README.md`).

| Step | Verdict | Main logs |
|---|---|---|
| 1 Headless hello | **PASS**, after one required change (runsc state root) | `10-s1-headless-hello.log`, `s1-artifacts/` |
| 2 waypipe SHM round trip | **PASS**: wayland-info, weston-terminal, foot | `20`–`23`, `screens/22-*`, `screens/23-*` |
| 3 Cgroup placement | **PARTIAL**: the planned root-unit shape contains none of the sandbox; three shapes contain every process class, and only one carries root-owned limits | `30`–`37-s3-*.log` |

**Go/no-go: GO** for Phase A (headless) and Phase B (GUI over the bridge),
with three non-optional conditions: an explicit runsc state root used by
every lifecycle call, an admin-writable owning scope, and a secctx-wrapped
bridge client.

### Workload image (offline)

The VM had no container image and the sandbox has no network, so
`stage-image.sh` builds one from the dependency closure of the VM's own
installed packages (284 packages, snapshot 20260929) plus foot from the
host's snapshot RPM cache (sha256 + `rpm -K` checked), imported as
`localhost/tier3s-spike/tw-terminals:20260929` (`01-stage-image.log`).

### Step 1 — headless hello: PASS (with the root flag)

As admin, `podman --runtime /usr/libexec/qdistro/tier3s-runsc
--runtime-flag=network=none [--runtime-flag=root=/run/user/1000/runsc] run
--rm --security-opt label=disable --security-opt no-new-privileges
--security-opt seccomp=smoke.json --cap-drop=ALL --userns=keep-id --user
1000:1000 --read-only --tmpfs /tmp:size=64m --tmpfs
/run/user/1000:rw,U,mode=0700 --network=none IMG sh -c '…'`
(`smoke.json` = the tier-2 `weston-terminal.json` profile + `syslog`).

- Without the root flag: rc 126, `mkdir /var/run/runsc: permission denied`.
  runsc's state root is `$XDG_RUNTIME_DIR/runsc` else `/var/run/runsc`, and
  the wrapper's `env -i` strips `XDG_RUNTIME_DIR`.
- With it: rc 0; `Linux version 4.19.0-gvisor`, gVisor dmesg via
  `syslog(2)`, uid 1000, `/run/user/1000` 1000:1000 0700, loopback-only
  routes.
- Identity: `podman inspect` → `OCIRuntime=/usr/libexec/qdistro/tier3s-runsc`;
  the sandbox process (`State.Pid`) has `/proc/<pid>/exe` =
  `gvisor-bin/gvisor_sentry` whose sha512 equals the pin's
  `sidecar_gvisor_sentry_sha512`; the gofer is the pinned `runsc`.
- OCI config (`s1-artifacts/config.json`): user 1000:1000, no new
  privileges, empty SELinux label, no capabilities, read-only root, the
  tmpfs `U` option became `uid=1000,gid=1000`, seccomp default ERRNO(38),
  `syslog` allowed. gVisor ignores `rprivate`, `nodev`, `tmpcopyup`.
- Process classes, all in the user's `libpod-conmon-<id>.scope`: conmon
  (uid 1000), `runsc-gofer` (exe runsc, uid 100000), `runsc-sandbox` (exe
  `gvisor_sentry`, uid 100000), `runsc-fd-parking` (uid 1000), systrap stubs
  (exe `gvisor_sentry`, empty cmdline). No `libpod-<id>.scope`
  (`--ignore-cgroups`). The workload has no host process.
- Stop: a flagged `podman stop` cleans up; conmon's exit command carries the
  root flag. A **plain** `podman stop` (no tier3s flags) fails (rc 125) and
  leaves the sandbox running.
- Seccomp errno: runsc returns **EPERM** for every ERRNO action including the
  default (runc/crun return ENOSYS for unlisted calls). This breaks glibc's
  ENOSYS-keyed fallbacks: `lchmod` (fchmodat2 fallback) fails with EPERM;
  `ls` reports EPERM from `llistxattr` (195). Denials appear as
  `Syscall <nr>: denied by seccomp` in the Sentry debug log, never in
  `--strace` output.

### Step 2 — waypipe SHM round trip: PASS

Dev lane (no secctx): host-side `waypipe -s <dir>/link.sock -o --no-gpu
client` as admin on qdwin; sandbox side `waypipe -s
/run/qdistro/link/link.sock -o --no-gpu server -- <app>` with
`--runtime-flag=host-uds=open` and a bind of the admin 0700 socket dir.

- wayland-info (`20`): rc 0, qdwin's full global list through the bridge.
- Negative (`21`): without `host-uds=open`, ECONNREFUSED: a configuration
  cause, not a gVisor incompatibility. (Its outer exit 0 is the script's; the
  oracle is the sandbox rc 1 + ECONNREFUSED.)
- weston-terminal (`22`) and foot (`23`): windows render, keyboard input
  works, `uname -r` = `4.19.0-gvisor`, no `/dev/dri` (software/SHM), the
  window is gone after stop. Screenshots in `screens/` were viewed, not
  OCR'd. foot's first attempt (`attempts/23a…`) hit `invalid locale`: an
  image defect (the same image under runc has no UTF-8 locale either), fixed
  in the stager.
- Non-fatal seccomp denials: `setfsuid` (122), `setfsgid` (123),
  `fadvise64` (221), and once `link` (86, fontconfig cache lock).
- **Security observation:** through the plain dev-lane client the app's
  registry **advertises** qdwin's privileged globals (`qdwin_locker_v1`,
  `qdwin_shell_v1`, `qdwin_nested_manager_v1`, `qdwin_stream_input_v1`,
  virtual keyboard, input method, layer shell, output manager). Binds were
  not attempted; qdwin gates several of them per global or per operation.

### Step 3 — cgroup placement: PARTIAL (three containing shapes, one with root limits)

`s3-cgroups.sh`, workload `sleep 45`, recursive `cgroup.procs` of the target
cgroup, every descendant of conmon classified. "7" is the observed process
count of one sandbox (stubs vary).

| Shape | Result | Limits |
|---|---|---|
| planned: root unit (`MemoryMax`, `TasksMax`) → `runuser` → podman (`30`) | **0/7**; conmon and runsc land in user@1000 scopes | cover `runuser` only |
| (a) root scope, `Delegate=yes`, chowned to admin, podman `--cgroup-manager=cgroupfs --cgroup-parent=<scope>/sandbox` (`31`, rerun `37`) | **7/7** (podman CLI and runuser too) | root-set memory.max / pids.max |
| control: (a) minus `--cgroup-parent` (`35`) | **7/7** | as (a) |
| control: (a) minus the chown (`36`) | **0/7** (podman to its own `podman-<pid>.scope`) | none |
| two-variable control: no delegation, no flag (`34`) | **0/7** | none |
| (a′) user slice via the systemd manager (`32`) | **7/7** | pids only delegated |
| (b) `systemd-run --user --scope -p Delegate=yes` + `--cgroups=split` (`33`) | **7/7** | pids only |

Admin-writability of the scope is the deciding input; `--cgroup-parent`
did not change placement (runsc ignores the OCI `cgroupsPath` under
`--ignore-cgroups`; the `sandbox` child stays empty). That the limit files
stay root-owned is **inferred** from the selective chown, not shown; whether
admin can raise them was **not tested**. The spike's `inner.sh` runs as root
from an admin-writable directory and must not be copied into a launcher.

## Facts Phase A may rely on (this pin, this VM)

1. podman 6.0.2 + runsc 20260928.0 (systrap, `--oci-seccomp`,
   `--ignore-cgroups`) runs a keep-id, read-only, cap-dropped,
   no-new-privileges, `label=disable`, `network=none` container as admin,
   given the state root (fact 2).
2. With `env -i` in the wrapper, runsc needs `--root`, and **every** runtime
   control call (stop, kill, rm, recovery) must use the same root. A plain
   `podman stop` without it fails and leaks the sandbox; plain `ps`/`inspect`
   succeed, so metadata visibility does not prove control.
3. tmpfs `U` → OCI `uid=/gid=`, honoured by gVisor.
4. Host process classes and uids as in step 1. Uid 100000 is mapped container
   root shared by other keep-id launches: never clean up "everything owned by
   100000".
5. `--oci-seccomp` maps every ERRNO to EPERM; denials are visible only in the
   Sentry debug log.
6. `host-uds=open` + a bind of an admin 0700 dir holding an admin 0600 socket
   lets the sandbox connect to a host UDS.
7. waypipe 0.11.0 `server -o --no-gpu` works under systrap; weston-terminal
   and foot render with keyboard input (software/SHM).
8. Containing shapes: (a) admin-delegated root scope with the cgroupfs
   manager (root-owned limits), (b) `--cgroups=split` in a delegated user
   scope (pids only here), (a′) a user slice via the systemd manager (pids
   only).
9. The *tested* cleanup sequence (`--rm`, flagged `podman stop`, then
   stopping both units) leaves no runsc/conmon/waypipe process and no `t3s-*`
   unit (`40-final-state.log`, each s3 log's end scan). Not shown: that
   `podman stop` alone cleans up the host bridge client, launcher-SIGKILL
   recovery, restart reconciliation. Log 32 leaves an empty user slice.

## Wrong or incomplete planning assumptions

| # | Assumed | Observed |
|---|---|---|
| W1 | the wrapper + planned command run as written | rc 126 without `--root` (state root) |
| W2 | `/proc/<sandbox pid>/exe` is the pinned `runsc` | it is `gvisor-bin/gvisor_sentry`; prove identity by its sha512 against the pin's sidecar hash (gofer = `runsc`) |
| W3 | a root unit wrapping `runuser … podman` owns the sandbox | 0/7 |
| W4 | `--cgroup-parent=<scope>` places the sandbox | the scope's admin-writability decides (`35`/`36`) |
| W5 | ERRNO→EPERM "may" break glibc fallbacks | it does (`lchmod`, `llistxattr`) |
| W6 | a dashed `--cgroup-parent` slice name is flat | systemd nests dashed names (`attempts/32a…`) |
| W7 | (confirmation) the secctx-wrapped bridge client is required | the plain client exposes privileged globals in the registry |
| W8 | (image fact) the minimal rootfs had a locale | it lacked `glibc-locale-base`; verify locale in Phase A images |

## Evidence boundaries

- The Phase S scripts are observational: many print a failure and end with a
  successful `echo`, so `### exit=0` or "0 failed VM steps" is not itself a
  PASS. Each verdict rests on the substantive output it cites (pin-matched
  exe hashes, the OCI artifact, the UDS negative, process and cgroup
  listings, viewed screenshots). The Phase 0 fix run
  (`logs/phase0-fix-20261002/`) is asserting instead: each step's exit status
  is its count of failed `CHECK`s.
- Advertisement of a global over the bridge is not authorization to use it;
  binds and operations were not tested.
- Placement is observed placement, not an adversarial resource-containment
  proof; limit ownership/non-escalation is inferred, not tested.
- The provisioner's three-path install (tree, wrapper, stamp) is not one
  atomic transaction; power-loss or SIGKILL recovery is not proven (a killed
  run leaves PID-named `.new.`/`.old.` siblings, which the probe ignores and
  the next run does not reuse).
- The wrapper implements the scrubbed environment and constant flags; it is
  not a policy-enforcing launcher, and its argument pass-through must stay
  under launcher control in Phase A.
- No KVM claim: the VM has `/dev/kvm`, nothing used it, its absence was not
  tested.

## Phase A/B handoff

1. **Runtime root and lifecycle (Phase A).** Choose where the runsc root
   lives (wrapper constant vs launch flag) and centralize it across start,
   stop, kill, rm and recovery. Record the container id/token, runtime root,
   stable process identities and the owning scope in a root-only control
   dir. Test a missing or wrong root, a failed stop, service death, restart
   reconciliation, and two simultaneous launches (tearing one down preserves
   the other). Final teardown goes through the root supervisor and the
   recorded scope with verified targets and recursive emptiness checks —
   never by uid 100000 or app name.
2. **Owning scope (Phase A).** Replace the root-unit recipe with the measured
   shape: a root-created scope selectively delegated to admin plus
   `--cgroup-manager=cgroupfs`; couple its lifetime to the launch service;
   account for conmon, gofer, Sentry, fd-parking, every stub and the launcher
   overhead (podman CLI and runuser count against `TasksMax`). Privileged
   setup lives in installed root-owned helpers; limit files and cleanup
   records stay root-controlled; cgroupfs and control endpoints stay out of
   sandbox exports. Re-prove the shipped command with the recursive
   `cgroup.procs` check. Phase C verifies limit ownership/non-escalation and
   memory/tasks/CPU behaviour on the same topology.
3. **Seccomp per workload (Phase A).** Decide explicitly on `fchmodat2`
   (exercise the affected chmod), `llistxattr`, `setfsuid`, `setfsgid`,
   `fadvise64`, `link`. Do not allow calls just to silence the debug log, and
   do not expect `defaultErrnoRet=38` to restore ENOSYS (runsc hard-codes
   EPERM).
4. **Images (Phase A/B).** Provision a UTF-8 locale in both terminal images;
   verify locale, HOME and writable cache/runtime ownership in the built
   images. Keep the non-fatal `fallocate(PUNCH_HOLE)` and waypipe
   degenerate-damage messages for compatibility/performance work.
5. **Identity (Phase A).** podman selects the tier3s wrapper; `State.Pid` is
   the Sentry; `/proc/<pid>/exe` hashes to `sidecar_gvisor_sentry_sha512`; the
   gofer hashes to `runsc_sha512`. Version strings and the absence of
   app-named host processes are corroboration only.
6. **Bridge (Phase B).** Run the host-side waypipe client under
   `qdistro-secctx-exec` with a root launcher parent (the tier-3 topology),
   with attested tier3s identity, and fail the launch if identity setup
   fails. The oracle is **per interface**, not "every listed global
   disappears": qdwin keeps `zwlr_output_manager_v1` enumeration public
   (apply/test are authorized separately) and binds `qdwin_stream_input_v1`
   openly (its `claim` checks a token and the forwarder pid). So test absence
   or bind refusal for the hidden interfaces, refusal of unauthorized
   *operations* for the visible ones, against the actual tagged peer with
   authorization overrides off. Keep the hostile-stream test and the decision
   that the admin-side waypipe parser is trusted. Test rendering and input for
   both terminals and complete teardown of the host client.
7. **Acceptance drivers.** Replace the observational spike drivers with
   asserting ones; test the actual shipped hardened refusal, network policy,
   lifecycle cleanup and executable identity — a prerequisite screen or
   metadata inspection is not a substitute.
8. **A DONE bar:** the shipped launch path places every runtime process class
   inside the recorded owning scope; normal exit, stop, launcher SIGKILL and
   restart reconciliation leave no launch-owned processes, scope or
   token/control dirs; tearing down one launch preserves the other; runtime
   identity and the state-root policy are verified.

Operational note: qdlocker idle-locks the dev VM after 5 min;
`host-unlock.sh` unlocks through QMP keys and checks qdwin's
`locked_changed=0` journal line.

## Reviews (provenance)

Phase 0: codex sol (gpt-6-sol) r1–r3, APPROVE at r3. Phase S: sol r1 REVISE,
r2 APPROVE; astra milestone APPROVE with planning items (folded above).
Full-branch astra review 2026-10-02: REVISE (probe exec ordering,
provisioning lock, guard-test reachability, in-tree results) → fixed as
described under Phase 0; re-review rounds are recorded in the tracker. Briefs
and answers live in the private tracker (`todo/paravirt/reviews/`).
