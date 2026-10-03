# tier3s lifecycle contract (Phase A + Phase B milestone B-i)

Status: **Experimental, dev profile only** (owner O4), explicit launch, no
fallback to tier 2/3 (O6), `network=none` only (O3). This file is the
contract the Phase A code is written against. Milestone A-i wrote it and the
launch-path code (`spawn-tier3s.sh`, `qdistro-tier3s-scope`,
`qdistro-tier3s-cleanup`, the wrapper/probe changes, the seccomp profile,
the image recipe). A-ii (session manager, broker, units, installer) and
A-iii (VM drivers s120–s122) implement the interfaces named here.

Phase B milestone **B-i** adds the GUI bridge (`todo/paravirt`
`08-kickoff-phase-B.md`, deltas B1–B4): the waypipe byte-stream path Phase S
proved (`05-phase-S-results.md` §S2). gVisor cannot pass host-backed
`SCM_RIGHTS`, so a GUI workload's windows cannot reach the admin compositor
over a Wayland socket directly; they go through waypipe instead. §1 gains the
bridge client process class, §5 the bridge half of the spawn, §7 the workload
declarations and terminal images/profiles. B-ii (manager stanza dir, pod-app
surface) and B-iii (VM drivers, GUI acceptance) are separate milestones and
not covered here.

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
  systemctl start qdistro-tier3s-silo@<name>.service   (User=root, Type=notify)     [A-ii, r1]
    ExecStart=/usr/libexec/qdistro/qdistro-tier3s-silo-launch %i                  [A-ii]
      env -i … NOTIFY_SOCKET=<systemd's> TIER3S_ROOT_LAUNCHER=1 TIER3S_LAUNCH_UNIT=%n TIER3S_LAUNCH_TOKEN=<t> TIER3S_SILO=<name> …
      exec /usr/lib/qdistro/tier3s/spawn-tier3s.sh <workload> -- <argv…>         (root supervisor)
        probe → read-only binding → token → broker gate → activation record
        → reap stale → control record (published whole) + per-launch dir
        → [GUI: host waypipe client + launch record + RegisterLaunch] → transient scope:
        systemd-run --scope --unit=qdistro-tier3s-<t>.scope -p Delegate=yes
                    -p BindsTo=<launch unit> -p Before=<launch unit> -p TasksMax= -p MemoryMax=
          -- /usr/libexec/qdistro/qdistro-tier3s-scope enter <t> 1000 -- podman …
               (root: verify own cgroup, delegate it to admin, exec runuser -u admin podman …)
          … recorded running → READY=1 (the start job completes only here)
    ExecStop=-/usr/libexec/qdistro/qdistro-tier3s-cleanup --unit %n
    ExecStopPost=/usr/libexec/qdistro/qdistro-tier3s-cleanup --unit %n
