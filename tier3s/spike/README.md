# tier3s/spike — Phase S feasibility spike (throwaway)

Plan: `todo/paravirt/03-implementation-plan.md` Phase S. Full write-up:
`todo/paravirt/05-phase-S-results.md`. Nothing here is a shipped file; the
scripts exist to produce evidence and will not be reused as-is in Phase A.

Pinned release: **runsc 20260928.0** (`tier3s/RUNSC_RELEASE`, provisioned by
`tier3s/provision-runsc.sh`, Phase 0). Test VM: dev profile, Tumbleweed
20260929, podman 6.0.2, waypipe 0.11.0, kernel 7.2.8. Every podman/runsc step
ran inside the VM as admin (uid 1000); the host only staged source, took
`virsh` screenshots and sent QMP keys.

## The three logs (evidence: `logs/phaseS-20261001/`, see its `INDEX.md`)

| Step | Log | Verdict |
|---|---|---|
| 1 headless hello | `10-s1-headless-hello.log` (+ `s1-artifacts/`) | PASS with one required change: runsc's state root (`--runtime-flag=root=…`) |
| 2 waypipe bridge | `20-…wayland-info`, `22-…weston-terminal`, `23-…foot` (+ `screens/`) | PASS: wayland-info, weston-terminal and foot render through the bridge and take keyboard input |
| 3 cgroup placement | `30`–`37-s3-*.log` | PARTIAL: the root unit as written contains none of the sandbox; three shapes contain every process class, only one carries root-owned limits |

## Go / no-go

**Go** for Phase A (headless) and for Phase B (GUI through the waypipe bridge).
The pinned podman + runsc + keep-id + `label=disable` + `--oci-seccomp` +
read-only + tmpfs-`U` + `network=none` combination starts, runs and cleans up
in the dev VM. `waypipe server` runs under gVisor systrap, and two Tumbleweed
terminals render through `--no-gpu` SHM with working keyboard input. Three
conditions are not optional:

1. runsc needs an explicit state root, because the wrapper's `env -i` strips
   `XDG_RUNTIME_DIR`. Every podman call that touches the container needs it:
   a plain `podman stop` fails and leaves the sandbox running.
2. The owning cgroup must be a scope that admin can write: either a
   root-created scope delegated to admin with podman's cgroupfs manager, or
   `--cgroups=split` inside a delegated user scope. A root unit wrapping
   `runuser … podman` contains none of the sandbox. One-variable controls show
   that admin-writability is the deciding input and `--cgroup-parent` is not
   (`35`/`36`).
3. The bridge's host-side waypipe client must run under
   `qdistro-secctx-exec`. Through the plain dev-lane client, the sandboxed
   app's registry advertises qdwin's privileged globals (locker, shell,
   virtual keyboard, layer shell). Binds were not tested.

## Scripts

| File | Runs on | Role |
|---|---|---|
| `run-phaseS.sh` | host | Driver for the whole spike (stage, s1, s2, s3, final state); refuses a non-empty log dir |
| `vmlog.sh` / `vmfetch.sh` | host | `vm-exec` transcript with the exact command + exit code / copy a guest dir out by sha256-checked tarball |
| `host-unlock.sh` | host | Unlocks the idle-locked session through QMP keys and checks qdwin's `locked_changed=0` journal line |
| `lib.sh` | VM | Shared flags (`t3s_podman_argv` = the 03 step-1 command + runsc root), `as_admin`, process/cgroup reporters, pin lookup by exe sha512 |
| `stage-image.sh` | VM | Offline workload image from the VM's own installed packages (dependency closure) + foot from the host's snapshot RPM cache (sha256 + `rpm -K`) |
| `smoke.json` / `make-smoke-json.py` | — | tier-2 `weston-terminal.json` + one `syslog` ALLOW entry; `--check` proves the file equals a fresh render |
| `s1-headless-hello.sh` | VM | Step 1 |
| `s2-waypipe.sh` | VM | Step 2 (`info`, `start`, `status`, `stop`) |
| `s3-cgroups.sh` | VM | Step 3 shapes `root-unit`, `parent-root` (+ one-variable controls `deleg-noparent`, `nodeleg-parent`), `scope-plain`, `parent-user`, `split` |
