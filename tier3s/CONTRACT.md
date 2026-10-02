# tier3s lifecycle contract (Phase A)

Status: **Experimental, dev profile only** (owner O4), explicit launch, no
fallback to tier 2/3 (O6), `network=none` only (O3). This file is the
contract the Phase A code is written against. Milestone A-i wrote it and the
launch-path code (`spawn-tier3s.sh`, `qdistro-tier3s-scope`,
`qdistro-tier3s-cleanup`, the wrapper/probe changes, the seccomp profile,
the image recipe). A-ii (session manager, broker, units, installer) and
A-iii (VM drivers s120–s122) implement the interfaces named here.

Decisions are numbered after the kickoff deltas (`D-A1` = ΔA1, …). Each cites
the VM evidence it rests on; the feasibility checks ran in a dev test VM
before this file was finalized, because two decisions depended on how runsc
and systemd actually behave (`spike/logs/phase-a-20261002/feasibility*/`,
`INDEX.md` there). They are feasibility evidence, not the A-iii acceptance
drivers.

## 1. Topology

```
SessionManager1.CreateTier3sSilo(name, workload, template_silo, network="none")   [A-ii]
SessionManager1.StartSilo(name)                                                   [A-ii]
  writes /run/qdistro/silo-launch/<name>.env  (root 0600, KEY='VALUE')
  systemctl start qdistro-tier3s-silo@<name>.service            (User=root)       [A-ii]
    ExecStart=/usr/libexec/qdistro/qdistro-tier3s-silo-launch %i                  [A-ii]
      env -i … TIER3S_ROOT_LAUNCHER=1 TIER3S_LAUNCH_UNIT=%n TIER3S_LAUNCH_TOKEN=<t> TIER3S_SILO=<name> …
      exec /usr/lib/qdistro/tier3s/spawn-tier3s.sh <workload> -- <argv…>         (root supervisor)
        probe → read-only binding → token → broker gate → activation record
        → control record + per-launch dir → transient scope:
        systemd-run --scope --unit=qdistro-tier3s-<t>.scope -p Delegate=yes
                    -p BindsTo=<launch unit> -p Before=<launch unit> -p TasksMax= -p MemoryMax=
          -- /usr/libexec/qdistro/qdistro-tier3s-scope enter <t> 1000 -- podman …
               (root: verify own cgroup, delegate it to admin, exec runuser -u admin podman …)
    ExecStop=-/usr/libexec/qdistro/qdistro-tier3s-cleanup --unit %n
    ExecStopPost=/usr/libexec/qdistro/qdistro-tier3s-cleanup --unit %n
```

Podapps (`LaunchPodApp` analogue) use the same spawn with no `TIER3S_SILO`,
unit `qdistro-tier3s-app@<token>.service`; A-ii may ship silos first and
must then refuse the podapp path explicitly.

Every podman call runs **as admin (uid 1000)**, rootless, `--userns=keep-id`
(D4 C1). Root does three things only: supervise, create the scope, and tear
down through the recorded scope. Nothing runs podman or runsc as root.

### Installed paths (the A-ii installer installs exactly these, root-owned)

| Path | From |
|---|---|
| `/usr/lib/qdistro/tier3s/` | root-owned copy of `tier3s/`: `spawn-tier3s.sh`, `probe.sh`, `RUNSC_RELEASE`, `tier3s-runsc`, `seccomp/<workload>.json` (the probe compares the installed wrapper and pin against this copy) |
| `/usr/libexec/qdistro/qdistro-tier3s-scope` | `tier3s/qdistro-tier3s-scope` |
| `/usr/libexec/qdistro/qdistro-tier3s-cleanup` | `tier3s/qdistro-tier3s-cleanup` |
| `/usr/lib/tmpfiles.d/qdistro-tier3s.conf` | `tier3s/tmpfiles/qdistro-tier3s.conf`, then `systemd-tmpfiles --create` on it |
| `/usr/libexec/qdistro/tier3s-runsc`, `/usr/libexec/qdistro/runsc/` | `provision-runsc.sh` (Phase 0, on demand, D1) |
| units + launch helper | `session_manager/qdistro-tier3s-silo@.service`, `…-silo-launch` [A-ii] |