```

**The start acknowledges a running launch (astra/fable A r1).** The unit is
`Type=notify`, `NotifyAccess=main`, `TimeoutStartSec=120`. The spawn sends
`READY=1` only once the launch is recorded `phase=running`, or once a short
workload that exited 0 before it was seen running has been torn down and
verified. Every refusal (profile, probe, broker, binding, seccomp file,
image) and every failure before that exits without `READY=1`, so `systemctl
start` fails and the manager sees it (§6). The spawn unsets `NOTIFY_SOCKET`
at once and keeps it for its own `systemd-notify` only; the probe, podman
(`env -i`) and the scope never get it.

**Who can send `READY=1` (astra A r2 #4, fable A r2 P3-2).** Only the unit's
main PID: the root spawn (the launch helper execs into it). The spawn runs
`systemd-notify --ready` as a direct child, as root; `systemd-notify` first
tries to send with its parent's PID, which takes privilege, so the message is
attributed to the spawn itself. Every other process in the launch unit's
cgroup is refused by systemd however it learns the socket path (the path is
not a secret): the admin processes the spawn runs there (the probe's podman,
`podman image exists`, the start poll's inspect, `dbus-send`, the resolver)
and the admin podman calls of the pre-launch reaper (which run in their own
call scopes, §4), with anything they start, such as a container's OCI runtime
helper during a `podman rm` of a stale labelled container. A forged
`READY=1` therefore cannot acknowledge a launch that never ran. The sandbox
runs in the owning scope, not in the unit's cgroup. VM test: s121 step 6
moves an admin process into the launch unit's cgroup while the spawn waits,
has it send `READY=1`, and finds the unit still `activating`; with a runtime
`NotifyAccess=all` drop-in the same forgery completes the start (the positive
control).

Podapps (`LaunchPodApp` analogue) would use the same spawn with no
`TIER3S_SILO`, unit `qdistro-tier3s-app@<token>.service`. **A-ii ships silos
only**: no app unit is installed, no session-manager API starts one, and
`spawn-tier3s.sh` refuses a launch without `TIER3S_SILO` at step 3.

The launch stanza (`/run/qdistro/silo-launch/<name>.env`, root 0600) holds
exactly `TIER3S_SILO`, `TIER3S_BINDING` (the row's `template_silo`),
`TIER3S_WORKLOAD`, `TIER3S_NETWORK=none`, `TIER3S_LAUNCH_TOKEN` (fresh per
start) and `TIER3S_ARGV_JSON`. `qdistro-tier3s-silo-launch` **parses** it
(fixed key set, each once, shell-quoted values decoded by `shlex`), never
sources it, requires `TIER3S_SILO` = the unit instance and admin = uid 1000,
and passes no `QDISTRO_PROFILE` (the spawn reads `/etc/qdistro/profile`).

The unit has **no `PartOf=qdistro-session-manager.service`** (unlike tier 2):
PartOf would turn a manager restart into a unit restart that relaunches with
the previous start's token. It has **`StopPropagatedFrom=` + `After=`
`qdistro-session-manager.service`** instead (owner O11, A-iii): a manager
**stop** enqueues a stop of every tier3s launch unit, which stops first (the
reverse of `After=`), so its `ExecStop`/`ExecStopPost` cleanup tears the
sandbox down while the manager is still up. No sandbox outlives its
supervisor. Measured on systemd 261 (A-iii dev VM, re-asserted by s122): the
propagation also fires when the manager is **restarted** (the launch unit gets
a stop job, never a restart, so no relaunch with the old token) and when the
manager **fails** (SIGKILL: "Failed with result 'signal'" is followed by the
launch unit's stop job). Reconciliation (§4) covers what propagation cannot:
launch units the manager never started (started while it was down) and
labelled containers without a live unit.

Journald files the workload's output (podman's attached stdout and conmon's
log driver) under the owning **scope** unit, where those processes run, not
under the launch unit: read it with `journalctl _SYSTEMD_UNIT=qdistro-tier3s-<token>.scope`
(A-ii smoke r1).

**The GUI bridge (B-i).** For a workload declared `GUI=1` (§7) the spawn
adds one host-side process class between publishing the record and running
podman: the **waypipe bridge client**. The root spawn runs it as a direct
child (in the **launch unit's** cgroup, never in the scope), dropped to
admin by `runuser -u admin`, wrapped by `qdistro-secctx-exec` with the
identity triple engine `qdistro.tier3s`, app-id `qdistro.tier3s.<silo>`,
instance `<token>` — the tag the admin compositor sees on every window the
sandbox publishes. It binds
`/run/qdistro-tier3s/<token>/link.sock` (the token-scoped bridge socket,
admin-owned mode `0600` under the client's `umask 0177`; keep-id means no
tier-3 group dance) and prefixes titles `[3s:<silo>] `. The podman run then
bind-mounts the per-launch dir at `/run/qdistro/link` with
`--runtime-flag=host-uds=open`, and the image-side
`qdistro-tier3s-entrypoint` runs `waypipe -s /run/qdistro/link/link.sock -o
--no-gpu server -- <argv>`: the sandbox is the waypipe **server**, the admin
side the **client** (Phase S topology; `--no-gpu` on both ends, `-o`
one-shot). The client is a new lifecycle member: recorded
(`bridge_client_*`, §4), registered with the broker before `podman run`
(§5), and killed and verified dead by the cleanup (§4). It connects out to
the admin compositor only (`WAYLAND_DISPLAY=wayland-1`); its peer is the
sandboxed server through the bind-mounted socket — it never touches the
control record root, which is never mounted.

Every podman call runs **as admin (uid 1000)**, rootless, `--userns=keep-id`
(D4 C1). Root does three things only: supervise, create the scope, and tear
down through the recorded scope. Nothing runs podman or runsc as root.

### Installed paths (root-owned; only with `QDISTRO_TIER3S=1`)

**Opt-in (owner O10, A-iii).** `install-session-manager.sh` installs the rows
below only when `QDISTRO_TIER3S=1`. Unset, empty or `0` installs nothing
tier-3s-specific (it logs `tier 3s not installed`); any other value is an
error (exit 2), so a typo never silently skips it. Re-running without the flag
does not remove an earlier install. The session manager's tier3s branches and
the broker prefix ship in their shared files regardless; without these paths
the manager skips reconciliation and a tier3s start fails (no unit, no spawn).

| Path | From |
|---|---|
| `/usr/lib/qdistro/tier3s/` | root-owned copy of `tier3s/`: `spawn-tier3s.sh`, `probe.sh`, `RUNSC_RELEASE`, `tier3s-runsc`, `seccomp/<workload>.json` (the probe compares the installed wrapper and pin against this copy) |
| `/usr/lib/qdistro/tier3s/workloads/<workload>.env` | `tier3s/workloads/`: the per-workload declarations (§7), parsed never sourced |
| `/usr/lib/qdistro/tier3s/qdistro-tier3s-entrypoint` | `tier3s/qdistro-tier3s-entrypoint`: the image-side waypipe-server launcher (§7); also the build-context copy |
| `/usr/lib/qdistro/tier3s/Containerfile.<workload>`, `headless-smoke.sh`, `configure-snapshot-repos.sh`, `SNAPSHOT`, `make-tier3s-image.sh` | `tier3s/`: the image-build context (§7), so an installed tree can rebuild every workload image as admin |
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
  `KEY=VALUE` lines, **parsed, never sourced**. The record appears **complete
  or not at all** (astra/fable A r1): the spawn builds it in
  `/run/qdistro-tier3s-ctl/.new-<token>/` and renames it into place under the
  global lock, after checking the token is unused, and creates the per-launch
  dir under the same lock. No scope exists before it. Later updates
  (`phase=running`) take the token's lock and replace `state` by rename.
- A `.new-<token>` dir exists only while a spawn holds the global lock (its
  EXIT trap removes a leftover one before releasing the lock, fable A r2
  P3-6); one seen by `--reap-stale` (which takes that lock) belongs to a
  spawn that died, and is removed.
- `/run/qdistro-tier3s-ctl/.call-<pid>-*`: a cleanup run's private work dir
  (its calls' output files), removed when the run exits; `--reap-stale`
  removes those of runs that no longer exist (SIGKILLed). A dir whose pid
  name is **reused** is stale too: a `.call-<pid>-*` whose mtime predates
  `/proc/<pid>/stat`'s starttime (converted with `btime` + `CLK_TCK`)
  belongs to a dead call and is swept; a live call's dir is never older
  than its own pid.
- An **incomplete record** (a token dir without `state`, which earlier code
  could leave behind when the spawn died between `mkdir` and its first write)
  has no unit. The cleanup removes it only on positive evidence that nothing
  ran under it: `qdistro-tier3s-<token>.scope` positively dead, its cgroup
  absent or empty, and admin's podman listing (which must succeed) shows no
  container with that token label. Otherwise it is preserved and the cleanup
  exits non-zero. `--unit <any unit>`, `--reap-stale` and `<token>` all
  apply this, and the manager's stop verification keeps counting it for every
  unit until it is gone. The cost of never ignoring it (fable A r2 P3-4):
  while one incomplete record is legitimately preserved (its scope is live or
  unknown, or a container still carries its token), **every** tier3s
  `StopSilo` reports "did not take effect" (the silo stays Active) and every
  refused `StartSilo` ends Active + `start_unresolved` instead of Stopped,
  for unrelated silos too; the manager's log names the surviving
  `/run/qdistro-tier3s-ctl/<token>` ("control record(s) … survive the stop").
  Only pre-r1 code could leave one, and preserving it needs real evidence;
  the operator's fix is the cause the cleanup names, then
  `qdistro-tier3s-cleanup <token>`.
- `/run/qdistro-tier3s/<token>/` admin 0700 under a root 0755 parent: the
  per-launch dir. Phase B mounts it into a GUI sandbox at
  `/run/qdistro/link`; it holds `link.sock` (the bridge socket the host
  waypipe client binds, admin-owned 0600) and **no control state** (03 step
  8). A headless launch mounts nothing from it.

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
| `gui` | bridge (B-i) | `1` when the workload declared `GUI=1` |
| `launch_record` | bridge (B-i) | the secctx launch-record path, `/run/user/<admin-uid>/qdistro-tier3s-launchrec-<token>.pid` |
| `bridge_wrapper_pid`, `bridge_wrapper_starttime` | bridge (B-i) | the supervisor's direct child heading the client chain (`runuser` → `qdistro-secctx-exec`) + field 22 |
| `bridge_client_pid`, `bridge_client_starttime` | bridge (B-i) | the inner waypipe-client pid the launch record published (the pid `RegisterLaunch` registered) + field 22 |

At start the spawn **verifies** that the conmon and Sentry pids sit inside
the scope's cgroup before recording them. If they do not, it tears the
launch down and fails. For a GUI launch the spawn likewise verifies that
the bridge client pid sits in the **launch unit's** cgroup (it is a child
of the supervisor, not a scope member) before registering it.

### Locks and bounds (astra A r1 #5, A r2 #2/#3, fable P3-1/P3-2/P3-7, A r2 P3-1)

- **Per token:** `flock` on the record dir `/run/qdistro-tier3s-ctl/<token>`.
  A teardown holds it for that token only; the spawn's `phase=running` update
  takes it too. `<token>` and `--unit` wait up to 120 s for it (a concurrent
  teardown of the same token: ExecStop and the spawn's EXIT trap); after the
  wait, a record that is gone counts as torn down. `--reap-stale` never waits:
  a token another teardown holds is skipped (logged as busy, not a failure).
- **Global:** `flock` on `/run/qdistro-tier3s-ctl/.lock`, held only for short
  steps with no podman or systemd call inside: the spawn's record publication
  (check unused, write `.new-<token>`, rename, create the per-launch dir), the
  removal of an orphan per-launch dir (re-checking that there is still no
  record) and the `.new-*` sweep. Waits are bounded (60 s).
- **Calls (what "bounded" means).** Every external call of the cleanup
  (`systemctl`, admin's `podman`, the `python3` label decoder) runs in the
  cleanup's own shell under one supervisor, with these properties:
  - its bound: podman 20 s, `podman stop` grace + 20 s, systemctl queries
    10 s, a scope stop 30 s, the decoder 30 s, each capped by what is left
    of a batch deadline. A call that times out, or that the deadline cut,
    is a **failed query**, never "absent" or "dead";
  - its stdout and stderr go to files in the run's work dir, each
    hard-capped at 64 KiB (`ulimit -f` in the call's subshell); a call that
    reaches the cap is a **failed query** (fable A r3 P3-4). Output never
    goes to a pipe, so a leftover process holding it blocks nothing (the
    cleanup reads the files once the call is over, and the manager's pipe
    from the cleanup is never handed to a call);
  - it runs in `timeout(1)`'s own process group, and that **whole group is
    SIGKILLed as soon as the call returns or its bound passes**, whether or
    not the call itself exited: a descendant that ignores SIGTERM, or
    outlives its parent, does not survive the call;
  - **admin calls** (every `podman`, through `runuser`) also run in their own
    root-created transient scope `qdistro-t3s-call-<pid>-<n>-<rand>.scope`
    (`DefaultDependencies=no`, so it also works during shutdown). `runuser`
    and rootless podman start helpers in **new sessions**, which a
    process-group kill misses; the scope's cgroup holds them all. After the
    call the cleanup writes `cgroup.kill` and the cgroup must then be empty
    or gone (else the call failed). If the cleanup itself dies first,
    systemd stops the scope at `RuntimeMaxSec` — the call's bound + 6 s
    measured **from the scope's activation**, which is when
    `RuntimeMaxSec` starts counting, not from the call's entry — and
    SIGKILLs it `TimeoutStopSec` (5 s) later (fable A r3 P3-1);
  - a TERM/INT/HUP to the cleanup kills the call in flight (group and
    scope) and exits 143 with every record preserved;
  - a `podman container exists` verdict counts only through an in-call
    `PMRC=<rc>` line the dropped-privilege command itself prints after
    podman ran (astra+fable A r3 P1): the
    `timeout`→`systemd-run`→`runuser` chain's own status can be a bare 1
    without podman ever running, so it is never read as "absent". The
    verdict must be the call's **complete output file**, compared byte
    for byte: exactly `PMRC=0\n` or `PMRC=1\n` — a NUL byte, extra bytes
    or any other content is a failed query (a captured *string* stops at
    the first NUL, so only the file proves nothing followed; sol r5 P1).
    The same discipline covers the initial check, the vanished re-queries
    and both post-removal checks, and every other one-line answer a call
    returns (`ActiveState`, `BindsTo`, `ControlGroup`, the inspect line):
    each counts only when it is the call's complete output. The admin's
    NSS lookup (`getent`, run under a deadline-capped `timeout`, resolved
    once per run) is the one call outside the supervisor; it is still
    bounded, so a wedged NSS cannot hold the token lock, and the lookup's
    own exit status gates acceptance — a provider that prints a
    complete-looking line and then stalls is killed at the bound and is a
    failed lookup, not a result (fable A r3 P3-2, sol r5 P3-4);
  - lock fds are closed for every call; `systemctl` runs with
    `--no-ask-password`.
  The guarantee, exactly: every process of an admin call is SIGKILLed by
  the call's bound + 5 s while the cleanup runs, and by its bound + 11 s
  (systemd's `RuntimeMaxSec` + `TimeoutStopSec`, counted from the call
  scope's activation) if the cleanup was
  SIGKILLed; only a process that
  root (or systemd on root's behalf) moves out of the call's cgroup escapes,
  and admin cannot move a process out of a root-owned cgroup. Root's
  `systemctl` and `python3` calls start no helpers; their process group is
  killed the same way, but a SIGKILL of the cleanup itself leaves such a
  call to its own `timeout(1)` (which still enforces the bound). The token
  lock is released when the call is over, whatever its descendants do.
- **Batches and the deadline:** `--unit` and `--reap-stale` take
  `--deadline` (default 90 s; the spawn's reaper uses 30 s). After it no new
  token, no label listing and no orphan per-launch dir is started, and
  inside it every call, lock wait and wait loop is capped by the time left.
  Waits are by the clock, not by a query count (the 20 s wait for systemd's
  BindsTo stop of an orphan scope, the scope-emptying waits). The clock is
  `EPOCHREALTIME` — realtime, not monotonic (sol r5 P3-2): a forward clock
  step ends a wait or a batch early; a **backward step extends it by the
  amount stepped** — a one-hour backward step can hold a nominal 90 s reap
  for about an hour. So a batch ends at its (wall-clock) deadline plus at
  most the kill grace of the call in flight
  (`timeout -k 5`, then up to 5 s for its scope to empty) and local file
  work. What it did not reach, or reached too late, is preserved, and the
  run exits non-zero. One token (`<token>`, the spawn's EXIT trap) has no
  deadline: its calls are bounded one by one, and only calls that each
  answer just under their bound add up, to a few minutes at most (record
  preserved). The manager bounds the whole helper run at 300 s
  (`_T_SYSTEMCTL_STOP`) and treats a timeout as a failed cleanup.

So a wedged teardown of launch A blocks neither B's teardown nor a new
launch's record publication (tested with a hanging fake podman), and a
wedged call cannot keep A's own token lock held past its bound.

### `qdistro-tier3s-cleanup` (root; the final teardown path)

```
qdistro-tier3s-cleanup <token>                 # one launch
qdistro-tier3s-cleanup --unit <launch unit> [--deadline <s>]
                                               # every record whose unit= matches (ExecStop/ExecStopPost),
                                               # plus incomplete records (recovered on evidence)