## 2. D-A1 — runsc state root: a fixed root per host uid, in the wrapper

**Decision.** The wrapper `tier3s-runsc` (the only runtime path podman is
given) passes `--root=/run/qdistro-tier3s-runsc/<host uid>` to every runsc
invocation. Rootless podman runs the wrapper inside its user namespace
(uid 0 there), so the wrapper maps its uid to the host uid through
`/proc/self/uid_map`. No podman call carries a root flag; the container's
recorded runtime (`OCIRuntime=/usr/libexec/qdistro/tier3s-runsc`) is enough,
so `podman ps`, `stop`, `rm`, conmon's exit command and the cleanup's calls
all reach the same root.

Why the wrapper and not a launch flag: a `--runtime-flag=root=…` must be
repeated on every later call, and a plain `podman stop` without it fails
(rc 125) and leaks the sandbox (Phase S B2). A wrapper constant cannot be
forgotten by a caller.

**Created only by provisioning.** `tmpfiles.d/qdistro-tier3s.conf` creates
`/run/qdistro-tier3s-runsc` (root 0755) and `/run/qdistro-tier3s-runsc/1000`
(1000:1000 0700) at install time and every boot. The wrapper, the spawn and
the cleanup never create it. Before runsc runs, the wrapper refuses (exit 125,
message on stderr and in the journal as `tier3s-runsc`, since podman drops a
runtime's stderr) when:
- the base or the root is missing, a symlink or not a directory;
- the root is not owned by the caller (mapped uid) or not mode 0700;
- the base is not mode 0755;
- the caller passes any `--root` (`--root`, `--root=`, `-root`, `-root=`);
- the root path is longer than 31 bytes. runsc's control socket is
  `<root>/runsc-<64 hex>.sock`, and runsc silently falls back to `/var/run`,
  `/run` and `/tmp` when that exceeds `sun_path`.

So a stop against a lost or replaced root fails visibly instead of runsc
minting an empty root and answering "no such container".

The **probe** checks the same root (`state_root`: base root-owned 0755, root
owned by the launching user, 0700, not symlinks). The **cleanup** runs as
root, drops to the **recorded** admin uid for every podman call and rejects a
record whose `runsc_root` differs from the root derived for that uid. root's
own uid would derive a different root.

Evidence (feasibility, the shipped wrapper, no root flag on any call):
- `feasibility/10`: plain `podman ps`, `stop` and `rm` reach the sandbox.
  The Sentry exe hash matches the pin, the root holds the container state and
  is empty after rm.
- `feasibility/11`: with the root moved aside, a plain `podman stop` returns
  rc 125 and the Sentry stays alive. No root is minted, and the journal has
  the refusal. With an empty replacement root (right owner and mode), runsc
  cannot find the container: rc 125, Sentry alive. After the original root is
  restored, stop and rm succeed and nothing is left.

## 3. D-A3b — owning scope: root-created, selectively delegated, cgroupfs

**Decision.** Phase S shape (a) without `--cgroup-parent` (log `35`). The
spawn (root) creates a transient **system** scope
`qdistro-tier3s-<token>.scope` with `systemd-run --scope -p Delegate=yes`, root-set
`TasksMax=1024` and `MemoryMax=2G` (set, not yet tested for enforcement:
Phase C), `--collect`, and its lifetime coupling (below). The scope's first
process is the installed root-owned helper `qdistro-tier3s-scope enter
<token> <admin-uid> -- podman …`, which:
1. verifies it is root, on a pure cgroup-v2 hierarchy, in exactly the
   cgroup of `qdistro-tier3s-<token>.scope`, as the cgroup's only process,
   with no child cgroups;
2. `chown`s to admin only the scope directory, `cgroup.procs`,
   `cgroup.subtree_control` and `cgroup.threads`. Every limit and accounting
   file (`memory.max`, `pids.max`, …) stays root's;
3. execs `runuser -u admin -- env -i <fixed env> /usr/bin/podman …`. It
   never runs anything as root and accepts no other program.

podman runs with `--cgroup-manager=cgroupfs`. The admin-writable scope is
what keeps rootless podman from escaping to its own `podman-<pid>.scope`
(Phase S `35`/`36`). `--cgroup-parent` is not used: runsc ignores the OCI
`cgroupsPath` under `--ignore-cgroups`. `--pids-limit` is kept for parity
with tier 2 only; it is **not enforced** under `--ignore-cgroups`. The
scope's `TasksMax` is the limit, and it also counts the launcher overhead
(podman CLI, runuser, conmon). No cgroupfs path or control endpoint is
mounted or exported into the sandbox; gVisor shows the workload a synthetic
cgroup tree.

**Lifetime coupling.** The scope carries `BindsTo=<launch unit>` and
`Before=<launch unit>`:
- On a stop job for the launch unit (`systemctl stop`, session-manager
  stop), `Before=` keeps the scope alive through `ExecStop`/`ExecStopPost`,
  so the verified cleanup does the graceful teardown. systemd then stops the
  scope (`feasibility-r3/20`: scope active in ExecStopPost with 9 procs,
  inactive about 2 s later).
- When the launch unit's main process dies unexpectedly (SIGKILL, crash),
  there is no stop job, so `Before=` orders nothing. `BindsTo` starts
  stopping the scope as the unit leaves `active`, **concurrently** with
  `ExecStopPost` (`feasibility-r3/21`: the scope is already `deactivating`
  during ExecStopPost; scope, sandbox and podman record are gone about 2 s
  later). The scope never outlives its launch service; the cleanup then
  verifies absence and removes the records.

So a teardown *failure* preserves the record and the scope only while the
launch unit is still active: a direct `qdistro-tier3s-cleanup <token>` (or
`--unit`) from root. That is how A-iii injects the runtime-failure case.

**Process classes inside the scope** (`feasibility-r3/20`, recursive
`cgroup.procs`, 9/9, every process in the scope cgroup itself):
- launcher overhead: runuser, the podman CLI, conmon;
- `runsc-gofer` (exe `runsc`);
- `runsc-sandbox` (exe `gvisor_sentry`, = `State.Pid`);
- `runsc-fd-parking`;
- 3 systrap stubs (exe `gvisor_sentry`, empty cmdline).

No runsc-bundle process was outside the scope, and admin could not raise
`memory.max` or `pids.max`. The shipped command is re-proved by s120
(A-iii).

## 4. D-A8 — control record, lock, cleanup, reaper

### Records

- `/run/qdistro-tier3s-ctl/` root 0700 (tmpfiles). It is **never mounted into
  a sandbox**.
- `/run/qdistro-tier3s-ctl/<token>/` root 0700, with `state` root 0600:
  `KEY=VALUE` lines, **parsed, never sourced**, written atomically (temp file
  plus `mv`) under the lock.
- `/run/qdistro-tier3s/<token>/` admin 0700 under a root 0755 parent: the
  per-launch dir. It is Phase B's bridge socket dir and holds **no control
  state** (03 step 8). Phase A mounts nothing from it.

`state` fields:

| Key | When | Meaning |
|---|---|---|
| `schema` | create | `1` |
| `token` | create | 32 lowercase hex |
| `container` | create | `qdistro-tier3s-<silo>` or `qdistro-tier3s-app-<token>` |
| `unit` | create | the launch unit (`TIER3S_LAUNCH_UNIT`, verified against `/proc/self/cgroup`) |
| `scope_unit` | create | `qdistro-tier3s-<token>.scope` (recorded **before** the scope exists, so a SIGKILL in between cannot hide it) |
| `admin_uid` | create | `1000` |
| `runsc_root` | create | `/run/qdistro-tier3s-runsc/<admin_uid>` (D-A1) |
| `per_launch_dir` | create | `/run/qdistro-tier3s/<token>` |
| `phase` | create, start | `created` → `running` |
| `container_id` | start | podman Id |
| `scope_cgroup` | start | the scope's `ControlGroup` as systemd reports it |
| `conmon_pid`, `conmon_starttime` | start | `State.ConmonPid` + `/proc/<pid>/stat` field 22 |
| `sentry_pid`, `sentry_starttime` | start | `State.Pid` (the Sentry) + field 22 |

At start the spawn **verifies** that the conmon and Sentry pids sit inside
the scope's cgroup before recording them. If they do not, it tears the
launch down and fails.

### Lock

There is one `flock` on `/run/qdistro-tier3s-ctl/.lock`. It serializes record
creation and every cleanup or reap. The spawn holds it only to create its
record and never while waiting on podman. A cleanup holds it for one token's
teardown.

### `qdistro-tier3s-cleanup` (root; the final teardown path)

```
qdistro-tier3s-cleanup <token>                 # one launch
qdistro-tier3s-cleanup --unit <launch unit>    # every record whose unit= matches (ExecStop/ExecStopPost)
qdistro-tier3s-cleanup --reap-stale [--except-unit <unit>]
                                               # records whose unit is not active/activating/deactivating,
                                               # plus labelled containers with no live launch unit (reconciliation)
```

Per token, in order; any failure **exits non-zero and preserves the control
record (and the scope, if it is still alive)**:
1. Validate the token and the record. The record is a real directory, root
   0700, `state` root 0600; the fields are well-formed; `admin_uid` is 1000;
   `runsc_root` equals the root derived for `admin_uid`; `scope_unit`
   matches the token; `per_launch_dir` matches the token.
2. The state root is present, not a symlink, owned by `admin_uid`, 0700. A
   missing or replaced root fails here with "refusing to query or stop", so
   it never produces a false "no container".
3. Query `podman container exists <container>` as the recorded admin. 0 is
   present, 1 is absent, anything else is "**podman query failed**" (error,
   preserve). It never conflates a failed query with "no container" (unlike
   tier 2's `spawn-tier2.sh` reaper, which suppresses errors). If present,
   the container's `qdistro_tier3s_token` label must equal the token.
4. If present: `podman stop -t 10`, then `podman rm -f --ignore`, then
   re-query. The container must be absent; a stop or rm error is an error.
5. Scope: if systemd still knows `scope_unit`, its `ControlGroup` must equal
   the recorded `scope_cgroup` (the verified target). Wait for the cgroup
   subtree to be empty, reading `cgroup.procs` recursively. If it is not
   empty, `systemctl stop <scope_unit>` (systemd kills by cgroup) and wait
   again; still not empty is an error.
6. The recorded `(pid, starttime)` of conmon and the Sentry must be dead. If
   either is alive with the same starttime, it escaped the scope: error,
   preserve. It is never killed by pid, uid (100000 is shared by every
   keep-id launch, 05 fact 4) or process name.
7. Remove `per_launch_dir` (a real directory under the root-owned parent;
   `rm -rf` does not follow symlinks), then the control dir.

A missing control record with a leftover per-launch dir: the dir is removed
only when `qdistro-tier3s-<token>.scope` is not active.

### Reaper and reconciliation

The spawn runs `qdistro-tier3s-cleanup --reap-stale --except-unit <own unit>`
before creating its record. A record whose `unit` is not live is stale, and so
is a record carrying the spawn's own unit but another token (one unit runs
one launch at a time). Reap failures are logged and do not block the new
launch; launches are independent.

Restart reconciliation (A-ii):
- After a session-manager restart, the manager runs `qdistro-tier3s-cleanup
  --reap-stale` and stops every tier3s launch unit it does not mean to keep;
  that unit's `ExecStopPost` reaps it.
- `--reap-stale` also lists labelled containers as admin (`podman ps -a
  --filter label=qdistro_tier3s_token`). A failed listing is an error, never
  "nothing to reap". A container whose `qdistro_tier3s_unit` label names a
  unit that is not live gets the per-token teardown. With no control record,
  that teardown is a stop/rm by name plus stopping
  `qdistro-tier3s-<token>.scope`, reported as "unrecorded".
- Containers carry the labels `qdistro_tier3s_token=<token>` and
  `qdistro_tier3s_unit=<launch unit>`.

### Teardown paths (A-iii tests each; DONE bar 2)

| Path | What happens |
|---|---|
| normal exit | `podman run --rm` returns. The spawn's EXIT trap runs `cleanup <token>`, which verifies absence and removes the records. Then the unit's `ExecStopPost` finds nothing |
| plain `podman stop` (admin) | reaches the sandbox through the wrapper root, so it continues as normal exit |
| session-manager / `systemctl stop` | `ExecStop` cleanup with the scope alive, then the scope stops (Before=, BindsTo) |
| launcher SIGKILL / service failure | `ExecStopPost` cleanup runs while BindsTo stops the scope, then verifies |
| manager restart, state lost | `--reap-stale` + unit stop |
| runtime query/stop failure (root missing or replaced) while live | `cleanup` errors, and the record and scope stay. After the root is restored, `cleanup` tears everything down |

## 5. Spawn contract (`spawn-tier3s.sh`)

`spawn-tier3s.sh <workload> -- <app> [args…]`, root supervisor, ≤ 600 lines.
The order is fixed and fail-closed (exit 2 on every refusal). The denial
oracle is "no `podman run` and no activation record":

1. **Profile:** anything but `dev` refuses with "tier 3s is dev-profile only
   in this PoC" (README O4). There is no hardened launch path.
2. **Refused env knobs:** `TIER3S_SECCOMP_PROFILE`, `TIER3S_ALLOW_PRIVESC`,
   `TIER3S_KEEP_CAPS`, `TIER3S_RUNTIME`, `TIER3S_CGROUP_PARENT`. Any network
   but `none` is refused.
3. **Root launcher:** `TIER3S_ROOT_LAUNCHER=1`, euid 0, admin uid 1000, and
   `TIER3S_LAUNCH_UNIT` matching `qdistro-tier3s-(silo|app)@….service` and
   equal to the unit of the spawn's own cgroup. There is no direct-admin
   lane.
4. **Probe:** `/usr/lib/qdistro/tier3s/probe.sh --user admin` must exit 0.
   Anything else refuses with its `RESULT` line (prerequisites, state root,
   pinned runsc, wrapper). There is no fallback tier.
5. **Read-only resolution:**
   - the workload name, and its seccomp file
     `/usr/lib/qdistro/tier3s/seccomp/<workload>.json`, which must exist
     (there is no podman-default fallback);
   - the image: for a templated silo, `qdistro-resolve-binding <silo>
     --launch-env` **without** `--record` (a digest plus the state path);
     otherwise `localhost/qdistro/tier3s-<workload>:latest`.
6. **Token:** `TIER3S_LAUNCH_TOKEN` (validated `^[0-9a-f]{32}$`) or a fresh
   one. Then the container name.
7. `TIER3S_PRINT_PLAN=1` prints the plan (`KEY=VALUE`, one `PODMAN_ARG=`
   line per podman argument, one `SCOPE_ARG=` per systemd-run argument) and
   exits 0 **before** the gate and any side effect.
8. **Broker gate** as admin: `CheckPermission("qdistro.tier3s.spawn:<workload>/<app
   basename>")`. Only `allow` passes; deny, unknown, empty, unsupported or a
   D-Bus error all exit 2. The function is copied from `spawn-tier2.sh`
   (marked `unify after Phase B`). A-ii adds the prefix to the broker's
   rules-only tuple.
9. **Activation recording:** for a templated silo, `qdistro-resolve-binding
   --record --launch-env` as admin. The digest must equal step 5's, or the
   launch refuses.
10. Reap stale launches; under the lock, create the control record and the
    per-launch dir. The EXIT/TERM trap runs `cleanup <token>` from here on.
11. `podman image exists` as admin.
12. `systemd-run --scope …` (D-A3b) in the background. Poll `podman inspect`
    until running, verify that conmon and the Sentry are in the scope, then
    record them (`phase=running`). Wait, and exit with podman's status.

The podman command (as admin, inside the scope):

```
podman --runtime /usr/libexec/qdistro/tier3s-runsc --runtime-flag=network=none
       --cgroup-manager=cgroupfs
  run --rm --name <container> --label qdistro_tier3s_token=<t> --label qdistro_tier3s_unit=<unit>
      --security-opt label=disable          # runsc rejects SELinux process labels
      --security-opt no-new-privileges --cap-drop=ALL
      --security-opt seccomp=/usr/lib/qdistro/tier3s/seccomp/<workload>.json
      --userns=keep-id --user 1000:1000 --read-only
      --tmpfs /tmp:rw,size=64m,mode=1777
      --tmpfs /run/user/1000:rw,U,mode=0700  # U -> OCI uid=1000,gid=1000 (literal uid= is rejected)
      --tmpfs /home/admin/.cache:rw,U,mode=0700
      [-v <state_path>:/home/admin:rw]      # templated silo only, no recursive chown
      --pids-limit=512                      # parity with tier 2 only; NOT enforced under --ignore-cgroups
      --network=none
      --env HOME=/home/admin --env XDG_RUNTIME_DIR=/run/user/1000 --env LANG=C.UTF-8
      <image> <argv…>
```

Diagnostics (dev only): `TIER3S_DEBUG_LOG_DIR=<absolute admin-owned dir>`
adds `--runtime-flag=debug --runtime-flag=debug-log=<dir>/`, which is where
seccomp denials are visible.

## 6. Session-manager contract (A-ii implements; D7 = explicit branches)

There is no backend table: the new kind gets explicit branches (D7: a table
would have to cover the whole contract below before it pays off).

- **Kind:** `KIND_TIER3S = "tier3s"` in `SILO_KINDS`; `validate_kind`
  accepts it; `validate_launch` takes the tier-2 launch shape with
  `network ∈ {none}` only.
- **Creation API:** `CreateTier3sSilo(name, workload, template_silo,
  network)`. Its own method, so `CreateTemplateSilo` keeps its hard-coded
  tier-2 kind.
- **Uid:** `validate_silo_uid(uid, "tier3s")` requires the admin launch owner
  (1000), like tier 2. Loader and quarantine (`:3187-3211`) treat tier3s
  rows like tier2 rows (admin uid, not a silo uid).
- **Mapping:** silo `<name>` maps to unit `qdistro-tier3s-silo@<name>.service`,
  which maps to container `qdistro-tier3s-<name>`. The manager pre-commits the
  token in the env stanza (`TIER3S_LAUNCH_TOKEN`), so silo, token,
  container, unit and scope are all derivable. The control record is the
  persisted mapping.
- **Observe:** `observe_silo` / `tier3s_silo_running`. The silo runs iff its
  unit is active **and** podman (as admin) reports the container running. A
  failed podman query is "unknown", never "stopped".
- **Stop:** `systemctl stop qdistro-tier3s-silo@<name>.service` (ExecStop and
  ExecStopPost run the cleanup). The manager then verifies through
  `tier3s_silo_running` and the absence of `/run/qdistro-tier3s-ctl/<token>`.
- **Freeze/resume: unsupported for tier3s.** `FreezeSilo` / `ResumeSilo` on a
  tier3s silo raise `BadArgument("freeze/resume is unsupported for tier3s
  silos")` before touching any cgroup. Freezing the scope would freeze the
  podman CLI and conmon as well, and gVisor-level pause is not wired.
- **Egress:** none. The tier3-user-only egress branches stay tier3-user only,
  and a tier3s silo with egress is rejected at creation.
- **Restart reconciliation:** §4 "Reaper and reconciliation".
- **Installer:** `scripts/install/install-session-manager.sh` installs the §1
  paths and units (only those lines).

Broker (A-ii): add `"qdistro.tier3s.spawn:"` to the rules-only prefix
tuple, add a unit test, and document it in `doc/permissions.md`. Tier 3s has
no SELinux type; `_ADMIN_HOSTILE_SELINUX_TYPES` is unchanged (Phase D).

## 7. Workload image and seccomp (D-A4, D-A5)

- `tier3s/Containerfile.headless-smoke` builds from
  `registry.opensuse.org/opensuse/tumbleweed:${SNAPSHOT}` (the `snapshot.conf`
  pin), with repos pinned by the tier3s-local `configure-snapshot-repos.sh`.
  It is labelled `org.qdistro.snapshot=<pin>` and also writes
  `/etc/qdistro/tier3s-image` with the pin. It installs `glibc-locale-base`
  (UTF-8 locale), sets `LANG=C.UTF-8`, has passwd entry
  `admin:x:1000:1000::/home/admin:/bin/bash`, and `/home/admin/.cache` exists
  for the tmpfs. Its default command `qdistro-tier3s-smoke` prints the
  checks the A-iii driver asserts.
- `tier3s/make-tier3s-image.sh` is copied from `tier2/make-tier2-image.sh`
  (unify later). It tags `qdistro/tier3s-<workload>:latest`, refuses an
  image whose snapshot label differs from the pin, prints `IMAGE_ID=` and
  `IMAGE_DIGEST=`, and with `--oci-archive <dir>` saves
  `<dir>/tier3s-<workload>.oci.tar` for transfer into a qci worker.
- Seccomp: `tier3s/seccomp/headless-smoke.json`, rendered by
  `seccomp/make-profiles.py` from tier 2's profile. Per-workload decisions,
  with the reasons in the file:

  | Call | Decision | Why |
  |---|---|---|
  | `fchmodat2` | **DENY**, forced by the pin | runsc 20260928.0's converter drops the name (`OCI seccomp: ignoring syscall "fchmodat2"`, `feasibility/31`), so an ALLOW is inert. `chmod -h` / `lchmod` give EPERM; plain `chmod` works |
  | `llistxattr` | ALLOW | `ls -l` prints EPERM errors otherwise; read-only metadata answered by the Sentry |
  | `setfsuid`, `setfsgid` | DENY | not used by this workload |
  | `fadvise64` | DENY | advisory; callers ignore the failure |
  | `link` | DENY | not used by this workload |
  | `syslog` | ALLOW | the gVisor `dmesg` banner (corroboration only) |

  Every ERRNO, the default included, is EPERM under runsc;
  `defaultErrnoRet: 38` restores nothing. Terminal profiles are Phase B.

## 8. What is not claimed

- A green host unit test proves the launch scripts' logic against fakes, not
  scope delegation, `ExecStopPost`, placement or teardown. Those are VM facts
  (A-iii, through the qci VM lane).
- The scope limits are set, not yet shown to be enforced (Phase C). Admin
  cannot raise them by writing the files (`feasibility-r3/20`); that is not
  an adversarial containment proof.
- Under SIGKILL of the launch service, teardown is systemd killing the
  scope's cgroup, then verification. It is not a graceful `podman stop`.
- `network=none` only, dev profile only, no KVM claim.