qdistro-tier3s-cleanup --reap-stale [--except-unit <unit> --token <new token>] [--deadline <s>]
                                               # records whose unit is positively dead, plus labelled
                                               # containers with no live launch unit (reconciliation)
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
3. **The token scope's owner, before anything is stopped** (sol A-iii r4 P1,
   astra/fable A r1). The scope's state is live, dead or unknown (unknown:
   preserve). If live, systemd's `BindsTo=` of the scope must be exactly the
   record's `unit` (the spawn always creates it with `BindsTo=<unit>`) and its
   `ControlGroup` the recorded `scope_cgroup` (when recorded), under
   `…/qdistro-tier3s-<token>.scope`; for the reaper the unit must also be
   positively dead at that moment. Every query's exit status counts: a failed
   or empty answer for a live scope is looked at again and then preserves.
   So a valid but stale unit name in a record never tears down a live launch
   that another unit owns.
4. Query `podman container exists <container>` as the recorded admin. 0 is
   present, 1 is absent, anything else is "**podman query failed**" (error,
   preserve) — and only when the call's complete stdout file is exactly the
   `PMRC=<rc>` line (§4; a NUL or any extra byte is a failed query). It
   never conflates a failed query with "no container" (unlike
   tier 2's `spawn-tier2.sh` reaper, which suppresses errors). If present,
   `podman container inspect` gives its ID and its `qdistro_tier3s_token`
   label, which must equal the token.
5. If present: `podman stop -t 10 <id>`, then `podman rm -f --ignore <id>`,
   then re-query by ID: it must be absent; a stop or rm error is an error. A
   container that vanishes concurrently is re-queried, and only a definitive
   "absent" goes on. Acting on the ID means a same-named container created
   meanwhile is never touched.
6. Scope again, re-validated against the first lookup. If live: wait for its
   cgroup subtree to be empty, else `systemctl stop <scope_unit>` (systemd
   kills by cgroup) and wait again. Then every cgroup the scope can have
   (systemd's answer, the recorded one, and `/system.slice/<scope>`, where
   `systemd-run --scope` puts it) must be absent or empty by a **complete**
   recursive scan: a failed `find` or any unreadable `cgroup.procs` is "not
   empty" (astra A r1 #2). A dead scope whose cgroup still holds a process is
   an error.
7. The recorded `(pid, starttime)` of conmon and the Sentry must be dead. If
   either is alive with the same starttime, it escaped the scope: error,
   preserve. It is never killed by pid, uid (100000 is shared by every
   keep-id launch, 05 fact 4) or process name.
8. **The bridge client (B-i, GUI launches only).** Right after step 3's
   scope check the teardown SIGTERMs the recorded `bridge_client_pid` and
   `bridge_wrapper_pid` (each matched by starttime, a zombie already counts
   as dead), waits a bounded grace, then SIGKILLs what survives — the
   sandboxed waypipe server's peer goes first. Here at the end, any
   recorded bridge pid still alive with its recorded starttime (and not a
   zombie) is an escape: error, preserve. Then the recorded `launch_record`
   file (shape-checked under `/run/user/<admin-uid>/`) is removed.
9. Remove `per_launch_dir` (a real directory under the root-owned parent;
   `rm -rf` does not follow symlinks), then the control dir.

A missing control record with a leftover per-launch dir: the dir is removed
only when `qdistro-tier3s-<token>.scope` is not active, and under the global
lock after re-checking that there is still no record. Asked for explicitly
(`cleanup <token>`), a live scope is an error. From the reaper, a live scope
whose `BindsTo=` launch unit is still live is left alone; one bound to a
positively dead unit (or to the spawn's own unit under an older token) is
waited for up to 20 s by the clock (systemd's own BindsTo stop), then stopped by the reaper,
and the dir removed (A-iii s122: a reconciliation reap raced the scope stop
and left the dir behind).

### Reaper and reconciliation

Every reaper decision reads a unit's state as **live**, **dead** (systemd
positively answers `inactive` or `failed`) or **unknown** (the query failed or
answered anything else). The state comes from `systemctl show -p
ActiveState --value`, and only from a call that **completed with status 0**
whose complete output is the one state line (astra A r2 #1, sol r5 P1): an
answer printed by a query that then timed out, was killed or failed — or a
NUL-truncated prefix of a larger output — is unknown, whatever it said. It acts only on dead; unknown preserves the record,
container, scope and per-launch dir and makes `--reap-stale` exit non-zero
(sol A-iii r2). A live orphan scope (no record) is stopped only when its
`BindsTo=` positively names a tier3s launch unit that is dead, checked before
and again after the bounded wait (sol A-iii r1). An unrecorded labelled
container is reaped only when its `qdistro_tier3s_unit` label is a valid tier3s
launch unit that is dead (or is the spawn's own `--except-unit`); a missing or
invalid label preserves it and is an error (sol A-iii r3). For a recorded
launch and an unrecorded labelled container alike, the token scope must be
dead, or live and bound to exactly that unit (rule 3 above, sol A-iii r4):
a valid but **stale** unit name never authorizes the teardown of a scope
another unit owns. The unrecorded path then removes the container by ID and
leaves the scope to the guarded orphan path; there is no unconditional scope
stop.

The spawn runs `qdistro-tier3s-cleanup --reap-stale --except-unit <own unit>
--token <its new token> --deadline 30` before publishing its record. A record
whose `unit` is positively dead is stale, and so is a record (or labelled
container) carrying the spawn's own unit under **another** token (one unit
runs one launch at a time); the new token itself is never a candidate, and
`--except-unit` is refused without a valid `--token`. Reap failures are
logged and do not block the new launch. The reaper never waits on a token
another teardown holds, so launches do not stall each other.

Restart reconciliation (A-ii, as implemented):
- **No tier3s launch survives a session-manager restart.** At startup
  (`autostart_pass`, before the autostart sweep) the manager stops every
  live `qdistro-tier3s-{silo,app}@*.service` (ExecStop/ExecStopPost run the
  verified cleanup), then runs `qdistro-tier3s-cleanup --reap-stale`. The
  sweep then relaunches silos that were Active or are autostart, each with a
  fresh token. Skipped on a host without the tier3s install; failures are
  logged and never block other silos.
- A manager **stop** (no restart) stops every live launch unit through
  `StopPropagatedFrom=` (§1, owner O11): each unit's `ExecStop`/`ExecStopPost`
  runs the verified cleanup before the manager itself stops. systemd does the
  same on a manager restart or failure (§1), so reconciliation at the next
  start is the recovery path for launches the manager did not start, records
  of failed teardowns and labelled containers without a live unit.
- `--reap-stale` also lists labelled containers as admin (`podman ps -a
  --filter label=qdistro_tier3s_token --format json`), decoded and validated
  in python3 (fable A r1 P2-2): label values are admin-chosen bytes, so they
  are never split on a separator. Each entry's token, unit label and ID must
  match their patterns; an entry that does not is preserved and counted as a
  failure. (A-iii s122 had found that podman 6 rejects `index .Labels` in a ps
  template; the JSON listing replaces the template.) A failed listing or bad
  JSON is an error, never "nothing to reap". A container whose
  `qdistro_tier3s_unit` names a positively dead unit, and whose token scope
  passes rule 3, is removed by ID (`podman rm -f -t 10 <id>`, re-queried),
  reported as "unrecorded"; its scope and per-launch dir then go through the
  guarded orphan path.
- Containers carry the labels `qdistro_tier3s_token=<token>` and
  `qdistro_tier3s_unit=<launch unit>`.

### Teardown paths (A-iii tests each; DONE bar 2)

| Path | What happens |
|---|---|
| normal exit | `podman run --rm` returns. The spawn's EXIT trap runs `cleanup <token>`, which verifies absence and removes the records. Then the unit's `ExecStopPost` finds nothing |
| plain `podman stop` (admin) | reaches the sandbox through the wrapper root, so it continues as normal exit |
| session-manager / `systemctl stop` | `ExecStop` cleanup with the scope alive, then the scope stops (Before=, BindsTo) |
| manager service stop (`systemctl stop qdistro-session-manager`, O11) | `StopPropagatedFrom=` enqueues the launch unit's stop, ordered before the manager's; then as the row above |
| launcher SIGKILL / service failure | `ExecStopPost` cleanup runs while BindsTo stops the scope, then verifies |
| manager restart, state lost | `--reap-stale` + unit stop |
| refused launch (broker, profile, probe, image, …) | exits before `READY=1`: `systemctl start` fails, `ExecStopPost` runs the cleanup, the manager verifies the launch gone and reports the refusal (§6) |
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
   lane. `TIER3S_SILO` is required in Phase A (pod apps refused, §1);
   `TIER3S_BINDING` (default `TIER3S_SILO`) must be a silo name.
4. **Probe:** `/usr/lib/qdistro/tier3s/probe.sh --user admin` must exit 0.
   Anything else refuses with its `RESULT` line (prerequisites, state root,
   pinned runsc, wrapper). There is no fallback tier. The probe runs **before**
   the broker gate and does work as admin on every launch attempt, denied ones
   included: it imports `localhost/tier3s-probe:empty` and runs
   `podman create`/`rm` of a never-started `tier3s-probe-<pid>` container to
   check the runtime (fable A r1 P3-8). That is no `podman run`; the s121
   event oracle filters exactly these two events, and nothing else.
5. **Read-only resolution:**
   - the workload name, and its seccomp file
     `/usr/lib/qdistro/tier3s/seccomp/<workload>.json`, which must exist
     (there is no podman-default fallback);
   - the **workload declaration** `/usr/lib/qdistro/tier3s/workloads/
     <workload>.env` (B-i): **parsed, never sourced** — blank lines and `#`
     comments, then `GUI=0` or `GUI=1` at most once; anything else refuses.
     A missing file means a headless workload (`GUI=0`); a present but
     malformed one refuses the launch;
   - the image: for a templated silo, `qdistro-resolve-binding <binding>
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
10. Reap stale launches (`--except-unit <unit> --token <token> --deadline
    30`). Then, under the global lock: refuse a token already in use, arm the
    EXIT/TERM trap's `cleanup <token>`, publish the complete record
    (`.new-<token>` renamed into place) and create the per-launch dir.
11. `podman image exists` as admin.
12. **GUI only (B-i): the waypipe bridge client, before `podman run`.**
    `GUI=0` skips this step entirely and the launch is unchanged from
    Phase A. For `GUI=1`:
    - the admin compositor socket `/run/user/<admin-uid>/wayland-1` must
      exist as a socket, else the launch refuses (checked before the broker
      gate);
    - the spawn starts, as its direct child in the launch unit's cgroup,
      `runuser -u admin -- env -i … XDG_RUNTIME_DIR=/run/user/<uid>
      WAYLAND_DISPLAY=wayland-1 DBUS_SESSION_BUS_ADDRESS=unix:path=<rt>/bus
      QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1
      QDISTRO_LAUNCH_RECORD_PATH=/run/user/<uid>/qdistro-tier3s-launchrec-<token>.pid
      QDISTRO_LAUNCH_RECORD_TOKEN=<token>
      qdistro-secctx-exec --sandbox-engine qdistro.tier3s
      --app-id qdistro.tier3s.<silo> --instance-id <token>
      -- waypipe -s /run/qdistro-tier3s/<token>/link.sock -o --no-gpu
      --title-prefix "[3s:<silo>] " client`, under `umask 0177` so the
      socket is admin-owned 0600;
    - it reads the launch record with a bounded retry: the file must
      contain `<inner pid> <token>` whose token is the one just handed
      down (a pre-created admin-owned file cannot spoof the registration;
      `secctx-exec` creates it `O_EXCL|O_NOFOLLOW` 0600 and refuses a path
      whose parent is not exactly `XDG_RUNTIME_DIR`), the inner pid must be
      live, in the **launch unit's** cgroup, and its starttime recorded;
    - `link.sock` must appear as a bound socket within the bound;
    - `AdminBroker1.RegisterLaunch(silo, "qdistro.tier3s",
      "qdistro.tier3s.<silo>", <token>, "", <inner pid>, "tier3s", 0)` —
      **mandatory, as root, with bounded retries**: any failure refuses the
      launch before `podman run` (B-i is stricter than tier 3's
      warning-only registration);
    - `bridge_wrapper_pid`, `bridge_client_pid` (+ starttimes),
      `launch_record`, `gui=1` go into the control record as soon as they
      are known, so a refusal after the client started is still torn down.
13. `systemd-run --scope …` (D-A3b) in the background. Poll `podman inspect`
    until running, within a 60 s **polling budget** by the clock (each
    inspect bounded to 5 s and its answer awaited 7 s at most; fable A r2
    P3-3). The budget is not a strict bound — one iteration may overrun it
    by up to those per-iteration bounds — so the unit's `TimeoutStartSec`
    (120 s) remains the outer bound on the start (astra A r3 P3-3). Then
    verify that conmon and
    the Sentry are in the scope, then record them (`phase=running`, under the
    token's lock) and send `READY=1` (§1: from the spawn's own PID).
    A workload that exits 0 before it was seen running is torn down and
    verified first, then `READY=1`. Wait, and exit with podman's status.

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
      [-v /run/qdistro-tier3s/<token>:/run/qdistro/link:rw]   # GUI only (B-i)
      [--runtime-flag=host-uds=open]        # GUI only (B-i): bind-mounted unix sockets allowed
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
- **Installer:** `scripts/install/install-session-manager.sh` installs, only with
  `QDISTRO_TIER3S=1` (O10), the §1
  paths and units (only those lines).

Broker (A-ii): add `"qdistro.tier3s.spawn:"` to the rules-only prefix
tuple, add a unit test, and document it in `doc/permissions.md`. Tier 3s has
no SELinux type; `_ADMIN_HOSTILE_SELINUX_TYPES` is unchanged (Phase D).

### As implemented in A-ii

- create **and** start refuse every profile but dev (`/etc/qdistro/profile`,
  parsed, root-owned and not group/other-writable, else unset) with
  "tier 3s is dev-profile only in this PoC (profile=…); there is no hardened
  launch path and no fallback tier", before any state change. The spawn and
  probe refuse again on their own.
- `validate_launch` (tier3s): `workload` matches the spawn's
  `^[a-z0-9][a-z0-9-]{0,40}$`, `template_silo` is a silo name, `network` is
  exactly `none` (no legacy mapping), argv as tier 2. An empty argv uses the
  workload's default (`headless-smoke` → `qdistro-tier3s-smoke`), else
  `[workload]`.
- **Start (astra/fable A r1).** `StartSilo` runs `systemctl start` of the
  `Type=notify` unit with its own bound (`_T_TIER3S_START` = 135 s, above the
  unit's `TimeoutStartSec=120`), so it returns once the launch runs, or fails
  when the spawn refuses it or the launch dies first. On such a failure the
  manager clears the Active intent only after verifying the launch gone (the
  stop verifier `tier3s_silo_running`: unit inactive/failed, no container, no
  control record); then the silo is Stopped with `observed_status=failed`,
  and the D-Bus error says the launch was refused or failed before it ran,
  with the spawn's `REFUSE:` line when the journal already has it (best
  effort). A retry after the cause is fixed is a real start. If the launch
  cannot be verified gone, the silo stays Active with `start_unresolved`
  (stop before retrying), like a start timeout (`StartNotCancelled`). It
  starts nothing else (no fallback). On a manager start the autostart sweep
  relaunches an Active silo once; a refused relaunch leaves it Stopped.
- `StartSilo` is synchronous on the manager's main loop (astra A r2 #5,
  fable A r2 P3-5). The 135 s is the bound of the `systemctl start` call
  only. The whole start path holds the main loop, and with it every other
  D-Bus call and main-loop callback of the manager: the start (≤ 135 s),
  then on a timeout the compensating stop (`_T_SYSTEMCTL_CANCEL`, ≤ 30 s),
  or on a failed start the stop verifier (`systemctl is-active` ≤ 30 s,
  `podman container exists` ≤ 30 s) and the best-effort refusal lookup
  (`systemctl show` + `journalctl`, ≤ 30 s each). Worst cases: about 165 s
  (timeout path) and 255 s (failed path); measured in the VM runs: seconds.
  A D-Bus client with busctl's or qdshell's default 25 s call timeout sees
  its own timeout for a start that takes longer; the start goes on and the
  silo's state is still right afterwards. The manager has no `WatchdogSec`
  and owns its bus name before the autostart sweep, so a slow start cannot
  fail the manager's own start. Making StartSilo asynchronous is a
  manager-wide change outside Phase A.
- `systemctl start` vs the unit's `TimeoutStartSec=120`: systemd's timeout
  fires first; the manager's 135 s covers the teardown after it only when
  that ends within 15 s, else the start is reported unresolved (Active +
  `start_unresolved`, stop before retrying). Both answers are honest.
- `tier3s_silo_running` (stop verification) is true unless the unit is
  `inactive`/`failed`, admin's `podman container exists` answers 1, and no
  control record names the unit (an unreadable control dir counts as a
  record). Scanning records by `unit=` covers a token the manager lost. When a
  **completed** `systemctl stop` still leaves records (the unit's cleanup
  failed and the unit is now failed), the manager runs `qdistro-tier3s-cleanup
  --unit <unit>` once and re-verifies, so a retried StopSilo can succeed;
  otherwise the silo is forced Active and the error names the unit.
- `FreezeSilo`/`ResumeSilo` refuse a tier3s silo in any state, audited as
  `deny`.
- runsc keeps one shared, empty, read-only `null-netns` file in the state root
  for `network=none`; it is not per-container state and is never removed.

## 7. Workloads, images and seccomp (D-A4, D-A5; B-i adds the GUI rows)

### Workload declarations (B-i)

`tier3s/workloads/<workload>.env` declares what a workload needs of the
launch path. It is **parsed, never sourced**: blank lines and `#` comments,
then `GUI=0` or `GUI=1` at most once — anything else refuses the launch. A
missing file means `GUI=0` (headless). `GUI=1` adds the whole §5 step-12
bridge (compositor check, secctx client, launch record, RegisterLaunch) and
the two podman additions (`-v /run/qdistro-tier3s/<token>:/run/qdistro/link:rw`,
`--runtime-flag=host-uds=open`); `GUI=0` leaves the launch byte-identical to
Phase A.

| Workload | `GUI` | What it is |
|---|---|---|
| `headless-smoke` | 0 | the Phase A smoke checks (`qdistro-tier3s-smoke`) |
| `weston-terminal` | 1 | `weston-terminal` through the waypipe bridge |
| `foot` | 1 | `foot` through the waypipe bridge |

### Images

- `tier3s/Containerfile.headless-smoke` builds from
  `registry.opensuse.org/opensuse/tumbleweed:${SNAPSHOT}` (the `snapshot.conf`
  pin), with repos pinned by the tier3s-local `configure-snapshot-repos.sh`.
  It is labelled `org.qdistro.snapshot=<pin>` and also writes
  `/etc/qdistro/tier3s-image` with the pin. It installs `glibc-locale-base`
  (UTF-8 locale), sets `LANG=C.UTF-8`, has passwd entry
  `admin:x:1000:1000::/home/admin:/bin/bash`, and `/home/admin/.cache` exists
  for the tmpfs. Its default command `qdistro-tier3s-smoke` prints the
  checks the A-iii driver asserts.
- `tier3s/Containerfile.weston-terminal` and `tier3s/Containerfile.foot`
  (B-i): same base and pin discipline, plus `weston` / `foot`, `waypipe`,
  `fontconfig`, `dejavu-fonts`, `xkeyboard-config`, `terminfo-base` and
  `glibc-locale-base` (the Phase S package set: W8 needed the UTF-8 locale;
  fontconfig's link() behaviour drove the `link` decision below). Both use
  `ENTRYPOINT ["/usr/local/bin/qdistro-tier3s-entrypoint"]`.
- `tier3s/qdistro-tier3s-entrypoint` is the image-side half of the bridge
  (B-i, ΔB3): it waits a bounded time for `/run/qdistro/link/link.sock` (the
  bind-mounted bridge socket) and execs `waypipe -s /run/qdistro/link/
  link.sock -o --no-gpu server -- "$@"`, so the workload's Wayland traffic
  crosses the mount as waypipe's byte stream. No fallback: a missing socket
  exits non-zero.
- `tier3s/make-tier3s-image.sh` is copied from `tier2/make-tier2-image.sh`
  (unify later). It tags `qdistro/tier3s-<workload>:latest`, refuses an
  image whose snapshot label differs from the pin, prints `IMAGE_ID=` and
  `IMAGE_DIGEST=`, and with `--oci-archive <dir>` saves
  `<dir>/tier3s-<workload>.oci.tar` for transfer into a qci worker. With no
  workload argument it builds every `Containerfile.*`; the staged context is
  exactly what the recipes COPY (Containerfiles, `SNAPSHOT`,
  `configure-snapshot-repos.sh`, `headless-smoke.sh`,
  `qdistro-tier3s-entrypoint`).
- `tier3s/cache-image-archive.sh` (host side, B-i): builds every workload
  once in a dev VM and keeps one `tier3s-<workload>.oci.tar` plus
  `tier3s-<workload>.manifest.txt` per workload under
  `~/.cache/qdistro/tier3s-images/<input key>/` (`manifest.txt` is the
  headless-smoke manifest, the name the current consumer reads). The input
  key covers every Containerfile, the entrypoint, the shared helpers and the
  snapshot pin.

### Seccomp

Per-workload profiles rendered by `seccomp/make-profiles.py` from tier 2's
`weston-terminal.json`. Each workload decides every call below explicitly;
the reasons are embedded in the rendered file. Phase S facts: runsc's
converter drops `fchmodat2` (an ALLOW is inert — the generator refuses one);
every ERRNO, the default included, becomes EPERM under runsc
(`defaultErrnoRet: 38` restores nothing); `setfsuid`, `setfsgid`,
`fadvise64` and `link` were all observed in terminal runs and were
non-fatal, `link` from fontconfig's cache locking.

| Call | `headless-smoke` | `weston-terminal`, `foot` | Why |
|---|---|---|---|
| `fchmodat2` | **DENY** (forced) | **DENY** (forced) | runsc 20260928.0's converter drops the name (`OCI seccomp: ignoring syscall "fchmodat2"`, `feasibility/31`), so an ALLOW is inert. `chmod -h` / `lchmod` give EPERM; plain `chmod` works |
| `llistxattr` | ALLOW | ALLOW | `ls -l` prints EPERM errors otherwise; read-only metadata answered by the Sentry |
| `setfsuid`, `setfsgid` | DENY | DENY | observed in Phase S terminal runs, non-fatal (EPERM); no workload needs fsuid switching |
| `fadvise64` | DENY | DENY | advisory; callers ignore the failure |
| `link` | DENY | DENY | fontconfig's cache lock link() falls back cleanly on EPERM (Phase S: non-fatal) |
| `syslog` | ALLOW | DENY | headless only: the gVisor `dmesg` banner corroboration; the terminals never read the kernel log |

Every ERRNO, the default included, is EPERM under runsc.

## 8. What is not claimed

- A green host unit test proves the launch scripts' logic against fakes, not
  scope delegation, `ExecStopPost`, placement or teardown. Those are VM facts
  (A-iii, through the qci VM lane).
- The B-i GUI bridge is proven at the argv/record level only (bridge client
  composition, secctx triple, launch record, RegisterLaunch arguments,
  mount and `host-uds=open`, refusal ordering). A window actually appearing
  tagged on the compositor, the `[3s:<silo>] ` title prefix, and cleanup of
  a live GUI launch are B-iii's VM evidence.
- The scope limits are set, not yet shown to be enforced (Phase C). Admin
  cannot raise them by writing the files (`feasibility-r3/20`); that is not
  an adversarial containment proof.
- Under SIGKILL of the launch service, teardown is systemd killing the
  scope's cgroup, then verification. It is not a graceful `podman stop`.
- `network=none` only, dev profile only, no KVM claim.
- The cleanup's bound for **one token** is per call, not a wall-clock
  limit: a single-token teardown whose calls each answer just under their
  bounds can take a few minutes before it gives up (preserving the record).
  A batch (`--unit`, `--reap-stale`) is wall-clock bounded by its deadline
  plus the kill grace (§4).
- Call supervision kills what stays in a call's process group or call scope.
  A process that root moves out of the call's cgroup is not covered. The
  manager's own `podman container exists` query (stop verification) is a
  Python `subprocess` with a timeout: it kills `runuser`, not podman's
  helpers in new sessions, and holds no tier3s lock.
